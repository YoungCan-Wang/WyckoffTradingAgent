# PTC 沙箱摘要

CLI 工具 `ptc_summarize` 在无 import / 无网络沙箱里执行短 Python，只把 `summary` 回给模型。禁止把全市场原始行情塞进上下文。

**影响范围**：CLI / TUI 工具列表（新增只读工具）。不改漏斗、OMS、Web 页面、MCP 默认工具集。
