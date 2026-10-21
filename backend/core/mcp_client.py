"""MCP Streamable HTTP client. Tokens stay in environment variables."""
import json
import os
import re
from urllib.parse import urlsplit
import httpx
from jsonschema import Draft202012Validator

PROTOCOL = "2025-03-26"


def validate_url(url):
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("请输入不含凭据、查询参数的 HTTP(S) MCP 地址")
    allowed = {h.strip() for h in os.getenv("MCP_ALLOWED_HOSTS", "campus-tools,localhost,127.0.0.1").split(",")}
    if parsed.hostname not in allowed:
        raise ValueError("服务器不在 MCP_ALLOWED_HOSTS 允许列表中，请由服务器管理员配置")


async def rpc(server, method, params=None, notification=False, session=None):
    validate_url(server.url)
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": PROTOCOL}
    token = os.getenv(server.token_env, "") if server.token_env else ""
    if server.token_env and not token:
        raise ValueError("服务器令牌环境变量尚未配置")
    if token:
        headers["Authorization"] = "Bearer " + token
    sid = session if session is not None else server.session_id
    if sid:
        headers["Mcp-Session-Id"] = sid
    body = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if not notification:
        body["id"] = 1
    async with httpx.AsyncClient(timeout=55 if method == "tools/call" else 15, follow_redirects=False, trust_env=False) as client:
        async with client.stream("POST", server.url, headers=headers, json=body) as response:
            if response.status_code >= 400:
                raise ValueError(f"MCP HTTP {response.status_code}，请检查地址、令牌与服务状态")
            session_id = response.headers.get("Mcp-Session-Id", sid)
            if notification:
                return {}, session_id
            raw = bytearray()
            if "text/event-stream" in response.headers.get("content-type", ""):
                data_lines = []
                async for line in response.aiter_lines():
                    if line.startswith("data:"):
                        data_lines.append(line[5:].lstrip())
                    elif not line and data_lines:
                        data = json.loads("\n".join(data_lines)); data_lines = []
                        if data.get("id") == 1:
                            break
                    if sum(map(len, data_lines)) > 3145728:
                        raise ValueError("MCP 返回超出 3 MB 限制")
                else:
                    raise ValueError("MCP SSE 未返回匹配响应")
            else:
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 3145728:
                        raise ValueError("MCP 返回超出 3 MB 限制")
                data = json.loads(raw)
            if data.get("error"):
                raise ValueError("MCP 协议错误，代码 " + str(data["error"].get("code", "unknown")))
            if data.get("id") != 1 or "result" not in data:
                raise ValueError("MCP 响应格式不正确")
            return data["result"], session_id


async def discover(server):
    result, session = await rpc(server, "initialize", {"protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": {"name": "CampusClaw", "version": "1.0"}}, session="")
    if result.get("protocolVersion") != PROTOCOL:
        raise ValueError("MCP 协议版本不兼容")
    await rpc(server, "notifications/initialized", notification=True, session=session)
    tools = []
    cursor = None
    for _ in range(10):
        result, _ = await rpc(server, "tools/list", {"cursor": cursor} if cursor else {}, session=session)
        for tool in result.get("tools", []):
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,59}", tool.get("name", "")):
                continue
            schema = tool.get("inputSchema", {"type": "object"})
            Draft202012Validator.check_schema(schema)
            if len(tools) >= 100:
                raise ValueError("MCP 工具数量超出限制")
            if any(t["name"] == tool["name"] for t in tools):
                raise ValueError("MCP 工具名称重复")
            tools.append({"name": tool["name"], "description": str(tool.get("description", ""))[:2000], "inputSchema": schema})
        cursor = result.get("nextCursor")
        if not cursor:
            return tools, session or ""
    raise ValueError("MCP 工具分页超出限制")


def public_server(server):
    return {"id": server.id, "name": server.name, "url": server.url, "has_token": bool(server.token_env and os.getenv(server.token_env)),
            "connected": server.connected, "tools": server.tools, "error": server.error}
