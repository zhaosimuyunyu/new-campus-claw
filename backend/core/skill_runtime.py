import json
import re
from io import BytesIO
from pathlib import PurePosixPath
from zipfile import ZipFile, BadZipFile
from asgiref.sync import sync_to_async
from django.db.models import Q
from .models import UserSkill, AssistantConfig, MCPServer
from .skill_catalog import APPS, BUILTIN_SKILLS
from .agent_runtime import ToolSpec
from .mcp_client import rpc


def skill_list(user):
    uploaded = [{"id": f"upload-{s.id}", "name": s.name, "description": s.description, "instructions": s.instructions,
                 "resources": s.resources, "tool_refs": s.tool_refs, "category": "我的上传", "icon": "↑", "builtin": False,
                 "source": "个人上传", "version": "1.0.0"} for s in UserSkill.objects.filter(owner=user)]
    return [*BUILTIN_SKILLS, *uploaded]


def skill_summary(skill):
    return {k: v for k, v in skill.items() if k not in {"instructions", "resources"}} | {"resource_names": list(skill["resources"])}


def parse_upload(upload):
    if upload.size > 2 * 1024 * 1024:
        raise ValueError("技能文件不能超过 2 MB")
    resources = {}
    if upload.name.lower().endswith(".zip"):
        try:
            with ZipFile(BytesIO(upload.read())) as archive:
                infos = [i for i in archive.infolist() if not i.is_dir()]
                if len(infos) > 40 or sum(i.file_size for i in infos) > 2 * 1024 * 1024:
                    raise ValueError("ZIP 最多包含 40 个文件，解压后不能超过 2 MB")
                paths = [PurePosixPath(i.filename) for i in infos]
                if any(p.is_absolute() or ".." in p.parts or "\\" in str(p) or ":" in str(p) for p in paths):
                    raise ValueError("ZIP 包含不安全路径")
                mains = [p for p in paths if p.name.lower() == "skill.md"]
                if len(mains) != 1:
                    raise ValueError("ZIP 必须有且仅有一个 SKILL.md")
                main = mains[0]
                body = archive.read(str(main)).decode("utf-8-sig")
                for p in paths:
                    if p == main:
                        continue
                    if p.suffix.lower() not in {".md", ".txt", ".json", ".csv", ".yaml", ".yml", ".py", ".js", ".sh"}:
                        raise ValueError("技能包支持文本参考与 Python、JavaScript、Shell 脚本")
                    if not p.is_relative_to(main.parent):
                        raise ValueError("参考文件必须位于 SKILL.md 所在目录内")
                    resources[str(p.relative_to(main.parent))] = archive.read(str(p)).decode("utf-8-sig")
        except (BadZipFile, UnicodeError, RuntimeError) as exc:
            raise ValueError("无法读取 UTF-8 技能包") from exc
    elif upload.name.lower().endswith(".md"):
        try:
            body = upload.read().decode("utf-8-sig")
        except UnicodeError as exc:
            raise ValueError("SKILL.md 必须使用 UTF-8 编码") from exc
    else:
        raise ValueError("请上传 SKILL.md 或 ZIP 技能包")
    if len(body) > 50000 or any(len(v) > 50000 for v in resources.values()):
        raise ValueError("单个技能文件不能超过 50000 字符")
    name, description, refs = "", "", []
    # Safe YAML front matter: no constructors, only metadata.
    if body.startswith("---"):
        import yaml
        parts = body.split("---", 2)
        if len(parts) < 3:
            raise ValueError("技能 YAML 头部缺少结束标记")
        try:
            meta = yaml.safe_load(parts[1]) or {}
        except yaml.YAMLError as exc:
            raise ValueError("技能 YAML 头部格式不正确") from exc
        if not isinstance(meta, dict):
            raise ValueError("技能头部必须是键值对象")
        name, description = str(meta.get("name", "")), str(meta.get("description", ""))
        refs = meta.get("tools", [])
        body = parts[2].strip()
    else:
        lines = body.strip().splitlines()
        name = lines[0].lstrip("# ") if lines else ""
        description = next((l.strip() for l in lines[1:] if l.strip()), "个人上传技能")
    if not name.strip() or not body.strip():
        raise ValueError("技能必须包含名称与正文指令")
    if not isinstance(refs, list) or any(not isinstance(r, str) for r in refs):
        raise ValueError("tools 必须是工具引用字符串列表")
    return name[:100], description[:500], body, resources, refs


