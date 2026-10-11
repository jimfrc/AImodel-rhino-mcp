# -*- coding: utf-8 -*-
"""在 Rhino 8 的 ScriptEditor 里运行本文件，启动 GOLEM-3DMCP 桥接服务。

为什么不用官方的 `golem install-rhino`：
  官方 0.2.2 的 install-rhino 会把插件目录复制成 `golem_3dmcp_plugin`，
  但插件内部全部使用 `from rhino_plugin.xxx import ...` 绝对导入；
  且它自带的 startup.py 里 project_root 还硬编码着作者的 macOS 路径
  `/Users/kinghippo/Documents/GOLEM-3DMCP`。因此在 Windows 上必然加载失败。

本脚本做的事：
  1. 若已有旧实例在跑，先停掉；
  2. 清空 sys.modules 里的 rhino_plugin.* 缓存，保证每次都加载最新代码
     （改过 rhino_plugin/ 下的源码后必须这样，否则 Rhino 里跑的还是旧版本）；
  3. 在 127.0.0.1:9876 上启动 TCP 服务。

用法（Rhino 8）：命令行输入 ScriptEditor，打开本文件，点 Run。

控制台可用函数：
  golem_start()  启动（重复调用安全）
  golem_stop()   停止
  golem_status() 查看运行状态与已注册 handler 数量
"""

import sys

GOLEM_ROOT = r"F:\MCP-agent\golem-mcp"
HOST = "127.0.0.1"
PORT = 9876

if GOLEM_ROOT not in sys.path:
    sys.path.insert(0, GOLEM_ROOT)


def _stop_old():
    """停掉上一次运行留下的实例（模块可能还是旧代码）。"""
    try:
        import rhino_plugin.server as _old
    except Exception:  # noqa: BLE001
        return False
    try:
        if _old._is_running():
            _old.stop_server()
            print("GOLEM-3DMCP: 已停止旧实例")
            return True
    except Exception as exc:  # noqa: BLE001
        print("GOLEM-3DMCP: 停止旧实例失败：%s" % exc)
    return False


def _purge_modules():
    """清掉插件模块缓存，强制下次 import 读到磁盘上的最新代码。"""
    names = [m for m in list(sys.modules) if m == "rhino_plugin" or m.startswith("rhino_plugin.")]
    for name in names:
        sys.modules.pop(name, None)
    return len(names)


_stop_old()
_purged = _purge_modules()
if _purged:
    print("GOLEM-3DMCP: 已重载 %d 个插件模块" % _purged)

import rhino_plugin.server as _server  # noqa: E402
from rhino_plugin.dispatcher import get_registered_methods  # noqa: E402


def golem_start():
    _server.start_server(HOST, PORT)
    try:
        print(
            "GOLEM-3DMCP: listening on %s:%s, %d handlers registered"
            % (HOST, PORT, len(get_registered_methods()))
        )
    except Exception as exc:  # noqa: BLE001
        print("GOLEM-3DMCP: started, but handler list unavailable: %s" % exc)


def golem_stop():
    _server.stop_server()
    print("GOLEM-3DMCP: stopped")


def golem_status():
    try:
        running = _server._is_running()
    except Exception:  # noqa: BLE001
        running = "unknown"
    try:
        count = len(get_registered_methods())
    except Exception:  # noqa: BLE001
        count = -1
    print("GOLEM-3DMCP: running=%s port=%s handlers=%d" % (running, PORT, count))
    return running


# Rhino 的 ScriptEditor 运行时不一定把 __name__ 设为 "__main__"，
# 所以这里无条件启动；反复运行是安全的（start_server 内部幂等）。
golem_start()
