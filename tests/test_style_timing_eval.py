"""风格择时体检的判别性用例。

要点:一个回归测试如果对「植入真信号」和「纯噪声」给同一句判定,它就没测到东西。
所以这里三种输入必须落到三种不同判定上——植入信号=过对照,纯噪声=无信息,
单月撑起=不成立。
"""

from __future__ import annotations

import random

from core.style_timing_eval import (
    MIN_DAYS,
    DayRow,
    evaluate_style_timing,
)

MONTHS = ("2026-05", "2026-06", "2026-07", "2026-08")
DAYS_PER_MONTH = 20


def _dates() -> list[str]:
    return [f"{m}-{d:02d}" for m in MONTHS for d in range(1, DAYS_PER_MONTH + 1)]


def _row(
    date: str,
    trail: float,
    forward: float,
    *,
    market_trail: float | None = None,
    market_forward: float | None = None,
) -> DayRow:
    return DayRow(
        date=date,
        pool_trail={5: trail, 10: trail, 20: trail},
        pool_forward=forward,
        pool_win=None,
        n_pool=30,
        market_trail={5: market_trail if market_trail is not None else trail} | {10: 0.0, 20: 0.0},
        market_forward=market_forward if market_forward is not None else 0.0,
        n_market=200,
    )


def test_planted_signal_passes_all_controls() -> None:
    """强弱与前瞻同向、且每个月都同向时,应当判为过对照。"""
    rng = random.Random(11)
    rows = [_row(d, trail := rng.uniform(-10, 10), trail * 0.8 + rng.gauss(0, 1.0)) for d in _dates()]
    report = evaluate_style_timing(rows, horizon=5)
    block = report.windows["trail5"]
    assert block["full_sample"]["spread"] is not None
    assert block["full_sample"]["spread"] > 0
    assert block["verdict"] == "过全部对照", block


def test_pure_noise_reads_as_no_information() -> None:
    """切档键与前瞻无关时,必须落回置换带内,而不是给出结论。

    这一例特意留着:它的相位 |t| 高达 5.8,单看 t 会直接放行,是月内置换把它挡住的。
    换句话说,判定链里的置换不是装饰——去掉它,纯噪声就会读成「过全部对照」。
    """
    rng = random.Random(23)
    rows = [_row(d, rng.uniform(-10, 10), rng.gauss(0, 5.0)) for d in _dates()]
    report = evaluate_style_timing(rows, horizon=5)
    block = report.windows["trail5"]
    assert abs(block["phase_scan"]["t"]) > 2.5, "这一例的价值在于 t 够大,变小了就测不到东西"
    assert block["verdict"] == "落在月内置换带内,无信息", block["verdict"]


def test_single_month_carrying_everything_is_rejected() -> None:
    """只有一个月有关系、其余三个月纯噪声,应当判为单月撑起全部。

    这是回看 20 日实际死掉的方式:全样本 p 更漂亮,剔掉那个月就只剩零头。
    """
    rng = random.Random(37)
    rows = []
    for date in _dates():
        trail = rng.uniform(-10, 10)
        if date.startswith("2026-07"):
            forward = trail * 3.0
        else:
            forward = rng.gauss(0, 0.5)
        rows.append(_row(date, trail, forward))
    report = evaluate_style_timing(rows, horizon=5)
    assert report.windows["trail5"]["verdict"] == "单月撑起全部,不成立"


def test_market_control_flags_market_timing() -> None:
    """池子与市场同幅同向时,应当判成市场择时而不是风格择时。"""
    rng = random.Random(41)
    rows = []
    for date in _dates():
        trail = rng.uniform(-10, 10)
        shared = trail * 0.8 + rng.gauss(0, 1.0)
        rows.append(_row(date, trail, shared, market_trail=trail, market_forward=shared))
    report = evaluate_style_timing(rows, horizon=5)
    assert "市场择时" in report.windows["trail5"]["verdict"]


def test_insufficient_sample_is_not_a_failed_control() -> None:
    """天数不够时要明确说样本不足——「尚未可知」不能写成「对照未通过」。"""
    rng = random.Random(53)
    rows = [_row(f"2026-05-{d:02d}", trail := rng.uniform(-10, 10), trail) for d in range(1, MIN_DAYS)]
    report = evaluate_style_timing(rows, horizon=5)
    assert report.windows["trail5"]["verdict"] == "样本不足,不下判定"


def test_negative_finding_keeps_sign_consistency() -> None:
    """全样本为负时,「留一月同号」要按负方向数,不能按正号数。"""
    rng = random.Random(67)
    rows = [_row(d, trail := rng.uniform(-10, 10), -trail * 0.8 + rng.gauss(0, 1.0)) for d in _dates()]
    report = evaluate_style_timing(rows, horizon=5)
    block = report.windows["trail5"]
    assert block["full_sample"]["spread"] < 0
    lomo = block["leave_one_month_out"]
    assert lomo["same_sign"] == f"{len(MONTHS)}/{len(MONTHS)}"
    assert lomo["positive"] == f"0/{len(MONTHS)}"


def test_eval_window_comes_from_usable_days() -> None:
    """区间必须来自真正可用的日子,不能来自请求区间。"""
    rows = [_row(d, 1.0, 1.0) for d in _dates()]
    rows.append(
        DayRow(
            date="2026-09-30",
            pool_trail={5: 1.0, 10: 1.0, 20: 1.0},
            pool_forward=None,  # 前瞻不足,这天不该进区间
            pool_win=None,
            n_pool=30,
        )
    )
    report = evaluate_style_timing(rows, horizon=5)
    assert report.eval_window == ["2026-05-01", "2026-08-20"]
    assert "2026-09" not in report.months


def test_permutation_outside_agrees_with_its_own_p_value() -> None:
    """outside 与 p_value 必须同源,否则判定和报告里的数会互相矛盾。

    真实数据上出现过:有符号分位带判「带内」、双侧绝对值算出 p=0.03,判定读 outside,
    于是报告里写着「置换 p=0.03」却判「落在带内,无信息」。零分布一偏就会这样。
    这里扫多组随机输入,逐个钉住 outside == (p_value < 0.05)。
    """
    for seed in range(12):
        rng = random.Random(seed)
        rows = [_row(d, rng.uniform(-10, 10), rng.gauss(0, 5.0)) for d in _dates()]
        report = evaluate_style_timing(rows, horizon=5)
        for name, block in report.windows.items():
            perm = block["month_block_permutation"]
            if "p_value" not in perm:
                continue
            assert perm["outside"] is (perm["p_value"] < 0.05), (
                f"seed={seed} {name}: outside={perm['outside']} 与 p={perm['p_value']} 不一致"
            )
