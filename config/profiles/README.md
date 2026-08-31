# Config Profiles

This directory stores safe, shareable strategy profiles.

Commit profiles only when they contain public defaults and no personal data.
Private overrides should use `.env`, `config/profiles/*.local.yml`, or
`config/profiles/*private*.yml`; these paths are ignored by git.

`a_share_prod.yml` is the default production-style profile (mainline engine
thresholds; themes empty = dynamic discovery). Environment variables still win
over profile values for runtime jobs.

A-share **trading** defaults (quotas, hard stops, regime blocks) live mainly in:

- `core/ai_candidate_allocation.py` / GitHub Actions env (`FUNNEL_AI_*`)
- `core/market_trade_mode.py` (写入闸门与 `STEP4_BUY_BLOCK_REGIMES` 同源；生产下 NEUTRAL/RISK_ON 均禁新仓)
- `.github/workflows/wyckoff_funnel.yml` and `holding_diagnosis.yml`

Operator guide: [`docs/OPERATOR_PLAYBOOK.md`](../../docs/OPERATOR_PLAYBOOK.md).
