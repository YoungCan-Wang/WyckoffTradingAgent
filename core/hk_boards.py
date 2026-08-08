"""HK-share board classification helpers.

HK stock codes follow ``NNNNN.HK`` format (5-digit number + ``.HK`` suffix).
Board membership is determined by the numeric prefix:
  - 0xxxx / 1xxxx / 2xxxx / 3xxxx / 4xxxx / 5xxxx / 6xxxx / 7xxxx / 8xxxx / 9xxxx → main board
  - 8xxxx where the numeric part < 8000 is historically GEM-listed
    (GEM codes were migrated to main-board 5-digit format in 2008;
     current GEM stocks have been reassigned, so we rely on an
     explicit GEM set loaded from ``data/market_universes/hk_gem.txt`` if available).

In practice the distinction that matters most for Wyckoff analysis is
*main-board liquid* vs *illiquid micro-cap*; the latter is already
filtered by ``min_quote_amount`` / ``min_quote_price`` at the data layer.
"""

from __future__ import annotations

from pathlib import Path

from core.wyckoff_engine import FunnelConfig

_GEM_CODES: set[str] | None = None
_GEM_FILE = Path(__file__).resolve().parents[1] / "data" / "market_universes" / "hk_gem.txt"


def _load_gem_codes() -> set[str]:
    if not _GEM_FILE.exists():
        return set()
    codes: set[str] = set()
    for line in _GEM_FILE.read_text(encoding="utf-8").splitlines():
        clean = line.split("#", 1)[0].strip().upper()
        if clean:
            codes.add(clean)
    return codes


def hk_board(code: object) -> str:
    """Return board classification for an HK stock code.

    Returns one of: ``"main"``, ``"gem"``, ``"foreign"``, ``"unknown"``.
    """
    text = str(code or "").strip().upper()
    if text.endswith(".HK"):
        num = text[:-3]
    else:
        num = text

    if not num or not num.isdigit():
        return "unknown"

    global _GEM_CODES
    if _GEM_CODES is None:
        _GEM_CODES = _load_gem_codes()

    if text in (_GEM_CODES or set()):
        return "gem"

    if len(num) == 5 and num.startswith("8") and int(num) < 8000:
        return "gem"

    return "main"


def is_hk_main_board(code: object) -> bool:
    return hk_board(code) == "main"


