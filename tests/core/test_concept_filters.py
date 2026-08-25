from core.concept_filters import is_actionable_theme_name, is_etf_code, is_user_facing_etf


def test_etf_themes_and_fund_codes_are_hidden_from_user_facing_lists() -> None:
    assert is_etf_code("159919")
    assert is_etf_code("510300")
    assert is_etf_code("560010")
    assert not is_etf_code("000001")
    assert is_user_facing_etf("159919", "沪深300ETF")
    assert is_user_facing_etf("", "黄金ETF")
    assert is_user_facing_etf("", "粮食ETF")
    assert not is_user_facing_etf("000001", "平安银行")
    assert not is_actionable_theme_name("黄金ETF")
    assert not is_actionable_theme_name("粮食ETF")
    assert is_actionable_theme_name("机器人")
