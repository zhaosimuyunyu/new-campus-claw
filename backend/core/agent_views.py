import asyncio
from functools import wraps
import json
import mimetypes
import os
from asgiref.sync import async_to_sync
from asgiref.sync import sync_to_async
from django.db.models import Q
from django.http import FileResponse, HttpResponse, JsonResponse, StreamingHttpResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_protect

from .agent_runtime import (RuntimeErrorWithStatus, ToolContext, attachment_prompt, builtin_tool_registry,
                            create_artifact, event, index_material, model_stream, require_course_member, search)
from .models import AgentArtifact, AgentAttachment, AgentMessage, AgentRun, AgentThread, KnowledgeEntry, Material


NO_HIT = "资料中未找到相关内容"


def _close_db_connection():
    from django.db import connection
    connection.close()


def agent_auth_required(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse({"error": "请先登录"}, status=401)
        return view(request, *args, **kwargs)
    return wrapped


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


def _artifact_data(item):
    return {"id": item.id, "name": item.name, "mime_type": item.mime_type,
            "download_url": f"/api/agent/artifacts/{item.id}/download/",
            "preview_url": f"/api/agent/artifacts/{item.id}/preview/", "created_at": item.created_at}


def _accessible_thread(user, thread_id):
    thread = AgentThread.objects.filter(id=thread_id, owner=user).select_related("course").first()
    if not thread:
        return None
    if thread.course_id and not require_course_member(user, thread.course_id):
        return None
    return thread


@csrf_protect
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


@csrf_protect
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


@csrf_protect
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
    use_materials = bool(data.get("use_course_materials", False))
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
        model_name = run.model
        try:
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
                for citation in citations:
                    await sync_to_async(event)(run, "citation", citation)
                    yield f"event: citation\ndata: {json.dumps(citation, ensure_ascii=False)}\n\n"
            history = await sync_to_async(list)(thread.messages.order_by("created_at").values("role", "content"))
            system = "你是 CampusClaw 课程垂域 Agent。回答必须清晰、简洁；如果提供了资料，只能依据资料回答并使用 [1]、[2] 标注引用。"
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
            async for delta, model in model_stream(messages, attachments):
                if not delta:
                    continue
                state = await sync_to_async(lambda: AgentRun.objects.filter(pk=run.pk).values_list("status", flat=True).first())()
                if state == AgentRun.Status.CANCELLED:
                    raise asyncio.CancelledError
                model_name = model or model_name
                answer_parts.append(delta)
                await sync_to_async(event)(run, "delta", {"text": delta})
                yield f"event: delta\ndata: {json.dumps({'text': delta}, ensure_ascii=False)}\n\n"
            answer = "".join(answer_parts)
            assistant = await sync_to_async(AgentMessage.objects.create)(thread=thread, role="assistant", content=answer,
                citations=citations, metadata={"model": model_name})
            run.status = AgentRun.Status.COMPLETED
            run.finished_at = timezone.now()
            await sync_to_async(run.save)(update_fields=["status", "finished_at"])
            await sync_to_async(event)(run, "message_end", {"message_id": assistant.id, "citations": citations})
            yield f"event: message_end\ndata: {json.dumps({'message_id': assistant.id, 'citations': citations}, ensure_ascii=False)}\n\n"
        except asyncio.CancelledError:
            partial = "".join(answer_parts)
            if partial:
                await sync_to_async(AgentMessage.objects.create)(thread=thread, role="assistant", content=partial,
                    citations=citations, metadata={"partial": True, "run_id": run.id, "model": model_name})
            run.status = AgentRun.Status.CANCELLED
            run.finished_at = timezone.now()
            await sync_to_async(run.save)(update_fields=["status", "finished_at"])
            raise
        except Exception as exc:
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
            await sync_to_async(_close_db_connection)()

    response = StreamingHttpResponse(stream(), content_type="text/event-stream; charset=utf-8")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response


@csrf_protect
@agent_auth_required
def cancel_run(request, run_id):
    if request.method != "POST":
        return HttpResponse(status=405)
    run = AgentRun.objects.filter(id=run_id, thread__owner=request.user, status=AgentRun.Status.RUNNING).first()
    if not run:
        return JsonResponse({"error": "运行不存在或已结束"}, status=404)
    run.status = AgentRun.Status.CANCELLED
    run.finished_at = timezone.now()
    run.save(update_fields=["status", "finished_at"])
    return JsonResponse({"ok": True})


@agent_auth_required
def artifact_download(request, artifact_id):
    artifact = AgentArtifact.objects.filter(id=artifact_id, owner=request.user).first()
    if not artifact or not artifact.file.storage.exists(artifact.file.name):
        return JsonResponse({"error": "文件不存在"}, status=404)
    return FileResponse(artifact.file.open("rb"), as_attachment=True, filename=artifact.name, content_type=artifact.mime_type)


@agent_auth_required
def artifact_preview(request, artifact_id):
    artifact = AgentArtifact.objects.filter(id=artifact_id, owner=request.user).first()
    if not artifact:
        return JsonResponse({"error": "文件不存在"}, status=404)
    return HttpResponse(artifact.preview_html or "暂无预览", content_type="text/html; charset=utf-8")


@csrf_protect
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


@csrf_protect
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
