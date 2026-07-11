# Wyckoff-Analysis Development Rules

> This file is the single source of truth for code quality rules.
> All AI coding assistants (Claude Code, Cursor, Copilot, Windsurf, etc.) MUST follow these rules.

## Project Overview

Multi-market quantitative analysis system based on Wyckoff method, covering A-shares, Hong Kong stocks, US stocks, and ETFs. Python backend (CLI + MCP) + React/TypeScript web frontend.

Streamlit is fully retired from `main`: do not add, restore, or maintain Streamlit runtime code here. The historical Streamlit MVP code is preserved on the `release/streamlit` branch, and its product architecture/screenshots are archived in [docs/STREAMLIT_MVP_ARCHITECTURE.md](docs/STREAMLIT_MVP_ARCHITECTURE.md).

## Quick Commands

```bash
# Python
.venv/bin/python -m pytest tests/ -x -q  # run tests
.venv/bin/ruff check .                   # lint
.venv/bin/ruff format --check .          # format check
.venv/bin/python scripts/quality_gate.py --ci  # function length + LOC trend

# Web (from web/ directory)
pnpm dev                                 # dev server
pnpm build                               # production build
pnpm -r exec tsc --noEmit                # typecheck
```

## Hard Rules (CI enforced, will block merge)

1. **Pass quality gate** — `.venv/bin/python scripts/quality_gate.py --ci` must pass function-length hard limits. LOC growth warnings are review signals, not automatic failures.

2. **Pass ruff check** — All Python code must pass `ruff check .` with the project config in `pyproject.toml`.

3. **Pass ruff format** — All Python code must be formatted with `ruff format`.

4. **Pass TypeScript strict mode** — Web code must compile with `tsc --noEmit` (strict: true, noUnusedLocals, noUnusedParameters).

5. **Pass pytest** — All tests must pass. Tests must not make real network calls.

## Review Rules (strong expectations, not mechanically CI-enforced)

1. **Function length target ≤ 50 lines; hard limits by layer** — 50 lines remains the design target, not a mechanical wall. New functions block merge only when they exceed the layer hard limit enforced by `scripts/quality_gate.py`: default/core/agents/tools/integrations/workflows/shared packages ≤70 lines, scripts/CLI orchestration ≤100 lines, React route pages ≤120 lines, React components/app glue ≤90 lines. Whitelisted legacy functions are tracked as visible debt in `.metrics/func_whitelist.json`; they may remain temporarily over limit, but must not grow longer.

2. **No redundant code** — Every function, variable, and abstraction must earn its existence. Review aggressively for wrapper functions whose body is a single forwarded call, variables assigned once and immediately returned, one-off abstractions with no clear reuse/design value, and re-exports that add no boundary clarity.

3. **No code bloat** — If 30 lines can do the job, don't write 50. Code volume is tracked in `.metrics/loc.json`; growth >5% without corresponding feature additions is a warning that must be explained or paid down.

4. **No dead code** — Don't leave unused imports, commented-out blocks, or unreachable branches. Delete them.

5. **Comments: only when WHY is non-obvious** — Don't explain what code does. Don't reference tickets or tasks. Only explain hidden constraints or surprising behavior.

6. **No debug artifacts** — Don't commit `console.log`, `breakpoint()`, `TODO/FIXME`, temporary dumps, or `print("debug")`-style traces. In `core/`, `integrations/`, `tools/`, and `agents/`, use logging instead of print-style diagnostics. In `scripts/` and `cli/`, user-facing progress/output via `print()` is allowed.

## Gate Levels

- **Fast gate (local/default)**: `.venv/bin/ruff check .`, `.venv/bin/ruff format --check .`, `.venv/bin/python scripts/quality_gate.py --check-functions`, and focused tests for touched code.
- **Full gate (CI/release)**: fast gate plus full `pytest`, TypeScript strict mode, web tests/build, and dry-run jobs where relevant.

## Architecture Constraints

- **Web: no new pages** — New features go into the Agent (chat) interface, not as separate routes.
- **No Streamlit in main** — Streamlit is no longer maintained on `main`; route product work through CF Pages, CLI, MCP, or GitHub Actions.
- **Data isolation: Route A** — Signals are shared; portfolio and settings are per-user.
- **Python ≥ 3.11**, **Node ≥ 20**, **pnpm** for web workspace.
- The standalone `wiki_repo_new/` checkout is intentionally hidden and independently versioned; keep it ignored and do not merge it into `docs/`.

## Documentation Synchronization

- Current code, workflow configuration, and live data contracts are the source of truth. Do not preserve documentation text that conflicts with implementation.
- Every change to strategy semantics, report fields, prompts, scheduled workflows, environment variables, SQL tables, CLI/MCP/Web tools, or operational behavior must update the affected `README*`, `GLOSSARY.md`, and `docs/` pages in the same change.
- The independent `wiki_repo_new/` repository must be reviewed for the same change and committed separately when affected. Keep its content aligned with code rather than copying stale prose from `docs/`.
- Before submission, search documentation for renamed files, removed symbols, changed defaults, and old execution wording. In particular, keep research states (`pending`, `confirmed`, `起跳板`) distinct from executable decisions (`BUY` after market and OMS gates).
- Do not commit one-off SQL migration files after the migration has been applied unless the repository explicitly adopts migration history as a maintained product artifact.

## Commit Messages

Use conventional prefixes: `feat:`, `fix:`, `refactor:`, `test:`, `docs:`, `chore:`.

## Before Submitting Code

```bash
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/python scripts/quality_gate.py --check-functions
```
