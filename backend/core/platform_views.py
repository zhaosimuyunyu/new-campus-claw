from asgiref.sync import async_to_sync
from django.http import JsonResponse, HttpResponse
from .agent_views import agent_auth_required, _json, _accessible_thread
from .agent_runtime import require_course_member
from .models import AssistantConfig, UserSkill, MCPServer, AgentRun
from .skill_catalog import APPS
from .skill_runtime import skill_list, skill_summary, parse_upload, validate_refs, load_config
from .mcp_client import discover, public_server, validate_url
import os
import re


def manager(user):
    return user.is_staff or user.can_create_course or user.memberships.filter(role="teacher").exists()


def config_data(c):
    return {"app_id": c.app_id, "template": c.template, "skill_ids": c.skill_ids, "mcp_refs": c.mcp_refs, "use_knowledge": c.use_knowledge}


@agent_auth_required
def catalog(request):
    if request.method != "GET":
        return HttpResponse(status=405)
    return JsonResponse({"apps": APPS, "skills": [skill_summary(s) for s in skill_list(request.user)], "can_manage_mcp": manager(request.user),
                         "provider": os.getenv("MODEL_PROVIDER", "mock"), "model": os.getenv("MODEL_NAME", "mock-local")})


@agent_auth_required
def skill_upload(request):
    if request.method != "POST":
        return HttpResponse(status=405)
    upload = request.FILES.get("file")
    if not upload:
        return JsonResponse({"error": "请选择技能文件"}, status=400)
    try:
        name, desc, body, resources, refs = parse_upload(upload)
        validate_refs(request.user, refs)
        item = UserSkill.objects.create(owner=request.user, slug="uploaded-skill", name=name, description=desc, instructions=body, resources=resources, tool_refs=refs)
        return JsonResponse({"id": f"upload-{item.id}"}, status=201)
    except (ValueError, KeyError, OSError) as exc:
        return JsonResponse({"error": str(exc)}, status=400)


@agent_auth_required
def skill_detail(request, skill_id):
    skill = next((s for s in skill_list(request.user) if s["id"] == skill_id), None)
    if not skill:
        return JsonResponse({"error": "技能不存在"}, status=404)
    if request.method == "DELETE":
        if skill["builtin"]:
            return JsonResponse({"error": "内置技能无法删除"}, status=400)
        UserSkill.objects.filter(owner=request.user, id=int(skill_id[7:])).delete()
        return JsonResponse({"ok": True})
    if request.method != "GET":
        return HttpResponse(status=405)
    # Keep asset bytes out of catalogue and detail responses.
    detail = {k: v for k, v in skill.items() if k != "binary_resources"}
    detail["binary_resource_names"] = list(skill.get("binary_resources", {}))
    return JsonResponse(detail)


@agent_auth_required
def assistant_config(request, course_id):
    if not require_course_member(request.user, course_id):
        return JsonResponse({"error": "课程不存在"}, status=404)
    c = load_config(request.user, course_id)
    if request.method == "GET":
        return JsonResponse(config_data(c))
    if request.method != "PUT":
        return HttpResponse(status=405)
    data = _json(request)
    try:
        if not isinstance(data, dict):
            raise ValueError("配置必须是 JSON 对象")
        app = data.get("app_id", c.app_id)
        if app not in {a["id"] for a in APPS}:
            raise ValueError("应用不存在")
        ids = data.get("skill_ids", c.skill_ids)
        available = {s["id"] for s in skill_list(request.user)}
        if not isinstance(ids, list) or len(ids) > 20 or any(not isinstance(i, str) or i not in available for i in ids):
            raise ValueError("技能不存在或超过 20 个")
        template = data.get("template", c.template)
        if not isinstance(template, str) or len(template) > 12000:
            raise ValueError("提示词模板不能超过 12000 字符")
        refs = data.get("mcp_refs", c.mcp_refs)
        if refs != c.mcp_refs:
            validate_refs(request.user, refs)
        c.app_id, c.template, c.skill_ids, c.mcp_refs = app, template, ids, refs
        c.use_knowledge = bool(data.get("use_knowledge", c.use_knowledge))
        c.save()
        return JsonResponse(config_data(c))
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)


