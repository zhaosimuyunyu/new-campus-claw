import asyncio
from functools import wraps
import json
import mimetypes
import os
from asgiref.sync import async_to_sync
from asgiref.sync import sync_to_async
from django.db.models import Q
from django.db import transaction
from django.http import FileResponse, HttpResponse, JsonResponse, StreamingHttpResponse
from django.utils.html import escape
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework.exceptions import AuthenticationFailed

from .agent_runtime import (RuntimeErrorWithStatus, ToolContext, attachment_prompt, builtin_tool_registry, model_tools,
                            create_artifact, event, index_material, model_stream, require_course_member, search)
from .models import AgentArtifact, AgentAttachment, AgentMessage, AgentRun, AgentThread, KnowledgeEntry, Material
from .agent_runtime import trace_sink
from .html_artifacts import extract_html, save_html_artifact


NO_HIT = "资料中未找到相关内容"


def _close_db_connection():
    from django.db import connection
    connection.close()


def agent_auth_required(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        try:
            result = JWTAuthentication().authenticate(request)
        except AuthenticationFailed:
            return JsonResponse({"error": "令牌无效或已过期"}, status=401)
        if result is None:
            return JsonResponse({"error": "请先登录"}, status=401)
        request.user = result[0]
        return view(request, *args, **kwargs)
    return csrf_exempt(wrapped)


def _json(request):
    try:
        return json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return {}


def _thread_data(thread):
    return {"id": thread.id, "title": thread.title, "course_id": thread.course_id,
            "status": thread.status, "created_at": thread.created_at, "updated_at": thread.updated_at}


def _message_data(message):
    return {"id": message.id, "role": message.role, "content": message.content,
            "citations": message.citations, "attachments": message.attachments, "metadata": message.metadata,
            "created_at": message.created_at}


@agent_auth_required
def material_preview(request, material_id):
    material = Material.objects.filter(id=material_id).select_related("knowledge_entry").first()
    if not material or not require_course_member(request.user, material.course_id):
        return JsonResponse({"error": "资料不存在"}, status=404)
    body = material.knowledge_entry.body_text if hasattr(material, "knowledge_entry") else ""
    if not body:
        return JsonResponse({"error": "资料尚未建立可预览文本索引"}, status=404)
    response = HttpResponse(f"<!doctype html><html lang='zh-CN'><meta charset='utf-8'><title>{escape(material.title)}</title>"
        f"<style>body{{font:16px/1.7 system-ui,sans-serif;max-width:850px;margin:40px auto;padding:0 24px;color:#23332d}}"
        f"pre{{font:inherit;white-space:pre-wrap;overflow-wrap:anywhere}}</style><h1>{escape(material.title)}</h1><pre>{escape(body)}</pre></html>",
        content_type="text/html; charset=utf-8")
    response["Content-Security-Policy"] = "sandbox; default-src 'none'; style-src 'unsafe-inline'"
    return response


def _artifact_data(item):
    return {"id": item.id, "name": item.name, "mime_type": item.mime_type,
            "source_url": f"/api/agent/artifacts/{item.id}/source/" if item.mime_type == "text/html" else None,
            "download_url": f"/api/agent/artifacts/{item.id}/download/",
            "preview_url": f"/api/agent/artifacts/{item.id}/preview/", "created_at": item.created_at.isoformat()}


def _finish_answer(user, thread, run, answer, citations, model_name, artifacts):
    created = []
    try:
        with transaction.atomic():
            locked = AgentRun.objects.select_for_update().get(pk=run.pk)
            if locked.status == AgentRun.Status.CANCELLED:
                raise asyncio.CancelledError
            display, sources = extract_html(answer)
            assistant = AgentMessage.objects.create(thread=thread, role="assistant", content=answer, citations=citations)
            for index, source in enumerate(sources, 1):
                created.append(save_html_artifact(user, thread, source, assistant.id, index))
            html_files = [_artifact_data(item) for item in created]
            assistant.metadata = {"tools": [{"name": e.payload.get("name"), "status": e.payload.get("status")} for e in run.events.filter(event_type="tool_result")], "run_id": run.id, "model": model_name, "artifacts": [*artifacts, *html_files], "display_content": display}
            assistant.save(update_fields=["metadata"])
            locked.status = AgentRun.Status.COMPLETED
            locked.finished_at = timezone.now()
            locked.save(update_fields=["status", "finished_at"])
            return assistant, html_files
    except BaseException:
        for item in created:
            item.file.delete(save=False)
        raise


def _accessible_thread(user, thread_id):
    thread = AgentThread.objects.filter(id=thread_id, owner=user).select_related("course").first()
    if not thread:
        return None
    if thread.course_id and not require_course_member(user, thread.course_id):
        return None
    return thread


@agent_auth_required
def threads(request):
    if request.method == "GET":
        visible = AgentThread.objects.filter(owner=request.user).filter(
            Q(course__isnull=True) | Q(course__memberships__user=request.user)).distinct()
        return JsonResponse([_thread_data(thread) for thread in visible.order_by("-updated_at")], safe=False)
    if request.method != "POST":
        return HttpResponse(status=405)
    data = _json(request)
    course_id = data.get("course_id")
    if course_id and not require_course_member(request.user, course_id):
        return JsonResponse({"error": "无权访问课程"}, status=404)
    thread = AgentThread.objects.create(owner=request.user, course_id=course_id, title=str(data.get("title") or "新对话")[:200])
    return JsonResponse(_thread_data(thread), status=201)


@agent_auth_required
def thread_messages(request, thread_id):
    thread = _accessible_thread(request.user, thread_id)
    if not thread:
        return JsonResponse({"error": "对话不存在"}, status=404)
    return JsonResponse([_message_data(item) for item in thread.messages.order_by("created_at")], safe=False)


@agent_auth_required
def knowledge_search(request, course_id):
    if not require_course_member(request.user, course_id):
        return JsonResponse({"error": "无权访问课程"}, status=404)
    query = request.GET.get("q", "").strip()
    mode = request.GET.get("mode", "hybrid")
    try:
        hits = async_to_sync(search)(course_id, query, mode, 10)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    except RuntimeErrorWithStatus as exc:
        return JsonResponse({"error": str(exc)}, status=exc.status)
    return JsonResponse({"hits": hits, "answer": None if hits else NO_HIT})


@agent_auth_required
def reindex_material(request, material_id):
    material = Material.objects.filter(id=material_id).select_related("course", "attachment").first()
    if not material or not require_course_member(request.user, material.course_id):
        return JsonResponse({"error": "资料不存在"}, status=404)
    if not require_course_member(request.user, material.course_id).role == "teacher":
        return JsonResponse({"error": "仅教师可重建索引"}, status=403)
    try:
        entry = index_material(material, "auto")
    except Exception as exc:
        entry = KnowledgeEntry.objects.filter(material=material).first()
        return JsonResponse({"status": entry.status if entry else "failed", "error": str(exc)}, status=503)
    return JsonResponse({"status": entry.status, "chunks": entry.chunks.count()})


@agent_auth_required
def reindex_course(request, course_id):
    membership = require_course_member(request.user, course_id)
    if not membership:
        return JsonResponse({"error": "课程不存在"}, status=404)
    if membership.role != "teacher":
        return JsonResponse({"error": "仅教师可重建索引"}, status=403)
    results = []
    for material in Material.objects.filter(course_id=course_id).select_related("attachment"):
        try:
            entry = index_material(material, "auto")
            results.append({"material_id": material.id, "status": entry.status, "chunks": entry.chunks.count()})
        except Exception as exc:
            results.append({"material_id": material.id, "status": "failed", "error": str(exc)})
    return JsonResponse({"results": results})


@agent_auth_required
def upload_attachment(request):
    if request.method != "POST":
        return HttpResponse(status=405)
    upload = request.FILES.get("file")
    if not upload or upload.size > 25 * 1024 * 1024:
        return JsonResponse({"error": "附件不能为空且不能超过 25 MB"}, status=400)
    course_id = request.POST.get("course_id") or None
    if course_id and not require_course_member(request.user, course_id):
        return JsonResponse({"error": "无权访问课程"}, status=404)
    item = AgentAttachment.objects.create(owner=request.user, course_id=course_id, file=upload,
        original_name=upload.name[:255], size=upload.size, mime_type=upload.content_type or mimetypes.guess_type(upload.name)[0] or "application/octet-stream")
    return JsonResponse({"id": item.id, "name": item.original_name, "size": item.size, "mime_type": item.mime_type})


async def _load_thread(user_id, thread_id):
    return await sync_to_async(lambda: AgentThread.objects.filter(id=thread_id, owner_id=user_id).select_related("course").first())()


async def _load_attachments(user_id, ids, course_id):
    return await sync_to_async(list)(AgentAttachment.objects.filter(id__in=ids, owner_id=user_id, course_id=course_id))


@agent_auth_required
def agent_stream(request, thread_id):
    if request.method != "POST":
        return HttpResponse(status=405)
    user = request.user
    thread = _accessible_thread(user, thread_id)
    if not thread:
        return JsonResponse({"error": "对话不存在"}, status=404)
    data = _json(request)
    content = str(data.get("content") or "").strip()
    if not content:
        return JsonResponse({"error": "消息不能为空"}, status=400)
    from .skill_runtime import runtime_config, configure_registry
    config, app, selected_skills, mcp_tools, unavailable_refs = runtime_config(user, thread.course_id)
    use_materials = bool(data.get("use_course_materials", config.use_knowledge if config else False))
    attachment_ids = [int(value) for value in (data.get("attachments") or []) if str(value).isdigit()]
    attachments = list(AgentAttachment.objects.filter(id__in=attachment_ids, owner=user, course_id=thread.course_id))
    if thread.title == "新对话":
        thread.title = content[:48]
        thread.save(update_fields=["title", "updated_at"])
    user_message = AgentMessage.objects.create(thread=thread, role="user", content=content,
        attachments=[{"id": item.id, "name": item.original_name, "mime_type": item.mime_type} for item in attachments])
    run = AgentRun.objects.create(thread=thread, model=os.getenv("MODEL_NAME", "mock"))

    async def stream():
        answer_parts = []
        citations = []
        artifacts = []
        model_name = run.model
        completed = False
        round_number = 0
        async def record_trace(kind, payload):
            await sync_to_async(event)(run, kind, {"round": round_number, **payload})
        trace_token = trace_sink.set(record_trace)
        try:
            await record_trace("context", {"app": app["id"], "template": config.template if config else "",
                "skills": [{"id": s["id"], "name": s["name"], "description": s["description"]} for s in selected_skills],
                "knowledge_enabled": use_materials, "mcp_refs": config.mcp_refs if config else [], "unavailable_tools": unavailable_refs})
            await sync_to_async(event)(run, "message_start", {"run_id": run.id})
            yield f"event: message_start\ndata: {json.dumps({'run_id': run.id, 'message_id': user_message.id}, ensure_ascii=False)}\n\n"
            if use_materials and not thread.course_id:
                raise RuntimeErrorWithStatus("只有课程对话可以参考课程资料", 400)
            if use_materials:
                try:
                    hits = await search(thread.course_id, content, "hybrid", 4)
                except RuntimeErrorWithStatus:
                    raise
                if not hits:
                    await record_trace("retrieval", {"query": content, "top_k": 4, "returned_chunks": [], "llm_called": False})
                    assistant = await sync_to_async(AgentMessage.objects.create)(thread=thread, role="assistant", content=NO_HIT, citations=[])
                    run.status = AgentRun.Status.COMPLETED
                    run.finished_at = timezone.now()
                    await sync_to_async(run.save)(update_fields=["status", "finished_at"])
                    await sync_to_async(event)(run, "delta", {"text": NO_HIT})
                    await sync_to_async(event)(run, "message_end", {"message_id": assistant.id, "citations": []})
                    yield f"event: delta\ndata: {json.dumps({'text': NO_HIT}, ensure_ascii=False)}\n\n"
                    yield f"event: message_end\ndata: {json.dumps({'message_id': assistant.id, 'citations': []}, ensure_ascii=False)}\n\n"
                    return
                citations = hits
                await record_trace("retrieval", {"query": content, "top_k": 4, "returned_chunks": hits, "whole_knowledge_base_sent": False})
                for citation in citations:
                    await sync_to_async(event)(run, "citation", citation)
                    yield f"event: citation\ndata: {json.dumps(citation, ensure_ascii=False)}\n\n"
            history = await sync_to_async(list)(thread.messages.order_by("created_at").values("role", "content"))
            system = "你是 CampusClaw 课程垂域 Agent。回答必须清晰、简洁；如果提供了资料，只能依据资料回答并使用 [1]、[2] 标注引用。你可以调用已声明的工具；需要生成 PDF 或 DOCX 时应调用对应工具，不能声称没有工具。"
            system += "需要交付 HTML 页面时，请输出包含 <html> 和 </html> 的完整文档，推荐使用 html 围栏代码块；CSS 和交互脚本放在文档中，并提供简短 title。系统会自动将 HTML 保存为可预览下载的文件。"
            system += "\n当前应用：" + app["prompt"]
            if config and config.template:
                template = config.template.replace("{{course}}", thread.course.name if thread.course else "").replace("{{question}}", content)
                system += "\n用户提示词模板：\n" + template
            if selected_skills:
                system += "\n已绑定技能（仅摘要）：\n" + "\n".join(f"{s['id']}: {s['name']} — {s['description']}" for s in selected_skills)
                system += "\n技能脚本在临时 Linux 容器中执行，工作目录包含技能参考文件与 scripts；可使用 Python、Node、Shell。生成文件请保存到 outputs/，会自动保存为可下载附件。Python Playwright 使用 executable_path='/usr/bin/chromium' 并带 --no-sandbox；脚本默认无网络，公网访问请调用已绑定的 browser_open。\n执行适合的技能前必须调用 read_skill 获取完整指令；参考文件用 read_skill_resource 按需读取。不得声称已执行未加载的技能。"
            if unavailable_refs:
                system += "\n以下工具当前不可用，请如实告知，不要编造结果：" + ", ".join(unavailable_refs)
            if citations:
                system += "\n课程资料依据:\n" + "\n".join(f"[{i}] {hit['title']} #{hit['chunk_index']}: {hit['excerpt']}" for i, hit in enumerate(citations, 1))
            messages = [{"role": "system", "content": system}]
            messages.extend({"role": row["role"], "content": row["content"]} for row in history if row["role"] in {"user", "assistant"})
            if attachments:
                multimodal = [{"type": "text", "text": content}]
                for attachment in attachments:
                    part = await asyncio.to_thread(attachment_prompt, attachment)
                    multimodal.extend(part if isinstance(part, list) else [part])
                messages[-1]["content"] = multimodal
            registry = builtin_tool_registry()
            permissions = {"artifact.create"}
            if thread.course_id:
                permissions.add("course.read")
            context = ToolContext(user, thread, frozenset(permissions))
            descriptions = configure_registry(registry, selected_skills, mcp_tools)
            if selected_skills:
                permissions.add("skill.read")
            if mcp_tools:
                permissions.add("mcp.call")
            context = ToolContext(user, thread, frozenset(permissions))
            tools = model_tools(registry, permissions)
            for tool in tools:
                name = tool["function"]["name"]
                if name in descriptions:
                    tool["function"]["description"] = descriptions[name]
                elif name == "read_skill":
                    tool["function"]["description"] = "执行技能前读取完整 SKILL.md 指令。绑定只提供摘要，使用前必须加载。"
                elif name == "read_skill_resource":
                    tool["function"]["description"] = "按需读取已绑定技能的指定参考文件。"
            # Unbound retrieval must not silently read the course knowledge base.
            if not use_materials and not any("search_course_knowledge" in sk["tool_refs"] for sk in selected_skills):
                tools = [t for t in tools if t["function"]["name"] != "search_course_knowledge"]
            allowed_names = {t["function"]["name"] for t in tools}
            total_calls = 0
            for round_number in range(1, 7):
                if round_number == 6 or total_calls >= 12:
                    tools = []
                    messages.append({"role": "system", "content": "已达到工具调用预算。请根据已有证据完成回答，明确未完成部分，不再调用工具。"})
                pending_calls = []
                round_text = []
                async for delta, model in model_stream(messages, attachments, tools):
                    state = await sync_to_async(lambda: AgentRun.objects.filter(pk=run.pk).values_list("status", flat=True).first())()
                    if state == AgentRun.Status.CANCELLED:
                        raise asyncio.CancelledError
                    model_name = model or model_name
                    if isinstance(delta, dict):
                        pending_calls = delta.get("tool_calls") or []
                        continue
                    if delta:
                        answer_parts.append(delta)
                        round_text.append(delta)
                        await sync_to_async(event)(run, "delta", {"text": delta})
                        yield f"event: delta\ndata: {json.dumps({'text': delta}, ensure_ascii=False)}\n\n"
                if not pending_calls:
                    break
                messages.append({"role": "assistant", "content": "".join(round_text), "tool_calls": pending_calls})
                for call in pending_calls:
                    name = call.get("function", {}).get("name", "")
                    parse_error = None
                    try:
                        arguments = json.loads(call.get("function", {}).get("arguments") or "{}")
                    except json.JSONDecodeError:
                        arguments = call.get("function", {}).get("arguments")
                        parse_error = "模型工具参数不是有效 JSON"
                    call_payload = {"name": name, "arguments": arguments, "round": round_number, "call_id": call.get("id")}
                    total_calls += 1
                    await sync_to_async(event)(run, "tool_call", call_payload)
                    yield f"event: tool_call\ndata: {json.dumps(call_payload, ensure_ascii=False)}\n\n"
                    import time
                    started = time.monotonic()
                    try:
                        if parse_error:
                            raise ValueError(parse_error)
                        if name not in allowed_names or not tools or total_calls > 12:
                            raise ValueError("工具不在本次候选集或已达到调用上限")
                        result = await asyncio.wait_for(registry.execute(name, arguments, context), timeout=45)
                        status = "error" if isinstance(result, dict) and result.get("isError") else "ok"
                    except Exception as exc:
                        result = {"isError": True, "error": str(exc)[:500]}
                        status = "rejected" if isinstance(exc, (ValueError, PermissionError, KeyError)) else "error"
                    result_payload = {"name": name, "result": result, "round": round_number, "status": status,
                                      "duration_ms": round((time.monotonic() - started) * 1000)}
                    await sync_to_async(event)(run, "tool_result", result_payload)
                    await sync_to_async(AgentMessage.objects.create)(thread=thread, role="tool", content=json.dumps(result, ensure_ascii=False), metadata={"name": name, "run_id": run.id})
                    yield f"event: tool_result\ndata: {json.dumps(result_payload, ensure_ascii=False)}\n\n"
                    if isinstance(result, dict):
                        for artifact_id in result.get("_artifact_ids", []):
                            image = await sync_to_async(lambda aid=artifact_id: AgentArtifact.objects.get(pk=aid, owner=user))()
                            image_payload = _artifact_data(image)
                            artifacts.append(image_payload)
                            await sync_to_async(event)(run, "artifact", image_payload)
                            yield f"event: artifact\ndata: {json.dumps(image_payload, ensure_ascii=False)}\n\n"
                    if status == "ok" and name in {"create_pdf", "create_docx"}:
                        artifact = await sync_to_async(lambda: AgentArtifact.objects.get(pk=result["id"]))()
                        payload = _artifact_data(artifact)
                        artifacts.append(payload)
                        await sync_to_async(event)(run, "artifact", payload)
                        yield f"event: artifact\ndata: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"
                    messages.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(result, ensure_ascii=False)})
            else:
                answer_parts.append("\n本次工具调用已达到上限。以上仅包含已取得的结果，尚未完成的步骤可在下一条消息继续。")
            answer = "".join(answer_parts)
            assistant, html_files = await sync_to_async(_finish_answer)(user, thread, run, answer, citations, model_name, artifacts)
            completed = True
            for payload in html_files:
                await sync_to_async(event)(run, "artifact", payload)
                yield f"event: artifact\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            await sync_to_async(event)(run, "message_end", {"message_id": assistant.id, "citations": citations})
            yield f"event: message_end\ndata: {json.dumps({'message_id': assistant.id, 'citations': citations}, ensure_ascii=False)}\n\n"
        except asyncio.CancelledError:
            if completed:
                raise
            partial = "".join(answer_parts)
            if partial:
                await sync_to_async(AgentMessage.objects.create)(thread=thread, role="assistant", content=partial,
                    citations=citations, metadata={"partial": True, "run_id": run.id, "model": model_name})
            run.status = AgentRun.Status.CANCELLED
            run.finished_at = timezone.now()
            await sync_to_async(run.save)(update_fields=["status", "finished_at"])
            raise
        except Exception as exc:
            if completed:
                raise
            partial = "".join(answer_parts)
            if partial:
                await sync_to_async(AgentMessage.objects.create)(thread=thread, role="assistant", content=partial,
                    citations=citations, metadata={"partial": True, "run_id": run.id, "model": model_name})
            run.status = AgentRun.Status.FAILED
            run.error_message = str(exc)[:2000]
            run.finished_at = timezone.now()
            await sync_to_async(run.save)(update_fields=["status", "error_message", "finished_at"])
            await sync_to_async(event)(run, "error", {"error": str(exc)})
            yield f"event: error\ndata: {json.dumps({'error': str(exc)}, ensure_ascii=False)}\n\n"
        finally:
            # The async generator uses a thread-sensitive Django connection.
            # Close it when a long-lived stream ends so cancelled/test streams
            # do not leave a PostgreSQL session behind.
            trace_sink.reset(trace_token)
            await sync_to_async(_close_db_connection)()

    response = StreamingHttpResponse(stream(), content_type="text/event-stream; charset=utf-8")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response


