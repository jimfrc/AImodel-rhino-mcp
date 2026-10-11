"""对话闭环：DeepSeek function calling ↔ GOLEM MCP 工具 ↔ Rhino 视口截图。

与旧工程 agent/loop.py 的区别：
  * 工具 schema 不是手写的，而是启动时从 GOLEM MCP server 动态拉取（89 个）
  * 执行工具走 MCP 协议（golem_client），不再直连私有帧协议
  * 单位不再是「米」：GOLEM 全流程使用 Rhino 当前文档单位，提示词里已要求先查单位
"""

from __future__ import annotations

import json
import logging
from typing import Awaitable, Callable

from openai import AsyncOpenAI

from .config import CFG
from .golem_client import compress_png

log = logging.getLogger("golem.web.agent")

EventHandler = Callable[[dict], Awaitable[None]]

_MAX_HISTORY_MESSAGES = 240

SYSTEM_PROMPT = """你是运行在 Rhino 8 里的三维建模 Agent，通过 MCP 工具直接读写 Rhino 文档。

工作流「读 → 建 → 看 → 改」：
1. 读：先调用 get_document_info 了解文档名、单位、对象数量；必要时用 list_layers / list_objects。
2. 规划：把需求拆成明确的几何步骤。**所有长度参数都使用 Rhino 当前文档单位**，
   不要假设是米；单位以 get_document_info 的返回为准。用户说「米」时你自己换算。
3. 执行：优先用专用工具（create_* / array_* / mirror_* / boolean_* / loft_curves / extrude_curve …）。
   创建后记下返回的 guid，后续修改一律用 guid 引用，不要靠名字猜。
   重复构件用 copy_objects / array_objects_linear / array_objects_polar，不要一个个重画。
4. 看：每次写完几何后必须 zoom_extents 再 capture_viewport 亲眼检查，截图就是你的眼睛。
5. 改：对照需求检查位置/尺寸/数量/朝向/间距。不满意用 delete_objects 清掉重做，最多重试 3 次。
6. 收敛：确认无误后给简短结论——建了什么、关键尺寸、guid、下一步建议。

硬性约束：
- 删除、隐藏、加锁前先用 list_objects / get_selected_objects 确认目标。
- 不确定的尺寸（规范指标、用户偏好）先问用户，不要臆造。
- 单轮不要塞过多对象；超过 20 个构件时拆分多轮。
- execute_python / run_rhino_command / execute_rhinoscript 只在没有专用工具时使用。
- 工具返回以 [ERROR] / [MCP_ERROR] 开头时先读错误信息自纠，不要重复发同一条调用。
"""


def _strip_titles(node):
    """去掉 JSON Schema 里的 title 字段，缩小 tool 定义体积。"""
    if isinstance(node, dict):
        return {k: _strip_titles(v) for k, v in node.items() if k != "title"}
    if isinstance(node, list):
        return [_strip_titles(x) for x in node]
    return node


def build_openai_tools(mcp_tools, deny: list[str]) -> list[dict]:
    out: list[dict] = []
    for tool in mcp_tools:
        if tool.name in deny:
            continue
        schema = tool.inputSchema or {"type": "object", "properties": {}}
        out.append({
            "type": "function",
            "function": {
                "name": tool.name,
                "description": (tool.description or "").strip(),
                "parameters": _strip_titles(schema),
            },
        })
    return out


def _assistant_dict(message) -> dict:
    data: dict = {"role": "assistant", "content": message.content or ""}
    calls = getattr(message, "tool_calls", None)
    if calls:
        data["tool_calls"] = [
            {
                "id": c.id,
                "type": "function",
                "function": {
                    "name": c.function.name,
                    "arguments": c.function.arguments or "{}",
                },
            }
            for c in calls
        ]
    return data


def _image_message(image_b64: str, caption: str, mime: str = "image/png") -> dict:
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": caption},
            {"type": "image_url", "image_url": {"url": "data:%s;base64,%s" % (mime, image_b64)}},
        ],
    }