def validate_refs(user, refs):
    if not isinstance(refs, list) or len(refs) > 20 or any(not isinstance(r, str) for r in refs):
        raise ValueError("工具引用最多 20 项")
    from .agent_runtime import builtin_tool_registry
    builtins = {s.name for s in builtin_tool_registry().describe()}
    for ref in refs:
        if ref in builtins:
            continue
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,59}@[a-z][a-z0-9-]{0,59}", ref):
            raise ValueError("工具引用格式应为 tool_name@server-name")
        name, server_name = ref.split("@")
        server = MCPServer.objects.filter(Q(owner=user) | Q(is_shared=True), name=server_name).first()
        if not server or not server.connected or name not in {t["name"] for t in server.tools}:
            raise ValueError("服务器不存在、未连接或工具尚未被发现")


def load_config(user, course_id):
    config, _ = AssistantConfig.objects.get_or_create(owner=user, course_id=course_id)
    return config



def save_execution_files(result, context):
    import base64, mimetypes
    from django.core.files.base import ContentFile
    from .models import AgentArtifact
    ids = []
    files = result.pop("files", [])
    for item in files[:8]:
        name = PurePosixPath(item["name"]).name[:200]
        raw = base64.b64decode(item["base64"], validate=True)
        if len(raw) > 900000:
            continue
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        artifact = AgentArtifact(owner=context.user, thread=context.thread, course_id=context.thread.course_id,
            name=name, mime_type=mime, preview_html=raw.decode("utf-8", "replace") if mime == "text/html" else "")
        artifact.file.save(name, ContentFile(raw), save=True)
        ids.append(artifact.id)
    if ids:
        result["_artifact_ids"] = ids
        result["artifacts"] = [{"id": i, "download_url": f"/api/agent/artifacts/{i}/download/"} for i in ids]
    return result

