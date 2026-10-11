"""Web 端配置。

优先级：真实环境变量 > golem-mcp/.env > 隔壁 rhino-mcp-agent/.env > 代码默认值。
"""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SIBLING_ENV = PROJECT_ROOT.parent / "rhino-mcp-agent" / ".env"
ENV_FILE: Path | None = None


def parse_env_text(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key, value = key.strip(), value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key] = value
    return out


def _load_env() -> Path | None:
    global ENV_FILE
    custom = os.environ.get("RHINO_WEB_ENV_FILE")
    candidates = [Path(custom).expanduser()] if custom else [PROJECT_ROOT / ".env", SIBLING_ENV]
    for cand in candidates:
        try:
            if not cand.is_file():
                continue
            for key, value in parse_env_text(cand.read_text(encoding="utf-8-sig")).items():
                if key not in os.environ:
                    os.environ[key] = value
            ENV_FILE = cand
            return cand
        except Exception:  # noqa: BLE001
            continue
    return None


_load_env()


def _s(name: str, default: str) -> str:
    return os.environ.get(name) or default


def _i(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _b(name: str, default: bool) -> bool:
    return _s(name, "1" if default else "0").strip().lower() in ("1", "true", "yes", "on")


class WebConfig:
    PROJECT_ROOT = PROJECT_ROOT

    # ---- LLM ----
    API_KEY: str = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""
    BASE_URL: str = _s("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
    MODEL: str = _s("RHINO_WEB_MODEL", _s("RHINO_AGENT_MODEL", "deepseek-v4-pro"))
    TEMPERATURE: float = _f("RHINO_WEB_TEMPERATURE", 0.2)
    MAX_TOKENS: int = _i("RHINO_WEB_MAX_TOKENS", 8192)
    LLM_TIMEOUT: float = _f("RHINO_WEB_LLM_TIMEOUT", 300.0)

    # ---- 闭环 ----
    MAX_TURNS: int = _i("RHINO_WEB_MAX_TURNS", 25)
    VISION: bool = _b("RHINO_WEB_VISION", True)
    KEEP_IMAGES: int = _i("RHINO_WEB_KEEP_IMAGES", 1)
    IMAGE_MAX_SIDE: int = _i("RHINO_WEB_IMAGE_MAX_SIDE", 1024)
    RPC_TIMEOUT: float = _f("RHINO_WEB_RPC_TIMEOUT", 180.0)

    # 逗号分隔的工具黑名单（例如 execute_python,run_rhino_command）
    DENY_TOOLS = [x.strip() for x in _s("RHINO_WEB_DENY_TOOLS", "").split(",") if x.strip()]

    # ---- 服务 ----
    HOST: str = _s("RHINO_WEB_HOST", "127.0.0.1")
    PORT: int = _i("RHINO_WEB_PORT", 8849)

    # ---- GOLEM 插件桥 ----
    RHINO_HOST: str = _s("GOLEM_RHINO_HOST", "127.0.0.1")
    RHINO_PORT: int = _i("GOLEM_RHINO_PORT", 9876)
    GOLEM_ARGS = ["-m", "golem_3dmcp", "start"]


CFG = WebConfig()
