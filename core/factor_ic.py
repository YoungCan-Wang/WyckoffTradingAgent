"""因子 IC 评估：判断一个指标对未来收益到底有没有预测力。

## 为什么需要它

生产漏斗全是**阈值门**（`RPS >= 75`、`涨幅 >= 15%`、`量能分位 <= 20%`），
`rank(axis=1)` 在 core/ 零命中——没有任何横截面排序。这带来三个后果，
2026-08-22 的回测全部印证：

1. **门槛线上的票是随机的**：复盘里出现 `RPS 69.3 差 0.7`、`涨幅 14.8% 差 0.2%`、
   `最小量差 31 手`，它们与刚过线的票无本质差别，却一个进一个不进。
   放宽阈值会同时放进一大批线上标的，故召回与精度无法同时改善。
2. **参数拟合必然过拟合**：walk-forward 仅 1/16 个测试窗口为正。每个阈值都是在
   历史上找的最优切点，样本外自然失效。
3. **只能过滤、不能排序**：系统能说「这 84 只符合条件」，说不出「哪只最好」，
   而实盘每天只买 1~2 只。

IC（Information Coefficient）用因子值与未来收益的横截面**秩相关**衡量预测力，
不需要切点，因此不存在上面第 1、2 个问题，并天然给出排序。

## 判读口径

- `rank_ic`：每个截面日算一次 Spearman 秩相关，再对日取均值。A 股日频选股中
  |IC| 0.02~0.05 即有实用价值，0.05 以上算强。
- `ic_ir` = IC 均值 / IC 标准差：稳定性。这比 IC 绝对值更重要——IC 高但飘忽的因子
  无法下注。经验上 |IC_IR| >= 0.3 才值得进模型。
- `positive_ratio`：IC 为正的日子占比。落在 45%~55% 视为无方向（与 AGENTS.md
  的噪声判定一致），此时无论 IC 均值多少都不采用。
- `monotonicity`：按因子值分 N 组，组均收益的秩与组序号的相关性。单调性差说明
  关系非线性，加权求和的线性合成会失效。

单期 IC 不构成证据：必须看 `ic_ir` 与 `positive_ratio`，并按 AGENTS.md 规则 7
用滚动窗口确认，而非在全样本上挑最优。
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import mean, stdev

# IC 为正的日子占比落在此区间视为无方向性。
RANDOM_BAND = (45.0, 55.0)
# 低于此值的截面样本数不参与计算——横截面秩相关需要足够宽度。
MIN_CROSS_SECTION = 100
# 少于此天数不出结论。
MIN_DAYS = 60
# 实用性门槛：经验值，用于自动给出「是否值得进模型」的初判。
USEFUL_ABS_IC = 0.02
USEFUL_ABS_IC_IR = 0.30


@dataclass(frozen=True)
class FactorICResult:
    name: str
    horizon: int
    days: int
    rank_ic: float | None
    ic_std: float | None
    positive_ratio: float | None
    monotonicity: float | None
    avg_universe: float

    @property
    def ic_ir(self) -> float | None:
        """IC 的信息比率。标准差为 0 时无意义，返回 None 而非无穷。"""
        if self.rank_ic is None or self.ic_std is None or self.ic_std <= 0:
            return None
        return self.rank_ic / self.ic_std

    @property
    def directionless(self) -> bool:
        if self.positive_ratio is None:
            return True
        return RANDOM_BAND[0] <= self.positive_ratio <= RANDOM_BAND[1]

    @property
    def verdict(self) -> str:
        if self.days < MIN_DAYS or self.rank_ic is None:
            return "样本不足"
        if self.directionless:
            return "无方向性"
        ir = self.ic_ir
        strong = abs(self.rank_ic) >= USEFUL_ABS_IC and ir is not None and abs(ir) >= USEFUL_ABS_IC_IR
        side = "正向" if self.rank_ic > 0 else "反向"
        return f"{side}·可用" if strong else f"{side}·偏弱"

    @property
    def useful(self) -> bool:
        """是否值得进合成模型。反向因子取负号后同样可用。"""
        if self.days < MIN_DAYS or self.rank_ic is None or self.directionless:
            return False
        ir = self.ic_ir
        return abs(self.rank_ic) >= USEFUL_ABS_IC and ir is not None and abs(ir) >= USEFUL_ABS_IC_IR

    @property
    def sign(self) -> int:
        """合成时的方向：+1 直接用，-1 需取负。"""
        if self.rank_ic is None:
            return 0
        return 1 if self.rank_ic > 0 else -1

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "horizon": self.horizon,
            "days": self.days,
            "avg_universe": round(self.avg_universe, 1),
            "rank_ic": _r(self.rank_ic),
            "ic_std": _r(self.ic_std),
            "ic_ir": _r(self.ic_ir),
            "positive_ratio": _r(self.positive_ratio, 1),
            "monotonicity": _r(self.monotonicity),
            "verdict": self.verdict,
            "useful": self.useful,
            "sign": self.sign,
        }


def _r(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(float(value), digits)


def summarize_ic(
    name: str,
    horizon: int,
    daily_ic: list[float],
    daily_universe: list[float],
    daily_monotonicity: list[float] | None = None,
) -> FactorICResult:
    """把逐日 IC 汇总成结论。按日等权——截面宽的日子不该主导均值。"""
    clean = [v for v in daily_ic if v is not None and v == v]
    if len(clean) < MIN_DAYS:
        return FactorICResult(name, horizon, len(clean), None, None, None, None, mean(daily_universe or [0]))
    mono = None
    if daily_monotonicity:
        mono_clean = [v for v in daily_monotonicity if v is not None and v == v]
        mono = mean(mono_clean) if mono_clean else None
    return FactorICResult(
        name=name,
        horizon=horizon,
        days=len(clean),
        rank_ic=mean(clean),
        ic_std=stdev(clean) if len(clean) > 1 else None,
        positive_ratio=100.0 * sum(1 for v in clean if v > 0) / len(clean),
        monotonicity=mono,
        avg_universe=mean(daily_universe) if daily_universe else 0.0,
    )


def composite_weights(results: list[FactorICResult]) -> dict[str, float]:
    """按 |IC_IR| 给可用因子分配权重，并带上方向符号。

    用 IC_IR 而非 IC 加权：稳定性比幅度更重要，IC 高但飘忽的因子下不了注。
    权重归一化到绝对值和为 1，便于与等权基线对比。
    """
    usable = [r for r in results if r.useful and r.ic_ir is not None]
    total = sum(abs(r.ic_ir) for r in usable if r.ic_ir is not None)
    if total <= 0:
        return {}
    return {r.name: round(r.sign * abs(r.ic_ir) / total, 6) for r in usable if r.ic_ir is not None}
