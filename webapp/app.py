"""Web 对话服务：FastAPI + WebSocket，自带 GOLEM MCP 客户端。

    python -m webapp.app
    # 浏览器打开 http://127.0.0.1:8849

事件协议（JSON）：
    客户端 → 服务端: {"text": "建一个球"}  或  {"action": "reset"}
    服务端 → 客户端: {"type": "tool_call"|"tool_result"|"assistant"|"image"|"error"|"info"|"done", ...}
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

from .agent import WebSession
from .config import CFG, ENV_FILE
from .golem_client import GolemClient

log = logging.getLogger("golem.web.app")

STATIC_DIR = Path(__file__).resolve().parent / "static"
PLUGIN_LAUNCHER = CFG.PROJECT_ROOT / "start_in_rhino.py"

mcp = GolemClient()
_run_lock = asyncio.Lock()

# 状态有 TTL 缓存：页面每 4 秒轮询，不能每次都去敲 Rhino。
# online 为 True 时 15 秒刷新一次，为 False 时 5 秒重试一次。
_info_cache: dict = {"at": 0.0, "data": None, "online": False, "error": ""}
_INFO_TTL = 15.0
_INFO_TTL_FAIL = 5.0

@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        await mcp.start()
    except Exception as exc:  # noqa: BLE001
        log.warning("GOLEM MCP 启动失败：%s: %s", type(exc).__name__, exc)
    yield
    await mcp.stop()


app = FastAPI(title="GOLEM Rhino Agent", lifespan=lifespan)


async def ensure_mcp() -> bool:
    if mcp.ready:
        return True
    try:
        await mcp.start()
    except Exception as exc:  # noqa: BLE001
        log.warning("GOLEM MCP 连接失败：%s: %s", type(exc).__name__, exc)
    return mcp.ready


def port_open(host: str, port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(1.5)
    try:
        return sock.connect_ex((host, port)) == 0
    finally:
        sock.close()

@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/status")
async def status():
    """Rhino 探活 + 文档摘要，供页面顶部状态灯使用。

    刻意不做裸 TCP 探活：那会让 Rhino 命令行每 4 秒刷一对
    connect/disconnect 日志。这里直接用 get_document_info 当探活，
    并带 TTL 缓存（在线 15s / 离线 5s）。
    """
    now = time.monotonic()
    ttl = _INFO_TTL if _info_cache["online"] else _INFO_TTL_FAIL
    if now - _info_cache["at"] > ttl and await ensure_mcp():
        try:
            ok, text, _img, _mime = await mcp.call("get_document_info", {}, timeout=15)
            _info_cache["online"] = bool(ok)
            _info_cache["data"] = json.loads(text) if ok else None
            _info_cache["error"] = "" if ok else (text or "")[:300]
        except Exception as exc:  # noqa: BLE001
            _info_cache["online"] = False
            _info_cache["data"] = None
            _info_cache["error"] = "%s: %s" % (type(exc).__name__, exc)
        _info_cache["at"] = now

    return JSONResponse({
        "online": _info_cache["online"],
        "port": CFG.RHINO_PORT,
        "mcp_ready": mcp.ready,
        "mcp_error": _info_cache["error"] or mcp.last_error,
        "tool_count": len(mcp.tools),
        "info": _info_cache["data"],
        "env_file": str(ENV_FILE) if ENV_FILE else None,
        "api_key": bool(CFG.API_KEY),
        "model": CFG.MODEL,
    })


@app.post("/api/undo")
async def undo():
    """页面上的撤销：GOLEM 没有专门的 undo 工具，走 Rhino 的 _Undo 命令。"""
    if not await ensure_mcp():
        return JSONResponse({"ok": False, "message": "GOLEM MCP 未连接：%s" % (mcp.last_error or "见启动日志")})
    try:
        ok, text, _img, _mime = await mcp.call("run_rhino_command", {"command": "_Undo"}, timeout=30)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "message": "%s: %s" % (type(exc).__name__, exc)})
    return JSONResponse({"ok": ok, "data": text})

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    ready = await ensure_mcp()
    if not ready:
        await ws.send_json({"type": "error", "message": "GOLEM MCP 未连接：%s" % (mcp.last_error or "见服务端日志")})
    session = WebSession(mcp)
    await ws.send_json({
        "type": "info",
        "message": "会话已建立 · 模型 %s · 工具 %d 个" % (CFG.MODEL, len(session.tools)),
    })

    async def emit(event: dict) -> None:
        try:
            await ws.send_json(event)
        except Exception:  # noqa: BLE001
            pass

    try:
        while True:
            payload = await ws.receive_json()
            if payload.get("action") == "reset":
                session.reset()
                await ws.send_json({"type": "info", "message": "上下文已清空"})
                continue

            text = (payload.get("text") or "").strip()
            if not text:
                continue
            if _run_lock.locked():
                await ws.send_json({"type": "error", "message": "上一轮还在进行中，请稍候"})
                continue

            async with _run_lock:
                try:
                    await session.run(text, emit)
                except Exception as exc:  # noqa: BLE001
                    log.exception("agent run failed")
                    await ws.send_json({"type": "error", "message": "%s: %s" % (type(exc).__name__, exc)})
    except WebSocketDisconnect:
        pass


def banner() -> None:
    print("=" * 66)
    print("GOLEM Rhino Agent  →  http://%s:%s" % (CFG.HOST, CFG.PORT))
    print("环境文件 : %s" % (ENV_FILE or "未找到"))
    print("API Key  : %s" % ("已配置" if CFG.API_KEY else "未配置（在 .env 里填 DEEPSEEK_API_KEY）"))
    print("模型     : %s @ %s" % (CFG.MODEL, CFG.BASE_URL))
    print("Rhino 桥 : %s:%s（GOLEM 插件，不是旧的 9877）" % (CFG.RHINO_HOST, CFG.RHINO_PORT))
    if port_open(CFG.RHINO_HOST, CFG.RHINO_PORT):
        print("插件状态 : 已监听")
    else:
        print("插件状态 : 未监听，请在 Rhino 8 的 ScriptEditor 里运行：")
        print("           %s" % PLUGIN_LAUNCHER)
    print("=" * 66)


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    banner()
    uvicorn.run(app, host=CFG.HOST, port=CFG.PORT, log_level="warning")


if __name__ == "__main__":
    main()
