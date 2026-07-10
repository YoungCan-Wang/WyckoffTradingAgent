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
- `core/market_trade_mode.py` (NEUTRAL mainline_active, RISK_ON observe_only)
- `.github/workflows/wyckoff_funnel.yml` and `tail_buy_1440.yml`

Operator guide: [`docs/OPERATOR_PLAYBOOK.md`](../../docs/OPERATOR_PLAYBOOK.md).
