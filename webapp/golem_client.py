"""GOLEM MCP 客户端。

把 GOLEM 的 MCP server（`python -m golem_3dmcp start`）作为子进程拉起，
通过 stdio 复用官方 SDK 会话，从而直接拿到它暴露的全部工具 schema，
不必在本工程里手写 89 个工具定义。

调用链：
    本模块 → GOLEM MCP server 子进程 → TCP 127.0.0.1:9876 → Rhino 插件
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import json
import logging
import os
import sys
from contextlib import AsyncExitStack
from datetime import timedelta
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .config import CFG

log = logging.getLogger("golem.web.mcp")


def compress_png(image_b64: str, max_side: int) -> str:
    """把 base64 PNG 等比缩到最长边不超过 max_side，重新编码后返回 base64。"""
    from PIL import Image

    raw = base64.b64decode(image_b64)
    im = Image.open(io.BytesIO(raw)).convert("RGB")
    side = max(64, min(4096, int(max_side)))
    scale = min(1.0, side / float(max(im.size)))
    if scale < 1.0:
        im = im.resize((max(1, int(im.width * scale)), max(1, int(im.height * scale))))
    buf = io.BytesIO()
    im.save(buf, format="PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii")


_TRANSPORT_TYPES = (
    "BrokenPipeError",
    "ConnectionResetError",
    "ConnectionAbortedError",
    "ConnectionRefusedError",
    "TimeoutError",
    "BrokenResourceError",
    "ClosedResourceError",
    "EndOfStream",
)

_TRANSPORT_HINTS = (
    "10052",
    "10053",
    "10054",
    "10061",
    "Cannot connect to Rhino plugin",
    "Not connected to Rhino plugin",
    "aborted",
    "reset by peer",
    "Broken pipe",
    "Connection reset",
    "Connection refused",
)


def is_transport_error(exc: BaseException) -> bool:
    """判断是不是「链路坏了」而不是「工具执行失败」。"""
    if type(exc).__name__ in _TRANSPORT_TYPES:
        return True
    text = str(exc)
    return any(hint in text for hint in _TRANSPORT_HINTS)


class GolemClient:
    """一个进程内共用一个 MCP 会话。"""

    def __init__(self) -> None:
        self._stack: AsyncExitStack | None = None
        self.session: ClientSession | None = None
        self.tools: list = []
        self._lock = asyncio.Lock()
        self._restart_lock = asyncio.Lock()
        self.last_error: str = ""

    @property
    def ready(self) -> bool:
        return self.session is not None

    async def start(self) -> None:
        if self.session is not None:
            return
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        params = StdioServerParameters(
            command=sys.executable,
            args=list(CFG.GOLEM_ARGS),
            env=env,
            cwd=str(Path(__file__).resolve().parents[1]),
        )
        stack = AsyncExitStack()
        try:
            read, write = await stack.enter_async_context(stdio_client(params))
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            tools = (await session.list_tools()).tools
        except Exception as exc:  # noqa: BLE001
            with contextlib.suppress(Exception):
                await stack.aclose()
            self.last_error = "%s: %s" % (type(exc).__name__, exc)
            raise
        self._stack = stack
        self.session = session
        self.tools = tools
        self.last_error = ""
        log.info("GOLEM MCP 已连接，%d 个工具", len(tools))

    async def stop(self) -> None:
        stack, self._stack = self._stack, None
        self.session = None
        if stack is not None:
            try:
                await stack.aclose()
            except Exception as exc:  # noqa: BLE001
                log.warning("关闭 MCP 会话时出错：%s", exc)

    async def restart(self) -> None:
        """重建整个 MCP 会话（重新拉起子进程，从而拿到全新的 TCP 连接）。"""
        async with self._restart_lock:
            await self.stop()
            await asyncio.sleep(0.3)
            await self.start()

    async def call(self, name: str, args: dict | None = None, timeout: float | None = None):
        """调用一个工具，返回 (ok, text, image_b64, image_mime)。

        链路级失败（连接被中止/重置/超时/MCP 子进程卡死）时自动重建会话并重试一次；
        工具自身的业务错误（参数不合法、对象不存在）不重试，避免重复产生副作用。
        """
        if not self.ready:
            await self.start()
        try:
            return await self._call_once(name, args, timeout)
        except Exception as exc:  # noqa: BLE001
            if not is_transport_error(exc):
                raise
            log.warning("工具 %s 链路失败，重建 MCP 会话后重试：%s", name, exc)
            await self.restart()
            return await self._call_once(name, args, timeout)

    async def _call_once(self, name: str, args: dict | None = None, timeout: float | None = None):
        if self.session is None:
            raise RuntimeError("GOLEM MCP 未连接")
        secs = float(timeout or CFG.RPC_TIMEOUT)
        async with self._lock:  # 插件侧同一连接串行，这里也不并发
            result = await self.session.call_tool(
                name,
                args or {},
                read_timeout_seconds=timedelta(seconds=secs),
            )

        texts: list[str] = []
        image: str | None = None
        mime = "image/png"
        for item in getattr(result, "content", None) or []:
            kind = getattr(item, "type", None)
            if kind == "text":
                texts.append(getattr(item, "text", "") or "")
            elif kind == "image":
                image = getattr(item, "data", None) or None
                mime = getattr(item, "mimeType", None) or "image/png"
            else:
                texts.append(str(item))

        raw_text = "\n".join(t for t in texts if t).strip()
        payload = None
        if raw_text:
            try:
                payload = json.loads(raw_text)
            except Exception:  # noqa: BLE001
                payload = None
        if payload is None:
            structured = getattr(result, "structuredContent", None)
            if structured is not None:
                payload = structured

        if isinstance(payload, dict) and isinstance(payload.get("image"), str):
            image = payload.pop("image")
            mime = "image/png"

        if payload is not None:
            text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        else:
            text = raw_text or "{}"

        return (not getattr(result, "isError", False)), text, image, mime
