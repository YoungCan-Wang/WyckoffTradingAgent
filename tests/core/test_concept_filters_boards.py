"""场内基金判定改用 A 股板段白名单取反：号段黑名单会漏新基金类别。"""

from __future__ import annotations

import pytest

from core.concept_filters import is_etf_code, is_user_facing_etf

# 覆盖全部 A 股交易板段：沪主板、科创、深主板/中小板、创业板、北交所
A_SHARE_CODES = [
    "600660",
    "601318",
    "603296",
    "605288",
    "688301",
    "689009",
    "000523",
    "001979",
    "002648",
    "003816",
    "300628",
    "301008",
    "920088",
    "830799",
    "430047",
    "870508",
]

FUND_CODES = [
    "510300",
    "512880",
    "513050",
    "515790",
    "560000",
    "159915",
    "501000",
]


@pytest.mark.parametrize("code", A_SHARE_CODES)
def test_a_share_codes_are_never_treated_as_funds(code: str) -> None:
    """展示层误把个股判成基金会直接从卡片里抹掉一只候选，比漏一只基金严重。"""
    assert not is_etf_code(code)
    assert not is_user_facing_etf(code, "某公司")


@pytest.mark.parametrize("code", FUND_CODES)
def test_fund_codes_are_filtered(code: str) -> None:
    assert is_etf_code(code)


@pytest.mark.parametrize("code", ["588000", "588080"])
def test_star50_etf_segment_is_covered(code: str) -> None:
    """回归：原号段黑名单只列 159/51/56，588 段的科创 50 ETF 会漏进用户面卡片。"""
    assert is_etf_code(code)


def test_short_codes_are_not_guessed() -> None:
    """不足 6 位无法判板段，不能瞎猜——上游可能只是把代码截断了。"""
    assert not is_etf_code("300")
    assert not is_etf_code("51")
    assert not is_etf_code("")


def test_name_marker_still_works_without_code() -> None:
    assert is_user_facing_etf("", "黄金ETF")
    assert not is_user_facing_etf("", "卫星化学")
