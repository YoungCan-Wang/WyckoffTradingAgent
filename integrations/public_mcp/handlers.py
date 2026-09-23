"""Protocol-specific adaptations that preserve production funnel semantics."""

from __future__ import annotations


def run_funnel_simulation(board: str = "all", limit: int | None = None) -> dict:
    board_name = str(board or "all").strip().lower()
    if board_name == "main_chinext":
        board_name = "main_chinext_star"
    if board_name not in {"all", "main_chinext_star", "main", "chinext", "star", "bse"}:
        return {"error": f"不支持的 board 值 '{board}'"}
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 0 or limit > 3000):
        return {"error": "limit 必须是 0–3000 的整数；全量扫描请留空或传 0"}

    from tools.funnel_public import public_funnel_details
    from workflows.wyckoff_funnel import run as run_funnel

    ok, symbols, bench_ctx, details = run_funnel(
        "",
        notify=False,
        return_details=True,
        pool_board=board_name,
        pool_limit_count=limit,
        executor_mode="thread",
    )
    if not ok:
        return {"error": "漏斗运行失败", "details": public_funnel_details(details)}
    return {
        "success": True,
        "candidates": symbols,
        "regime": bench_ctx,
        "details": public_funnel_details(details),
    }