# 2026-07 复盘 `recommendation_tracking_hk` 近 30 个交易日 312 条真实候选：整体胜率
# 42.6%、均收 -3.45%（系统性亏损）。分信号归因：SOS 胜率 21.6%／均收 -8.96%（曹操
# 出行 -57.8%、中国建材 -44.8%、嘀嗒出行反复 -43%+ 均由此触发）；EVR 胜率 35.8%／
# 均收 -3.40%；TrendPB 胜率 18.8%／均收 -11.06%（佑驾创新单只标的反复触发 3 次全部
# 亏损 -27%~-28%）；Compression 样本虽小但同样负期望；唯一正期望的是 Spring，胜率
# 81.8%／均收 +1.64%。用 350+ 只港股真实历史做 walk-forward 复核（15 日持有期，覆盖
# EVR 多组量比/滞涨阈值网格）后确认 EVR 在任何参数组合下都是负期望（-5.5%~-9.8%），
# 越收紧胜率越低（最严格组合胜率降到 0%），说明这不是阈值没调对，而是 EVR「放量滞涨
# =吸筹」的判断逻辑在港股数据源上本身不成立。根因有二：1) TickFlow 港股 K 线没有
# `turnover` 字段、`amount` 恒为 0，EVR 的换手率二次确认（`evr_min_turnover`）在
# 港股完全失效，「滞涨」经常是財技股股价被人为托住而非真实吸筹，调阈值无法修复这个
# 结构性缺陷；2) 港股漏斗没有市值分层（`market_cap_map` 为空时跳过市值过滤），大盘
# 蓝筹（汇丰、渣打、长江基建）与高波动题材股（网约车、脑科学、自动驾驶芯片）共用同
# 一套参数，后者贡献了几乎全部巨亏样本。再用三周期（2020 牛市/2022 熊市/近 6 月）
# walk-forward 网格对 TrendPB/Compression 做消融测试（修复 `workflows/backtest_
# data.py::board_match` 对 `.HK` 代码的漏识别 bug 后才能正确跑通港股回测）：同时关闭
# 两者在三个周期均不差于基线（牛市 cash_return 9.79%→11.43%、sharpe 1.35→1.48），
# 单独调紧 TrendPB 缩量确认或收紧 SOS 到 11%/4.5x 均与基线完全一致，说明这两个信号
# 在当前 `top_n=1` 口径下已很少真正被选中执行，调参无法改善，直接停用不会牺牲收益。
def apply_hk_funnel_cfg(cfg: FunnelConfig, *, min_avg_amount: float = 0.0) -> None:
    """港股漏斗参数调优——集中一处，供漏斗任务和回测网格共用。

    历史复盘依据见函数上方注释；核心结论：EVR 换手率过滤在港股数据源上结构性
    失效，TrendPB/Compression 实盘负期望且在 walk-forward 消融测试中关闭后
    跨周期收益不降反升，三者均已停用；SOS 收紧到强势尾部；Spring 是唯一正
    期望信号，适度放宽以扩大样本；用更高的日均成交额门槛替代缺失的市值分层，
    挡住低流动性题材股。
    """
    # SOS：换手率二次确认在港股失效，只能靠更高的涨幅+量能+突破幅度三重收紧
    # 压缩到真正的强势尾部（原 7%/3.0x 下 34 个样本仍有 82.4% 是亏损单；
    # walk-forward 复核显示收紧到 9%/4.0x 后胜率从约 50% 回升到 66.7%）。
    cfg.sos_pct_min = 9.0
    cfg.sos_vol_ratio = 4.0
    # Spring：日内振幅可以很大但 25% 以内才算有效回踩（超过多为老千股操纵）；
    # 这是唯一正期望信号（胜率 81.8%），适度放宽放量门槛以扩大样本覆盖。
    cfg.spring_tr_max_range_pct = 25.0
    cfg.spring_vol_ratio = 1.3
    # 年线偏离：南向龙头长期高偏离是常态
    cfg.global_entry_max_bias_200 = 35.0
    cfg.trend_entry_max_bias_200 = 40.0
    # 吸筹通道：GEM 仙股横盘是陷阱，收严位阶和振幅
    cfg.accum_price_from_low_max = 0.40
    cfg.accum_range_max_pct = 25.0  # 收严：横盘振幅不超过 25%（老千股常 30%+ 振荡洗盘）
    # EVR：换手率过滤失效导致该信号在港股结构性负期望，网格调参（vol_ratio
    # 1.8~3.0、max_rise/max_drop 1.0~3.0）在所有组合下均值均为负，直接停用。
    cfg.enable_evr_trigger = False
    # TrendPB：实盘胜率 18.8%／均收 -11.06%（佑驾创新反复触发 3 次全部亏损），
    # walk-forward 消融测试显示关闭后三周期收益不降反升，收紧参数无效，停用。
    cfg.enable_trend_pullback_trigger = False
    # Compression：样本虽小但同为负期望，且与 TrendPB 高度共线（同属趋势延续
    # 类信号），消融测试显示同步停用后跨周期表现进一步改善，一并停用。
    cfg.enable_compression_trigger = False
    # LPS：缩量回踩在港股有效性更高（做空盘枯竭后自然缩量），缩量比适度放宽
    cfg.lps_vol_dry_ratio = 0.55
    # RPS：港股结构性行情更极端，RPS 门槛适度降低以捕获南向资金突然涌入的标的
    cfg.rps_fast_min = 60.0
    cfg.rps_slow_min = 65.0
    # RS：港股大盘（恒指）成分股权重极度集中，RS 门槛适度放宽
    cfg.rs_min_long = 1.5
    cfg.rs_min_short = 0.5
    # 地量通道：港股仙股常出现虚假地量（无人交易），收严位阶保护
    cfg.dry_vol_price_from_low_max = 0.30
    # RS 背离通道：港股更容易出现"指数跌但个股不跌"（南向托底），保持启用
    cfg.enable_rs_divergence_channel = True
    cfg.rs_div_price_from_low_max = 0.45
    # 日均成交额：无市值分层可用，用更高的流动性门槛间接过滤低流动性题材股
    # （复盘显示巨亏样本集中在网约车/脑科学/自动驾驶芯片等高波动小盘题材），
    # 原 800 万门槛过低，抬高到 2000 万（≈ 2000 万港元）。
    cfg.min_avg_amount_wan = max(min_avg_amount / 10000.0, 2000.0)
    # 收盘价底线：低于 1 港元进入仙股区域，风险门禁已兜底，此处额外加码
    cfg.l1_min_close_price = 1.0