@agent_auth_required
def cancel_run(request, run_id):
    if request.method != "POST":
        return HttpResponse(status=405)
    run = AgentRun.objects.filter(id=run_id, thread__owner=request.user, status=AgentRun.Status.RUNNING).first()
    if not run:
        return JsonResponse({"error": "运行不存在或已结束"}, status=404)
    changed = AgentRun.objects.filter(pk=run.pk, status=AgentRun.Status.RUNNING).update(
        status=AgentRun.Status.CANCELLED, finished_at=timezone.now())
    if not changed:
        return JsonResponse({"error": "运行已结束"}, status=404)
    return JsonResponse({"ok": True})


def _accessible_artifact(request, artifact_id):
    artifact = AgentArtifact.objects.filter(id=artifact_id, owner=request.user).first()
    if artifact and _accessible_thread(request.user, artifact.thread_id):
        return artifact
    return None


@agent_auth_required
def artifact_source(request, artifact_id):
    artifact = _accessible_artifact(request, artifact_id)
    if not artifact or artifact.mime_type != "text/html" or not artifact.file.storage.exists(artifact.file.name):
        return JsonResponse({"error": "HTML 文件不存在"}, status=404)
    with artifact.file.open("rb") as source:
        response = HttpResponse(source.read(), content_type="text/plain; charset=utf-8")
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "no-store"
    return response


