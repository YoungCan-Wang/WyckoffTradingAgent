"""打包后的 IPC 服务入口。

桌面端原本 spawn `python -m cli ipc`，那要求用户机器上有仓库和 venv。打包后
Electron 改 spawn 这个二进制。

直接进 serve() 而不复用 cli.__main__.main()：
- 不需要 argparse，也就不把整棵子命令树（含 TUI 分支）拖进依赖图
- 避免 _set_terminal_title 之类面向终端的启动动作
"""

from __future__ import annotations

import sys


def main() -> int:
    # .env 仍然要读：用户的 API key 和数据源配置在那里。打包后 cwd 不确定，
    # 所以让 dotenv 自己按默认规则找，找不到就算了（配置也可能全在环境变量里）。
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:  # noqa: BLE001 - 配置缺失不该阻止启动
        pass

    from cli.ipc.stdio import serve

    verbose = "--verbose" in sys.argv or "-v" in sys.argv
    return serve(verbose=verbose)


if __name__ == "__main__":
    sys.exit(main())
