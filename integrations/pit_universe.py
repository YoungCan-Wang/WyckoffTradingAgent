"""Point-in-time A 股股票池：补齐历史退市与当时 ST 标的。

快照回放此前用「拉取当时的存续名单且不含 ST」当股票池，于是自动排除了两类在窗口内
真实可交易的标的：拉取日之前已退市的（乐视网、千山药机等），以及拉取日处于 ST 状态的。
实测缺口 4.1%–10.8%（越早的窗口越大），方向是乐观偏差，且集中在「便宜且困境」这一段。

本模块只负责「某个 as-of 日期应该有哪些股票」，不涉及行情抓取。
"""

from __future__ import annotations

from dataclasses import dataclass

from core.cn_boards import cn_board, is_supported_cn_board


@dataclass(frozen=True)
class PitSymbol:
    code: str
    name: str
    list_date: str
    delist_date: str

    @property
    def delisted(self) -> bool:
        return bool(self.delist_date)

    @property
    def is_st(self) -> bool:
        return "ST" in self.name.upper()


def _digits6(raw: object) -> str:
    text = "".join(ch for ch in str(raw or "") if ch.isdigit())
    return text[-6:].zfill(6) if len(text) >= 6 else ""


def _ymd(raw: object) -> str:
    return "".join(ch for ch in str(raw or "") if ch.isdigit())[:8]


def build_pit_symbols(rows: list[dict]) -> list[PitSymbol]:
    """把 `stock_basic` 的行（L 与 D 合并）转成去重后的 PitSymbol 列表。

    同一代码在退市与存续两张表都出现时（历史上代码被重新启用），保留带退市日的那条——
    回放需要知道它在某个时点是否已经摘牌。
    """
    best: dict[str, PitSymbol] = {}
    for row in rows or []:
        code = _digits6(row.get("symbol") or row.get("code") or row.get("ts_code"))
        if not code or not is_supported_cn_board(code):
            continue
        item = PitSymbol(
            code=code,
            name=str(row.get("name", "") or "").strip(),
            list_date=_ymd(row.get("list_date")),
            delist_date=_ymd(row.get("delist_date")),
        )
        current = best.get(code)
        if current is None or (item.delisted and not current.delisted):
            best[code] = item
    return sorted(best.values(), key=lambda x: x.code)


def tradable_on(symbols: list[PitSymbol], as_of: str, *, include_bse: bool = True) -> list[PitSymbol]:
    """给出 `as_of`（YYYYMMDD）当日真实可交易的标的。

    判据：已上市（`list_date <= as_of`），且未在该日之前摘牌（无退市日，或 `delist_date >= as_of`）。
    **不按 ST 过滤**——ST 股在窗口内是可交易的，排除它们正是原偏差的一半来源。
    """
    day = _ymd(as_of)
    if not day:
        return []
    out = []
    for item in symbols:
        if not include_bse and cn_board(item.code) == "bse":
            continue
        if item.list_date and item.list_date > day:
            continue
        if item.delist_date and item.delist_date < day:
            continue
        out.append(item)
    return out


def fetch_pit_symbols() -> list[PitSymbol]:
    """从 Tushare 取存续（含 ST）与已退市名单的并集。

    `list_status='L'` 含 ST；`list_status='D'` 带 `delist_date`。两者并集即完整历史池。
    实测 L=5539（含 ST 207）、D=339（全部带退市日，最早 1999-07-12）。
    """
    from integrations.tushare_client import get_pro

    pro = get_pro()
    fields = "ts_code,symbol,name,list_date,delist_date"
    rows: list[dict] = []
    for status in ("L", "D"):
        frame = pro.stock_basic(exchange="", list_status=status, fields=fields)
        if frame is not None and not frame.empty:
            rows.extend(frame.to_dict("records"))
    return build_pit_symbols(rows)


def universe_gap(should: list[PitSymbol], have: set[str]) -> dict[str, object]:
    """对比应有池与实际快照池，给出缺口构成。用于回放前的自检。"""
    missing = [s for s in should if s.code not in have]
    return {
        "should": len(should),
        "have": len({s.code for s in should} & have),
        "missing": len(missing),
        "missing_pct": round(len(missing) / max(len(should), 1) * 100, 2),
        "missing_delisted": sum(1 for s in missing if s.delisted),
        "missing_st": sum(1 for s in missing if s.is_st and not s.delisted),
        "sample": [s.code for s in missing[:8]],
    }