@agent_auth_required
def artifact_download(request, artifact_id):
    artifact = _accessible_artifact(request, artifact_id)
    if not artifact or not artifact.file.storage.exists(artifact.file.name):
        return JsonResponse({"error": "文件不存在"}, status=404)
    return FileResponse(artifact.file.open("rb"), as_attachment=True, filename=artifact.name, content_type=artifact.mime_type)


@agent_auth_required
def artifact_preview(request, artifact_id):
    artifact = _accessible_artifact(request, artifact_id)
    if not artifact:
        return JsonResponse({"error": "文件不存在"}, status=404)
    if not artifact.preview_html and artifact.file.storage.exists(artifact.file.name):
        response = FileResponse(artifact.file.open("rb"), filename=artifact.name, content_type=artifact.mime_type)
        response["Content-Security-Policy"] = "sandbox; default-src 'none'"
        response["Cache-Control"] = "no-store"
        return response
    response = HttpResponse(artifact.preview_html or "暂无预览", content_type="text/html; charset=utf-8")
    response["Content-Security-Policy"] = "sandbox allow-scripts; object-src 'none'; frame-src 'none'; connect-src 'none'; form-action 'none'"
    response["Cache-Control"] = "no-store"
    return response


@agent_auth_required
def create_artifact_view(request, thread_id):
    if request.method != "POST":
        return HttpResponse(status=405)
    thread = _accessible_thread(request.user, thread_id)
    if not thread:
        return JsonResponse({"error": "对话不存在"}, status=404)
    data = _json(request)
    kind = str(data.get("kind") or "pdf").lower()
    if kind not in {"pdf", "docx"}:
        return JsonResponse({"error": "仅支持 PDF 和 DOCX"}, status=400)
    try:
        artifact = create_artifact(request.user, thread, data.get("title"), str(data.get("content") or ""), kind)
    except Exception as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    return JsonResponse(_artifact_data(artifact), status=201)


@agent_auth_required
def tools(request):
    registry = builtin_tool_registry()
    return JsonResponse([{**spec.__dict__} for spec in registry.describe()], safe=False)


@agent_auth_required
def execute_tool(request, tool_name):
    if request.method != "POST":
        return HttpResponse(status=405)
    data = _json(request)
    thread = _accessible_thread(request.user, data.get("thread_id"))
    if not thread:
        return JsonResponse({"error": "对话不存在"}, status=404)
    permissions = {"artifact.create"}
    if thread.course_id and require_course_member(request.user, thread.course_id):
        permissions.add("course.read")
    context = ToolContext(request.user, thread, frozenset(permissions))
    try:
        result = async_to_sync(builtin_tool_registry().execute)(tool_name, data.get("arguments") or {}, context)
    except KeyError:
        return JsonResponse({"error": "工具不存在"}, status=404)
    except (PermissionError, ValueError, RuntimeErrorWithStatus) as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    return JsonResponse(result, safe=not isinstance(result, list))
