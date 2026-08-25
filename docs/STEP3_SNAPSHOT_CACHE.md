# Step3 dataSnapshotHash 缓存

Step3 研报缓存键绑定交易日、水温、候选指纹、RAG 否决清单、prompt 版本和模型。候选分数变了必须重跑 LLM，避免行情已变仍复用旧结论。

默认 `STEP3_SNAPSHOT_CACHE=1`。目录：`STEP3_SNAPSHOT_CACHE_DIR` 或 `logs/step3_cache`。

**影响范围**：Step3 AI 研报 / 飞书正文复用。不改漏斗分层、跨日确认、OMS、TUI。
