# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec：把 IPC 服务打成自包含二进制。

桌面端用户不用 TUI，所以 textual 整棵排除掉。rich 不能排 —— httpx 的 CLI
辅助模块会 import 它，排掉会在运行时炸。

懒加载且桌面端用不到的重依赖也排除（litellm 57M、playwright），
它们只在 CLI 的特定路径上才 import。如果将来桌面端要用模型路由，
把 litellm 从 excludes 里去掉即可。
"""

import sys
from pathlib import Path

REPO = Path(SPECPATH).parent

# 显式收集：这些包大量使用运行时反射/插件注册，静态分析会漏。
HIDDEN = [
    "pandas",
    "numpy",
    "supabase",
    "postgrest",
    "supabase_auth",
    "storage3",
    "realtime",
    "supabase_functions",
    "anthropic",
    "openai",
    "google.genai",
    "akshare",
    "yaml",
    "dotenv",
]

# TUI 专属，桌面端不需要。textual 5.9M，连带 markdown-it 等一串。
EXCLUDE = [
    "textual",
    "textual.app",
    "litellm",
    "playwright",
    "baostock",
    "efinance",
    "matplotlib",
    "tkinter",
    "pytest",
    "IPython",
]

a = Analysis(
    [str(REPO / "packaging" / "ipc_entry.py")],
    pathex=[str(REPO)],
    binaries=[],
    datas=[],
    hiddenimports=HIDDEN,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDE,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="wyckoff-ipc",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX 会破坏 macOS 代码签名，且对已签名的 .so 有兼容问题。
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="wyckoff-ipc",
)