@agent_auth_required
def servers(request, server_id=None):
    if not manager(request.user):
        return JsonResponse({"error": "仅管理角色可管理 MCP 服务"}, status=403)
    server = MCPServer.objects.filter(owner=request.user, id=server_id).first() if server_id else None
    if server_id and not server:
        return JsonResponse({"error": "服务器不存在"}, status=404)
    if request.method == "GET":
        return JsonResponse(public_server(server) if server else [public_server(s) for s in MCPServer.objects.filter(owner=request.user)], safe=bool(server))
    if request.method == "DELETE" and server:
        server.delete()
        return JsonResponse({"ok": True})
    if request.method != "POST":
        return HttpResponse(status=405)
    data = _json(request)
    try:
        if not isinstance(data, dict):
            raise ValueError("请输入 JSON 对象")
        if server:
            if data.get("action") == "disconnect":
                server.connected, server.tools, server.session_id, server.error = False, [], "", ""
            elif data.get("action") == "connect":
                try:
                    server.tools, server.session_id = async_to_sync(discover)(server)
                    server.connected, server.error = True, ""
                except Exception as exc:
                    server.connected, server.tools, server.session_id = False, [], ""
                    server.error = str(exc)[:300]
            else:
                raise ValueError("请选择 connect 或 disconnect")
            server.save()
            return JsonResponse(public_server(server))
        name, url, token_env = str(data.get("name", "")), str(data.get("url", "")), str(data.get("token_env", ""))
        if name == "platform-tools":
            raise ValueError("platform-tools 是平台保留名称")
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,59}", name):
            raise ValueError("服务器名须以小写字母开头，仅含小写字母、数字和连字符")
        if token_env and not re.fullmatch(r"MCP_[A-Z0-9_]+", token_env):
            raise ValueError("令牌变量名须以 MCP_ 开头，令牌原文只写在服务器 .env 中")
        validate_url(url)
        if MCPServer.objects.filter(owner=request.user, name=name).exists():
            raise ValueError("服务器名称已存在")
        server = MCPServer.objects.create(owner=request.user, name=name, url=url, token_env=token_env)
        return JsonResponse(public_server(server), status=201)
    except (ValueError, TypeError) as exc:
        return JsonResponse({"error": str(exc)}, status=400)


@agent_auth_required
def traces(request, thread_id):
    if request.method != "GET":
        return HttpResponse(status=405)
    thread = _accessible_thread(request.user, thread_id)
    if not thread:
        return JsonResponse({"error": "对话不存在"}, status=404)
    runs = []
    for run in thread.runs.order_by("-id")[:20]:
        runs.append({"id": run.id, "status": run.status, "model": run.model, "error": run.error_message,
                     "started_at": run.started_at, "finished_at": run.finished_at,
                     "events": list(run.events.exclude(event_type="delta").order_by("sequence").values("event_type", "payload", "sequence", "created_at"))})
    return JsonResponse(runs, safe=False)


@agent_auth_required
def tool_test(request, thread_id):
    if request.method != "POST":
        return HttpResponse(status=405)
    thread = _accessible_thread(request.user, thread_id)
    if not thread:
        return JsonResponse({"error": "对话不存在"}, status=404)
    from .skill_runtime import runtime_config, configure_registry
    from .agent_runtime import builtin_tool_registry, model_tools, ToolContext, event
    from django.utils import timezone
    import time, asyncio
    config, app, selected, mcp, unavailable = runtime_config(request.user, thread.course_id)
    registry = builtin_tool_registry()
    configure_registry(registry, selected, mcp)
    permissions = {"artifact.create", "skill.read"}
    if thread.course_id:
        permissions.add("course.read")
    if mcp:
        permissions.add("mcp.call")
    data = _json(request)
    name, arguments = data.get("name", ""), data.get("arguments", {})
    # Manual tests use the same candidate set as the conversation.
    allowed = {t["function"]["name"] for t in model_tools(registry, permissions)}
    if name not in allowed:
        return JsonResponse({"error": "请先将工具绑定到本课程助手"}, status=400)
    run = AgentRun.objects.create(thread=thread, model="manual-tool-test")
    event(run, "tool_call", {"name": name, "arguments": arguments, "round": 1, "manual": True})
    start = time.monotonic()
    try:
        async def execute():
            return await asyncio.wait_for(registry.execute(name, arguments, ToolContext(request.user, thread, frozenset(permissions))), 50)
        result = async_to_sync(execute)()
        status = "error" if isinstance(result, dict) and result.get("isError") else "ok"
        run.status = "completed" if status == "ok" else "failed"
    except Exception as exc:
        result, status = {"isError": True, "error": str(exc)[:500]}, "error"
        run.status, run.error_message = "failed", str(exc)[:500]
    payload = {"name": name, "result": result, "status": status, "round": 1, "duration_ms": round((time.monotonic()-start)*1000)}
    event(run, "tool_result", payload)
    run.finished_at = timezone.now()
    run.save()
    return JsonResponse({"run_id": run.id, **payload})


@agent_auth_required
def platform_tools(request):
    if request.method != "GET":
        return HttpResponse(status=405)
    return JsonResponse([{"id": s.id, "name": s.name, "connected": s.connected, "tools": s.tools}
                         for s in MCPServer.objects.filter(is_shared=True)], safe=False)
