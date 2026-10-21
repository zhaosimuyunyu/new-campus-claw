"""Local teaching MCP service, built with the official Python MCP SDK."""
import os
from mcp.server.fastmcp import FastMCP
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
import uvicorn

mcp = FastMCP("campus-tools", host="0.0.0.0", port=8100, stateless_http=True, json_response=True)


@mcp.tool()
def formula_hint(topic: str) -> str:
    """为代数、导数、积分或概率提供公式与解题提示，不代做整道作业。topic 是知识点或题目。"""
    if "导" in topic or "derivative" in topic.lower():
        return "导数提示：(x^n)' = n*x^(n-1)；复合函数使用链式法则。先识别外层和内层，再分别求导。"
    if "概率" in topic:
        return "概率提示：P(A|B)=P(A∩B)/P(B)，要求 P(B)>0。先明确样本空间与条件事件。"
    if "积分" in topic:
        return "积分提示：先判断是否适合换元或分部积分；不定积分别忘记积分常数 C。"
    return "代数提示：去括号时系数乘到每一项；合并同类项；方程两侧做相同运算，除法前确认除数不为零。"


@mcp.tool()
def unit_convert(value: float, from_unit: str, to_unit: str) -> dict:
    """精确换算长度(m/cm/km)、质量(kg/g)或时间(h/min/s)，仅支持同类单位。"""
    groups = [{"m": 1, "cm": .01, "km": 1000}, {"kg": 1, "g": .001}, {"h": 3600, "min": 60, "s": 1}]
    for group in groups:
        if from_unit in group and to_unit in group:
            return {"value": value * group[from_unit] / group[to_unit], "unit": to_unit}
    raise ValueError("单位不支持或不属于同一类别")


@mcp.tool()
def learning_checklist(subject: str) -> dict:
    """为指定课程主题生成学习检查清单，用于安排复习和验收准备。"""
    return {"subject": subject, "steps": ["能用自己的话解释核心概念", "能独立完成一个最小示例", "能解释失败情况及其原因", "保留可复现的操作步骤和证据"]}


async def sandbox_request(payload):
    import httpx
    async with httpx.AsyncClient(timeout=50, trust_env=False) as client:
        response = await client.post(os.getenv("SANDBOX_URL", "http://sandbox-runner:8200") + "/execute",
            headers={"Authorization": "Bearer " + os.getenv("SANDBOX_TOKEN", "")}, json=payload)
        response.raise_for_status()
        return response.json()


@mcp.tool()
async def python_execute(code: str) -> dict:
    """在隔离容器执行 Python 代码，适用于计算、统计或脚本验证。支持 pandas、matplotlib；无网络、无宿主文件访问，35 秒上限。返回 stdout、stderr 与退出码。"""
    if len(code) > 50000:
        raise ValueError("代码过长")
    return await sandbox_request({"files": {"main.py": code}, "entrypoint": "main.py"})


@mcp.tool()
async def shell_execute(command: str, files: dict[str, str] | None = None) -> dict:
    """在临时 Linux 工作目录执行 Shell 命令，可传入文本文件。支持 python、node 和常用系统命令；无网络，无宿主挂载，35 秒上限。仅 stdout/stderr 为执行证据。"""
    if len(command) > 20000 or (files and "main.sh" in files):
        raise ValueError("命令过长或文件名冲突")
    return await sandbox_request({"files": {**(files or {}), "main.sh": command}, "entrypoint": "main.sh"})


@mcp.tool()
async def browser_open(url: str) -> dict:
    """通过真实 Chromium 浏览器打开公网 HTTP(S) 网页，返回标题、正文、链接及页面截图。不支持登录、内部网络或下载。"""
    import json
    result = await sandbox_request({"files": {}, "entrypoint": "browser.py", "browser_url": url})
    if result["exit_code"]:
        raise ValueError(result["stderr"])
    return json.loads(result["stdout"])


class RequireToken(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        token = os.getenv("MCP_CAMPUS_TOKEN", "")
        import secrets
        if not token or not secrets.compare_digest(request.headers.get("authorization", ""), "Bearer " + token):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        return await call_next(request)


if __name__ == "__main__":
    app = mcp.streamable_http_app()
    app.add_middleware(RequireToken)
    uvicorn.run(app, host="0.0.0.0", port=8100)
