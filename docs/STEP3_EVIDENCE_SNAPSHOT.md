# Step3 证据快照

对外数字冻结在 `snapshot.json` 的 `numbers` 字段。LLM 只能写 `prose`，不能改 `selected_count` / `veto_count` 等。

默认路径：`STEP3_EVIDENCE_PATH` 或 `logs/step3_evidence_snapshot.json`。

**影响范围**：Step3 报告产物 / 飞书数字口径。不改漏斗计算、OMS、TUI。
