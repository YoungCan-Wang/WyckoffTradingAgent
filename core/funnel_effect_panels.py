"""效果检验用的行情面板构建：流动性池与 T 日已知动量。

从 ``scripts/evaluate_funnel_effect.py`` 抽出来的原因不是复用代码行，而是**动量定义
必须只有一处**。同动量对照的全部意义在于「两组的 mom20 用同一个尺子量」；漏斗效果
检验与影子车道效果检验若各自算一遍 20 日涨幅（窗口含不含 T 日、要不要 shift、
用不用复权），得到的邻域就不是同一个邻域，两份报告的对照组无法互相印证，而这种
偏差不会报错、只会静默地把结论读偏（见 memory two-gates-must-share-one-source）。

口径固定为：

- **流动性池**：20 日均额 ``shift(1)``，只含 T 日收盘可知的信息。
- **20 日动量**：按 T 日收盘算（含 T 日），候选与对照同口径，无前视。
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from core.funnel_effect_eval import Panels

# 20 日均额下限（万元）。低于此的票进不了对照池——流动性差的票的收益噪声与
# 候选不可比，把它们放进邻域只会放大控制组的方差。
DEFAULT_MIN_AMOUNT_WAN = 8000.0


def load_market_frame(path: str | Path) -> pd.DataFrame:
    """兼容两种列名：快照的 date/symbol/amount(元)，与 tushare 的 trade_date/ts_code/amount(千元)。"""
    text = str(path)
    compression = "gzip" if text.endswith(".gz") else None
    head = pd.read_csv(text, nrows=1, compression=compression)
    if "symbol" in head.columns:
        frame = pd.read_csv(text, usecols=["date", "open", "close", "amount", "symbol"], compression=compression)
        frame["code"] = frame.symbol.astype(str).str.extract(r"(\d+)")[0].str.zfill(6)
        frame["ds"] = pd.to_datetime(frame.date).dt.strftime("%Y-%m-%d")
        frame["amt_wan"] = pd.to_numeric(frame.amount, errors="coerce") / 1e4
    else:
        frame = pd.read_csv(text, usecols=["ts_code", "trade_date", "open", "close", "amount"], compression=compression)
        frame["code"] = frame.ts_code.astype(str).str.extract(r"(\d+)")[0].str.zfill(6)
        frame["ds"] = pd.to_datetime(frame.trade_date.astype(str), format="%Y%m%d").dt.strftime("%Y-%m-%d")
        frame["amt_wan"] = pd.to_numeric(frame.amount, errors="coerce") / 10.0
    for col in ("open", "close"):
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    return frame.dropna(subset=["open", "close"]).sort_values(["code", "ds"])


def load_benchmark_prices(path: str | Path) -> tuple[dict[str, float], dict[str, float]]:
    """基准指数的 (开盘, 收盘)。窗口要与候选完全一致，所以两头都要。"""
    if not str(path or "").strip() or not Path(path).exists():
        return {}, {}
    frame = pd.read_csv(path, usecols=["date", "open", "close"])
    frame["ds"] = pd.to_datetime(frame.date).dt.strftime("%Y-%m-%d")
    for col in ("open", "close"):
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frame = frame.dropna(subset=["open", "close"]).drop_duplicates("ds")
    return (
        dict(zip(frame.ds, frame.open.astype(float), strict=True)),
        dict(zip(frame.ds, frame.close.astype(float), strict=True)),
    )


def build_panels(
    frame: pd.DataFrame,
    min_amount_wan: float = DEFAULT_MIN_AMOUNT_WAN,
    benchmark: str | Path = "",
) -> Panels:
    """流动性池与 20 日动量都用 shift(1)，只含 T 日收盘可知的信息。"""
    frame = frame.copy()
    grouped = frame.groupby("code", sort=False)
    frame["avg20"] = grouped.amt_wan.transform(lambda s: s.rolling(20, min_periods=10).mean().shift(1))
    # 20 日涨幅按 T 日收盘算（含 T 日），配对时对候选和对照同口径，无前视。
    frame["mom20"] = grouped.close.transform(lambda s: 100.0 * (s / s.shift(20) - 1.0))
    liquid = frame[frame.avg20 >= min_amount_wan]
    bench_open, bench_close = load_benchmark_prices(benchmark)
    return Panels(
        open={ds: dict(zip(g.code, g.open, strict=True)) for ds, g in frame.groupby("ds", sort=False)},
        close={ds: dict(zip(g.code, g.close, strict=True)) for ds, g in frame.groupby("ds", sort=False)},
        liquid={ds: set(g.code) for ds, g in liquid.groupby("ds", sort=False)},
        mom20={
            ds: dict(zip(g.code, g.mom20, strict=True))
            for ds, g in frame.dropna(subset=["mom20"]).groupby("ds", sort=False)
        },
        dates=sorted(frame.ds.unique()),
        bench_open=bench_open,
        bench_close=bench_close,
    )


def build_panels_from_snapshot(
    snapshot_dir: str | Path,
    min_amount_wan: float = DEFAULT_MIN_AMOUNT_WAN,
) -> Panels | None:
    """从回测快照目录建面板。缺 hist_full.csv.gz 返回 None——效果检验降级为不出对照。

    降级而不抛：影子回测的召回率部分不依赖面板，快照缺失时不该把整份报告拖挂。
    但降级必须在报告里显式说明，不能让读者以为「没有对照组一节」等于「对照通过了」。
    """
    root = Path(snapshot_dir)
    market = root / "hist_full.csv.gz"
    if not market.exists():
        return None
    return build_panels(load_market_frame(market), min_amount_wan, root / "benchmark_main.csv")
