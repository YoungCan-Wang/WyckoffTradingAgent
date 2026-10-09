# Config Profiles

This directory stores safe, shareable strategy profiles.

Commit profiles only when they contain public defaults and no personal data.
Private overrides should use `.env`, `config/profiles/*.local.yml`, or
`config/profiles/*private*.yml`; these paths are ignored by git.

`a_share_prod.yml` is the default production-style profile (mainline engine
thresholds; themes empty = dynamic discovery). Environment variables still win
over profile values for runtime jobs.

Mainline and external-seed tools share `utils/config_profile.py`. Profile selection
keeps `WYCKOFF_CONFIG_PATH` ahead of `WYCKOFF_CONFIG_PROFILE`, then defaults to
the packaged `a_share_prod.yml`. Missing files/sections retain defaults; malformed
YAML still raises. No caching or runtime threshold changes are introduced.

`FunnelConfig.evr_lookback` and `markup_rs_positive_min` were unused declarations
and have been removed. Old environment keys remain ignored; Python constructor
calls must omit these keywords. Config digests change with the schema, not with
the remaining defaults. `.env.example` also drops the nonexistent
`FUNNEL_CFG_SECTOR_SUPER_STRENGTH_QUANTILE` and the intentionally ignored
`FUNNEL_CFG_ENABLE_EVR_TRIGGER`; use `FUNNEL_EVR_POLICY` for EVR enable policy.
This maintenance cleanup is not a strategy-performance or anti-overfitting result.

A-share **trading** defaults (quotas, hard stops, regime blocks) live mainly in:

- `core/ai_candidate_allocation.py` / GitHub Actions env (`FUNNEL_AI_*`)
- `core/market_trade_mode.py` (写入闸门与 `STEP4_BUY_BLOCK_REGIMES` 同源；生产下 NEUTRAL/RISK_ON 均禁新仓)
- `.github/workflows/wyckoff_funnel.yml` and `holding_diagnosis.yml`

Operator guide: [`docs/OPERATOR_PLAYBOOK.md`](../../docs/OPERATOR_PLAYBOOK.md).