def configure_registry(registry, selected, servers):
    allowed_skills = {s["id"]: s for s in selected}
    async def read_skill(args, context):
        skill = allowed_skills.get(args["skill_id"])
        if not skill:
            raise ValueError("技能未绑定到本课程助手")
        return {"skill_id": skill["id"], "name": skill["name"], "instructions": skill["instructions"], "resources": list(skill["resources"])}
    async def read_resource(args, context):
        skill = allowed_skills.get(args["skill_id"])
        if not skill or args["path"] not in skill["resources"]:
            raise ValueError("参考文件不存在或技能未绑定")
        return {"path": args["path"], "content": skill["resources"][args["path"]]}
    if selected:
        registry.register(ToolSpec("read_skill", "1.0", {"type": "object", "properties": {"skill_id": {"type": "string", "enum": list(allowed_skills)}}, "required": ["skill_id"], "additionalProperties": False}, {"type": "object"}, "skill.read", False, False), read_skill)
        registry.register(ToolSpec("read_skill_resource", "1.0", {"type": "object", "properties": {"skill_id": {"type": "string", "enum": list(allowed_skills)}, "path": {"type": "string"}}, "required": ["skill_id", "path"], "additionalProperties": False}, {"type": "object"}, "skill.read", False, False), read_resource)
    async def run_script(args, context):
        skill = allowed_skills.get(args["skill_id"])
        path = args["path"]
        if not skill or path not in skill["resources"] or not path.startswith("scripts/"):
            raise ValueError("脚本不存在或未绑定技能")
        import httpx, os
        async with httpx.AsyncClient(timeout=45, trust_env=False) as client:
            response = await client.post(os.getenv("SANDBOX_URL", "http://sandbox-runner:8200") + "/execute",
                headers={"Authorization": "Bearer " + os.getenv("SANDBOX_TOKEN", "")},
                json={"files": skill["resources"], "entrypoint": path, "args": args.get("args", [])})
            if response.status_code >= 400:
                raise ValueError("隔离执行失败：" + response.text[:200])
            result = await sync_to_async(save_execution_files)(response.json(), context)
            result["isError"] = result["exit_code"] != 0
            return result
    if selected:
        registry.register(ToolSpec("run_skill_script", "1.0", {"type": "object", "properties": {
            "skill_id": {"type": "string", "enum": list(allowed_skills)}, "path": {"type": "string"},
            "args": {"type": "array", "items": {"type": "string"}, "maxItems": 20}}, "required": ["skill_id", "path"], "additionalProperties": False},
            {"type": "object"}, "skill.read", False, False), run_script)
    descriptions = {"run_skill_script": "在临时隔离容器中执行已绑定技能 scripts/ 下的 Python、JavaScript 或 Shell 脚本。参数用字符串数组传入。默认无网络，不可访问宿主文件，35 秒上限。"}
    for server, tool in servers:
        name = f"mcp_{server.id}_{tool['name']}"
        def bind(server_id, tool_name):
            async def execute(args, context):
                current = await sync_to_async(lambda: MCPServer.objects.filter(Q(owner=context.user) | Q(is_shared=True), id=server_id).first())()
                if not current or not current.connected or tool_name not in {t["name"] for t in current.tools}:
                    raise ValueError("MCP 服务器已断开或工具不再可用")
                result, _ = await rpc(current, "tools/call", {"name": tool_name, "arguments": args})
                artifact_ids = []
                # Persist browser screenshots as protected conversation artifacts.
                import base64, json
                if isinstance(result, dict):
                    for block in result.get("content", []):
                        if block.get("type") != "text":
                            continue
                        try:
                            data = json.loads(block["text"])
                        except (ValueError, TypeError):
                            continue
                        if isinstance(data, dict) and "files" in data:
                            data = await sync_to_async(save_execution_files)(data, context)
                            artifact_ids.extend(data.get("_artifact_ids", []))
                            if isinstance(result.get("structuredContent"), dict):
                                result["structuredContent"] = data
                            block["text"] = json.dumps(data, ensure_ascii=False)
                        if isinstance(data, dict) and data.get("screenshot_base64"):
                            from .models import AgentArtifact
                            from django.core.files.base import ContentFile
                            def save_screen():
                                image = AgentArtifact(owner=context.user, thread=context.thread, course_id=context.thread.course_id,
                                    name="browser-screenshot.png", mime_type="image/png")
                                image.file.save(image.name, ContentFile(base64.b64decode(data.pop("screenshot_base64"))), save=True)
                                return image
                            image = await sync_to_async(save_screen)()
                            data["screenshot_url"] = f"/api/agent/artifacts/{image.id}/download/"
                            artifact_ids.append(image.id)
                            if isinstance(result.get("structuredContent"), dict):
                                result["structuredContent"].pop("screenshot_base64", None)
                                result["structuredContent"]["screenshot_url"] = data["screenshot_url"]
                            block["text"] = json.dumps(data, ensure_ascii=False)
                if artifact_ids:
                    result["_artifact_ids"] = artifact_ids
                return result
            return execute
        registry.register(ToolSpec(name, "1.0", tool["inputSchema"], {"type": "object"}, "mcp.call", False, False), bind(server.id, tool["name"]))
        descriptions[name] = f"{tool['name']}@{server.name}: {tool['description']}"
    return descriptions


def runtime_config(user, course_id):
    config = load_config(user, course_id) if course_id else None
    selected = [s for s in skill_list(user) if config and s["id"] in config.skill_ids]
    app = next((a for a in APPS if config and a["id"] == config.app_id), APPS[0])
    refs = list(config.mcp_refs) if config else []
    for s in selected:
        refs.extend(s["tool_refs"])
    servers = []
    unavailable = []
    for ref in dict.fromkeys(refs):
        if "@" not in ref:
            continue
        name, server_name = ref.split("@", 1)
        server = MCPServer.objects.filter(Q(owner=user) | Q(is_shared=True), name=server_name).first()
        tool = next((t for t in server.tools if t["name"] == name), None) if server else None
        if server and server.connected and tool:
            servers.append((server, tool))
        else:
            unavailable.append(ref)
    return config, app, selected, servers, unavailable
