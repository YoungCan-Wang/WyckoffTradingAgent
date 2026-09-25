# Public MCP server

This document covers WyckoffAgent **as a server** for external MCP hosts. It does not
change `cli/mcp_client.py`, which connects this Agent to third-party servers.

## Installation and entrypoints

```bash
uvx --from 'youngcan-wyckoff-analysis[mcp]' wyckoff-mcp
```

For development, install this checkout with the `mcp` extra, then run `wyckoff-mcp`
or `python mcp_server.py`. A PR does not update PyPI: the refactor is available via
the package command only after a maintainer publishes the corresponding release.

For a client that separates executable and argument array:

```json
{
  "mcpServers": {
    "wyckoff": {
      "command": "uvx",
      "args": ["--from", "youngcan-wyckoff-analysis[mcp]", "wyckoff-mcp"]
    }
  }
}
```

Shell quoting belongs to the shell, not inside a JSON argument. MCPVault's
non-shell command field uses `uvx --from=youngcan-wyckoff-analysis[mcp] wyckoff-mcp`
without quotes. Keep the quotes for zsh/bash copy-paste instructions.

## Architecture and lifecycle

`mcp_server.py` remains the installed console-script entrypoint. The implementation
is in `integrations/public_mcp/`:

- `contracts.py`: the 19 existing tool names, input defaults, JSON Schemas and conservative annotations.
- `server.py`: SDK-managed stdio lifecycle, discovery and MCP result serialization.
- `runtime.py`: strict input validation, action-level write policy, bounded results and error semantics.
- `backend.py`: lazy principal/context initialization and the shared production `ToolSurface`.
- `handlers.py`: the existing funnel parameter adaptation, not a replacement trading algorithm.

Import, `initialize` and `tools/list` must not load business modules, read a CLI
session, initialize SQLite, contact a model or data provider, or need user secrets.
The first allowed business call lazily loads the production backend. Missing
runtime dependencies or credentials are not disguised as a working analysis.

Every tool uses the same execution boundary, including portfolio reads, reports,
strategy decisions and the funnel simulation. Research discovery keeps the existing
bounded preview and production execution gates. Screening candidates remain
research, not orders. No strategy parameters, brokerage integration or production
workflow is changed.

## Identity and side effects

The supported deployment here is a local **stdio process for one user**, inheriting
that user's explicit environment or local CLI session on its first domain call.
Handshake without credentials does **not** mean that every business tool is anonymous.
The process is not an OAuth server and must not be exposed as a shared multi-user
HTTP endpoint. Streamable HTTP, request-scoped authentication and quota enforcement
require a separate implementation and security review; they are not advertised here.

`update_portfolio` and `record_trade_fill` are denied by default, before the domain
backend is loaded. This version also guards the mutating actions of
`research_hypothesis`; only `list` and `detail` bypass the write opt-in. `evaluate`
is conservatively treated as a possible write. For unattended writes the existing
`WYCKOFF_MCP_ALLOW_WRITES=1` opt-in remains an explicit acceptance of risk, **not** a
human approval or an authentication grant. CLI/desktop approval remains preferable.

Tool annotations are descriptive hints, not authorization. Expensive calls and
calls that may save research artifacts are not falsely marked read-only or
idempotent. Model hosts must not automatically retry trade-fill mutations.

## Input and output contract

All tools reject unknown arguments; callers cannot inject `tool_context`, tokens or
another user ID. Optional defaults preserve the difference between missing, `null`,
`false` and zero. In particular, omitted `free_cash` is still `None`, not zero;
`screen_stocks.limit=0` still requests the full scan.

Results are JSON objects in `structuredContent`, mirrored as a JSON text block for
older hosts. Existing object fields are preserved; a top-level array/scalar is
wrapped as `{"result": ...}`. Domain errors set MCP `isError=true` instead of looking
like successful tool results. The output schema intentionally promises an object,
not a fabricated fixed schema for every evolving domain report.

Boundary error codes include `UNKNOWN_TOOL`, `INVALID_ARGUMENTS`, `INPUT_TOO_LARGE`,
`WRITE_DENIED`, `MISSING_DEPENDENCY`, `TOOL_FAILED`, `INVALID_RESULT` and
`RESULT_TOO_LARGE`. Inputs are limited to 64 KiB; results to 1 MiB. Oversized or
non-JSON results fail explicitly rather than being silently truncated. Sensitive
credential fields are redacted; unexpected exception strings are not sent to the host.

Python stdout produced by legacy business libraries is redirected to stderr while
the SDK writes JSON-RPC to its previously captured stdout. Native C code writing
directly to file descriptor 1 is not covered by Python's redirection.

## Long-running work and compatibility

Business calls are serialized per process to avoid overlapping local state writes.
Mutating calls (`update_portfolio`, `record_trade_fill`, and non-list/detail
`research_hypothesis` actions) run on the calling thread without ToolSurface's
abandoning timeout worker, so the Runtime lock is not released while a write still
runs. Research scans/backtests keep a 600-second budget. A client timeout or
cancellation is **not** a guarantee that a non-mutating domain call stopped. No
automatic retry or pretend MCP Tasks implementation is introduced.

Protocol-version negotiation is handled by the installed MCP SDK. This PR does not
claim full support for every feature of the newest specification, Tasks, remote
OAuth, or every IDE. Verify each client before adding it to a compatibility listing.

## Verification and remaining packaging work

Focused tests cover preserved names/defaults, write denial before imports, malicious
context arguments, error signaling, result bounds and a real subprocess handshake
with domain imports deliberately blocked. CI must install `[dev,mcp]`; a skipped
SDK test is not handshake evidence.

The refactor makes **discovery imports** lightweight. It deliberately leaves the
root package's dependency graph unchanged to avoid breaking existing bare-package
CLI installations. `[mcp]` still adds to the full base dependency set, so the
MCPVault `ENOSPC` dependency-installation failure can still occur. Lazy imports do
not reduce download size. Packaging must be handled with a separately measured,
compatible distribution change, not by removing dependencies while advertising
unavailable business tools.
