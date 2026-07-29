"""Point-in-time date × symbol panel for cross-sectional factor research.

与 `snapshot_data/` 的区别在股票池口径。快照按「拉取当日仍存续且非 ST」的名单逐只拉 K 线，
于是任何历史窗口都自动排除了后来退市的和今天挂 ST 的标的（约占可交易标的 7.5%），而这批票
恰好集中在「便宜且困境」一段，对价值类研究是方向明确的乐观偏差。

这里改成按交易日横切：`daily` / `adj_factor` / `daily_basic` 都以 `trade_date` 为参数取全市场，
当天在交易的股票就一定在结果里，无需事先知道股票池，退市与 ST 自然包含。名称按 `namechange`
还原成逐日的历史名称，ST 状态因而也是 PIT 的。

价格存不复权原值加复权因子，收益由调用方用 `close * adj_factor` 计算，避免快照 qfq 那种
「随最新分红整条序列漂移」的口径。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd

from integrations.tushare_client import get_pro

logger = logging.getLogger(__name__)

DAILY_FIELDS = "ts_code,trade_date,open,high,low,close,pre_close,vol,amount"
BASIC_FIELDS = "ts_code,trade_date,turnover_rate_f,volume_ratio,pe_ttm,pb,ps_ttm,dv_ttm,total_mv,circ_mv"
FLOAT32_COLUMNS = (
    "open", "high", "low", "close", "pre_close", "vol", "amount", "adj_factor",
    "turnover_rate_f", "volume_ratio", "pe_ttm", "pb", "ps_ttm", "dv_ttm", "total_mv", "circ_mv",
)  # fmt: skip
MAX_ATTEMPTS = 4


class PanelFetchError(RuntimeError):
    """Tushare 连续失败，调用方需要中止而不是产出残缺面板。"""


def _call(endpoint: Callable[..., pd.DataFrame | None], **kwargs) -> pd.DataFrame:
    import time

    for attempt in range(MAX_ATTEMPTS):
        try:
            got = endpoint(**kwargs)
        except Exception as exc:  # tushare 在限流/网络抖动时抛通用异常
            if attempt == MAX_ATTEMPTS - 1:
                raise PanelFetchError(f"{endpoint.__name__} 连续 {MAX_ATTEMPTS} 次失败: {exc}") from exc
            time.sleep(2.0 * (attempt + 1))
            continue
        return got if got is not None else pd.DataFrame()
    return pd.DataFrame()


def trade_dates(pro, start: str, end: str) -> list[str]:
    # trade_cal 收到 2026-07-01 这种带横线的日期不会报错，而是静默返回残缺区间
    # （实测 2018-01-01~2026-06-30 少 116 天，2026-07-01~2026-07-29 直接为空）。
    start, end = start.replace("-", ""), end.replace("-", "")
    cal = _call(pro.trade_cal, exchange="SSE", start_date=start, end_date=end, is_open="1")
    if cal.empty:
        raise PanelFetchError(f"交易日历为空: {start}~{end}")
    return sorted(str(d) for d in cal["cal_date"])


def fetch_universe(pro) -> pd.DataFrame:
    """存续 + 已退市 + 暂停上市的并集，带上市/退市日期。"""
    frames = []
    for status in ("L", "D", "P"):
        got = _call(pro.stock_basic, list_status=status, fields="ts_code,symbol,name,list_date,delist_date")
        if not got.empty:
            frames.append(got.assign(list_status=status))
    if not frames:
        raise PanelFetchError("stock_basic 全部为空")
    out = pd.concat(frames, ignore_index=True)
    return out[out["symbol"].astype(str).str.isdigit()].reset_index(drop=True)


NAME_PAGE_SIZE = 5000
NAME_MAX_ROUNDS = 5


def fetch_name_history(pro, *, progress: Callable[[str], None] = logger.info) -> pd.DataFrame:
    """历史证券简称区间。用它还原 PIT 的 ST 状态，避免按今天的名字去过滤历史。

    `namechange` 不接受逗号分隔的 ts_code 列表（会静默返回空），只能整表分页。分页顺序在服务端
    不稳定，单轮会漏掉几个百分点，因此重复整轮取并集，直到一轮不再带来新行。
    """
    seen = pd.DataFrame(columns=["ts_code", "name", "start_date", "end_date"])
    for round_index in range(NAME_MAX_ROUNDS):
        frames = [seen]
        offset = 0
        while True:
            got = _call(
                pro.namechange,
                fields="ts_code,name,start_date,end_date",
                limit=NAME_PAGE_SIZE,
                offset=offset,
            )
            if got.empty:
                break
            frames.append(got)
            offset += NAME_PAGE_SIZE
            if len(got) < NAME_PAGE_SIZE:
                break
        merged = pd.concat(frames, ignore_index=True).drop_duplicates()
        gained = len(merged) - len(seen)
        seen = merged
        progress(f"namechange 第 {round_index + 1} 轮：累计 {len(seen)} 条（新增 {gained}）")
        if gained == 0:
            break
    seen["end_date"] = seen["end_date"].fillna("99999999")
    return seen.reset_index(drop=True)


def _fetch_one_day(pro, day: str) -> pd.DataFrame:
    daily = _call(pro.daily, trade_date=day, fields=DAILY_FIELDS)
    if daily.empty:
        return daily
    adj = _call(pro.adj_factor, trade_date=day, fields="ts_code,trade_date,adj_factor")
    basic = _call(pro.daily_basic, trade_date=day, fields=BASIC_FIELDS)
    out = daily
    for extra in (adj, basic):
        if not extra.empty:
            out = out.merge(extra.drop(columns=["trade_date"]), on="ts_code", how="left")
    return out


def _compact(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    out["symbol"] = out["ts_code"].str.slice(0, 6).astype("int32")
    out["date"] = pd.to_datetime(out["trade_date"], format="%Y%m%d")
    out = out.drop(columns=["ts_code", "trade_date"])
    for column in FLOAT32_COLUMNS:
        if column in out.columns:
            out[column] = pd.to_numeric(out[column], errors="coerce").astype("float32")
    return out.sort_values(["date", "symbol"], ignore_index=True)


def fetch_panel_slice(pro, days: Iterable[str], *, workers: int = 8) -> pd.DataFrame:
    """并发按日横切取数。限流器是进程级的，线程只用来盖住网络延迟。"""
    days = list(days)
    frames: list[pd.DataFrame] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_fetch_one_day, pro, day): day for day in days}
        for done in as_completed(futures):
            got = done.result()
            if not got.empty:
                frames.append(got)
    if not frames:
        raise PanelFetchError(f"面板切片为空: {days[0]}~{days[-1]}")
    return _compact(pd.concat(frames, ignore_index=True))


def build_panel(
    start: str,
    end: str,
    out_dir: Path,
    *,
    workers: int = 8,
    progress: Callable[[str], None] = logger.info,
) -> Path:
    """按年落盘，已存在的年份直接跳过，长任务中断后可以续跑。"""
    pro = get_pro()
    if pro is None:
        raise PanelFetchError("未配置 TUSHARE_TOKEN")
    out_dir.mkdir(parents=True, exist_ok=True)

    days = trade_dates(pro, start, end)
    progress(f"交易日 {len(days)} 天：{days[0]}~{days[-1]}")

    universe = fetch_universe(pro)
    universe.to_parquet(out_dir / "universe.parquet", index=False)
    names = fetch_name_history(pro, progress=progress)
    names.to_parquet(out_dir / "name_history.parquet", index=False)
    progress(f"股票池 {len(universe)} 只（含退市 {(universe['list_status'] == 'D').sum()}），名称变更 {len(names)} 条")

    by_year: dict[str, list[str]] = {}
    for day in days:
        by_year.setdefault(day[:4], []).append(day)
    for year, year_days in sorted(by_year.items()):
        target = out_dir / f"panel_{year}.parquet"
        if target.exists():
            progress(f"{year} 已存在，跳过")
            continue
        frame = fetch_panel_slice(pro, year_days, workers=workers)
        frame.to_parquet(target, index=False)
        progress(f"{year}: {len(frame)} 行 / {frame['symbol'].nunique()} 只 → {target.name}")
    return out_dir


def load_panel(panel_dir: Path, *, columns: list[str] | None = None) -> pd.DataFrame:
    parts = sorted(panel_dir.glob("panel_*.parquet"))
    if not parts:
        raise FileNotFoundError(f"{panel_dir} 下没有 panel_*.parquet")
    frame = pd.concat([pd.read_parquet(p, columns=columns) for p in parts], ignore_index=True)
    return frame.sort_values(["date", "symbol"], ignore_index=True)


def st_flags(names: pd.DataFrame, panel_dates: pd.Series) -> pd.DataFrame:
    """展开成 (date, symbol) -> is_st 的 PIT 标记。"""
    if names.empty:
        return pd.DataFrame(columns=["date", "symbol", "is_st"])
    st = names[names["name"].str.upper().str.contains("ST", na=False)].copy()
    if st.empty:
        return pd.DataFrame(columns=["date", "symbol", "is_st"])
    st["symbol"] = st["ts_code"].str.slice(0, 6).astype("int32")
    st["start"] = pd.to_datetime(st["start_date"], format="%Y%m%d", errors="coerce")
    st["end"] = pd.to_datetime(st["end_date"].replace("99999999", "20991231"), format="%Y%m%d", errors="coerce")
    unique_dates = pd.Series(sorted(pd.unique(panel_dates)), name="date")
    spans = st.dropna(subset=["start"])
    rows = [
        pd.DataFrame({"date": unique_dates[unique_dates.between(row.start, row.end)], "symbol": row.symbol})
        for row in spans.itertuples()
    ]
    if not rows:
        return pd.DataFrame(columns=["date", "symbol", "is_st"])
    out = pd.concat(rows, ignore_index=True).drop_duplicates()
    out["is_st"] = True
    return out
