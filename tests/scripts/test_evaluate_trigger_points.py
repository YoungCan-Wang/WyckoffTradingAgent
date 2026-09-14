"""走前挑表的四张候选表本身。

``core/trigger_points_eval.py`` 那侧的测试钉的是**判据**（三闸怎么算、贴线不四舍五入）。
但判据全部作用在 ``candidate_tables()`` 造出来的表上，这四张表自己没有测试钉过，而它们
各自都有一条**静默失效**路径：

- ``flat`` 的存在意义是自由参数 0 个 —— ``walk_forward_narrow`` 整段论证（收窄到两方后
  问题从「换成哪 6 个」变成「这 6 个值不值」）全押在这一条上。哪天它退化成「只拍平六个
  分值、sos 两档留着」，narrow 那格就变成 prod 跟一张近似 prod 的表相比，读出来是「不显著
  → 维持生产」—— 一个长得跟正常证据一模一样的假阴性。
- ``no_res`` 只该动共振那一档。多动一格，「只动全表最大的那次加分」这个归因就不成立。
- ``by_excess`` 是置换带的样本内最优端，前提是它只重排、不改量级。一旦引入新数字，它跟
  置换检验就不再同构，而报告仍会把它当「最优端」读。
"""

from __future__ import annotations

from core.trigger_points_eval import (
    FLAT_POINTS,
    PROD_POINTS,
    PROD_SOS_RESONANT,
    PROD_SOS_SINGLE,
    TRIGGER_KINDS,
)
from scripts.evaluate_trigger_points import candidate_tables

# 六个类型的实测超额全为负（首轮实测如此），sos 最差 —— 触发 by_excess 里 sos 降档那条分支。
KINDS_EXCESS = {
    "sos": -0.704,
    "spring": -0.299,
    "lps": -0.412,
    "evr": -0.350,
    "compression": -0.121,
    "trend_pullback": -0.490,
}


def test_prod_table_mirrors_production_constants() -> None:
    prod = candidate_tables(KINDS_EXCESS)["prod"]
    assert prod["points"] == PROD_POINTS
    assert prod["sos_single"] == PROD_SOS_SINGLE
    assert prod["sos_resonant"] == PROD_SOS_RESONANT


def test_prod_table_is_a_copy_not_the_module_constant() -> None:
    """改一张候选表不能反过来污染生产常量 —— 同一轮里四张表都读它。"""
    prod = candidate_tables(KINDS_EXCESS)["prod"]
    prod["points"]["spring"] = 999.0
    assert PROD_POINTS["spring"] != 999.0


def test_points_dict_covers_every_kind_except_sos() -> None:
    """sos 是唯一分两档的类型，故不进 ``points``。

    ``_by_excess_table`` 按 ``PROD_POINTS`` 迭代来重排。哪天 sos 被搬进 ``points``、
    或某一类被漏掉，重排就作用在一个不同的集合上，而「只重排不改量级」的说法仍然成立
    —— 报告读不出差别。
    """
    assert set(PROD_POINTS) == set(TRIGGER_KINDS) - {"sos"}


def test_flat_table_has_zero_free_parameters() -> None:
    """拍平表的全部分值（含 sos 两档）必须是同一个常数。

    这是 narrow 那格「自由参数 0 个」的字面含义：常数取多少不影响已触发票之间的排序，
    等于把这张表删掉。任何一档没跟上，narrow 就退化成 prod vs 近似 prod。
    """
    flat = candidate_tables(KINDS_EXCESS)["flat"]
    values = {*flat["points"].values(), flat["sos_single"], flat["sos_resonant"]}
    assert values == {FLAT_POINTS}
    assert set(flat["points"]) == set(PROD_POINTS)


def test_no_res_only_lowers_the_resonant_tier() -> None:
    no_res = candidate_tables(KINDS_EXCESS)["no_res"]
    assert no_res["points"] == PROD_POINTS
    assert no_res["sos_single"] == PROD_SOS_SINGLE
    # 共振档降回单独档 = 去掉全表最大的那次加分（+35），其余逐位不动。
    assert no_res["sos_resonant"] == PROD_SOS_SINGLE


def test_by_excess_only_reshuffles_the_production_value_multiset() -> None:
    """重排、不改量级 —— 否则它与置换检验不再同构。"""
    points = candidate_tables(KINDS_EXCESS)["by_excess"]["points"]
    assert sorted(points.values()) == sorted(PROD_POINTS.values())
    assert set(points) == set(PROD_POINTS)


def test_by_excess_gives_the_worst_kind_the_lowest_points() -> None:
    """排序只在 ``points`` 那五类之间，sos 走自己的两档，不参与重排。"""
    points = candidate_tables(KINDS_EXCESS)["by_excess"]["points"]
    scored = {k: v for k, v in KINDS_EXCESS.items() if k in PROD_POINTS}
    worst = min(scored, key=lambda k: scored[k])
    best = max(scored, key=lambda k: scored[k])
    assert points[worst] == min(PROD_POINTS.values())
    assert points[best] == max(PROD_POINTS.values())


def test_by_excess_collapses_sos_tiers_when_sos_is_worst() -> None:
    """sos 超额最差时两档同时降到最低 —— 共振加分方向已被实测判为反的。"""
    table = candidate_tables(KINDS_EXCESS)["by_excess"]
    assert table["sos_single"] == min(PROD_POINTS.values())
    assert table["sos_resonant"] == table["sos_single"]


def test_by_excess_keeps_production_sos_single_when_sos_is_not_worst() -> None:
    """sos 不是最差时单独档不动 —— 降档的依据是「它最差」，不是「它是 sos」。"""
    excess = {**KINDS_EXCESS, "sos": max(KINDS_EXCESS.values()) + 0.1}
    table = candidate_tables(excess)["by_excess"]
    assert table["sos_single"] == PROD_SOS_SINGLE
    # 共振档仍然跟着单独档走：这张表不保留 50 那一档。
    assert table["sos_resonant"] == PROD_SOS_SINGLE


def test_by_excess_falls_back_to_production_when_a_kind_is_missing() -> None:
    """缺一类超额就整表退回生产：半张重排表会被走前当成一个独立候选去挑。"""
    partial = {k: v for k, v in KINDS_EXCESS.items() if k != "lps"}
    table = candidate_tables(partial)["by_excess"]
    assert table["points"] == PROD_POINTS
    assert table["sos_single"] == PROD_SOS_SINGLE
    assert table["sos_resonant"] == PROD_SOS_RESONANT


def test_all_four_tables_are_offered() -> None:
    """走前那格的候选集就是这四张；漏一张会让 pick_dist 的分母静默变小。"""
    assert set(candidate_tables(KINDS_EXCESS)) == {"prod", "flat", "no_res", "by_excess"}