class WebSession:
    """一条浏览器连接对应一个会话（独立上下文，共用同一个 MCP 会话）。"""

    def __init__(self, mcp):
        self.mcp = mcp
        self.client = AsyncOpenAI(
            api_key=CFG.API_KEY or "EMPTY",
            base_url=CFG.BASE_URL,
            timeout=CFG.LLM_TIMEOUT,
        )
        self.tools = build_openai_tools(mcp.tools, CFG.DENY_TOOLS)
        self.messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]

    def reset(self) -> None:
        self.messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    async def run(self, user_text: str, emit: EventHandler) -> str:
        self.messages.append({"role": "user", "content": user_text})

        final_text = ""
        for _turn in range(max(1, CFG.MAX_TURNS)):
            try:
                resp = await self.client.chat.completions.create(
                    model=CFG.MODEL,
                    messages=self.messages,
                    tools=self.tools or None,
                    tool_choice="auto" if self.tools else None,
                    temperature=CFG.TEMPERATURE,
                    max_tokens=CFG.MAX_TOKENS,
                )
            except Exception as exc:  # noqa: BLE001
                await emit({"type": "error", "message": "调用模型失败：%s: %s" % (type(exc).__name__, exc)})
                return final_text

            if not resp.choices:
                await emit({"type": "error", "message": "模型返回空 choices"})
                return final_text

            choice = resp.choices[0]
            message = choice.message
            self.messages.append(_assistant_dict(message))

            text = (message.content or "").strip()
            if text:
                final_text = text
                await emit({"type": "assistant", "text": text})

            calls = list(getattr(message, "tool_calls", None) or [])
            if not calls:
                if not text:
                    await emit({
                        "type": "info",
                        "message": "模型本轮没有产生任何内容（finish_reason=%s），已停止。"
                                   % getattr(choice, "finish_reason", None),
                    })
                break

            for call in calls:
                await self._run_tool(call, emit)
            self._trim()
        else:
            await emit({
                "type": "info",
                "message": "已达到单轮工具回合上限 %d，先停下来汇报当前状态。" % CFG.MAX_TURNS,
            })

        await emit({"type": "done", "text": final_text})
        return final_text

    # ------------------------------------------------------------------ 内部

    async def _run_tool(self, call, emit: EventHandler) -> None:
        name = call.function.name
        raw_args = call.function.arguments or "{}"
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args or {})
        except (json.JSONDecodeError, TypeError, ValueError):
            bad = "[INVALID_ARGS] 参数不是合法 JSON: %s" % raw_args
            self.messages.append({"role": "tool", "tool_call_id": call.id, "content": bad})
            await emit({"type": "tool_result", "name": name, "ok": False, "text": bad})
            return

        await emit({"type": "tool_call", "id": call.id, "name": name, "args": args or {}})

        try:
            ok, text, image, mime = await self.mcp.call(name, args or {})
        except Exception as exc:  # noqa: BLE001
            ok = False
            text = "[MCP_ERROR] %s: %s" % (type(exc).__name__, exc)
            image, mime = None, "image/png"

        self.messages.append({"role": "tool", "tool_call_id": call.id, "content": text})
        await emit({"type": "tool_result", "name": name, "ok": ok, "text": text})
        if not image:
            return

        data = image
        if CFG.IMAGE_MAX_SIDE:
            try:
                data = compress_png(image, CFG.IMAGE_MAX_SIDE)
            except Exception as exc:  # noqa: BLE001
                log.warning("截图压缩失败，回退原图：%s", exc)

        await emit({"type": "image", "data": data, "caption": "%s 截图" % name})
        if CFG.VISION:
            self.messages.append(_image_message(data, "Rhino 当前视口截图", mime))
    def _trim(self) -> None:
        """控制上下文成本：旧截图换成文字占位，历史过长时从头裁剪。"""
        keep = max(0, CFG.KEEP_IMAGES)
        image_idx = [
            i for i, m in enumerate(self.messages)
            if m.get("role") == "user" and isinstance(m.get("content"), list)
        ]
        for i in image_idx[:-keep] if keep else image_idx:
            self.messages[i] = {"role": "user", "content": "[前面查看过的截图，已释放以节省上下文]"}

        if len(self.messages) > _MAX_HISTORY_MESSAGES:
            head = self.messages[0]
            tail = self.messages[-(_MAX_HISTORY_MESSAGES - 1):]
            while tail and tail[0].get("role") == "tool":
                tail.pop(0)
            self.messages = [head] + tail
