from __future__ import annotations

from core.candidate_preference import (
    candidate_matches_preference,
    candidate_style_match_styles,
    has_style_preference,
    has_theme_preference,
    infer_style_match_styles,
    missing_style_preference_labels,
    preference_match_status,
    style_preference_labels,
    style_preference_match_status,
    style_preference_styles,
    style_preference_text,
    theme_preference_text,
)


def test_has_style_preference_detects_styles_or_raw_text():
    assert has_style_preference({"styles": ["trend"]}) is True
    assert has_style_preference({"raw": " 低吸 "}) is True
    assert has_style_preference({}) is False
    assert has_style_preference(None) is False
    assert has_style_preference("not-a-dict") is False


def test_has_theme_preference_detects_theme_or_raw_text():
    assert has_theme_preference({"theme": "半导体"}) is True
    assert has_theme_preference({"raw": " 新能源 "}) is True
    assert has_theme_preference({}) is False
    assert has_theme_preference(None) is False


def test_style_preference_styles_extracts_and_caps_at_four():
    value = {"styles": ["trend", "pullback", "quality", "trend", "extra"]}
    assert style_preference_styles(value) == ["trend", "pullback", "quality", "trend"]
    assert style_preference_styles({}) == []
    assert style_preference_styles(None) == []


def test_style_preference_labels_maps_known_styles_to_chinese():
    assert style_preference_labels({"styles": ["trend", "pullback", "quality"]}) == ["趋势", "低吸", "质量"]
    assert style_preference_labels({"styles": ["unknown"]}) == ["unknown"]


def test_candidate_matches_preference_true_when_flag_set():
    assert candidate_matches_preference({"style_match": True}, "style") is True


def test_candidate_matches_preference_true_when_score_positive():
    assert candidate_matches_preference({"style_match_score": "2"}, "style") is True
    assert candidate_matches_preference({"style_match_score": "not-a-number"}, "style") is False


def test_candidate_matches_preference_true_when_reasons_present():
    assert candidate_matches_preference({"style_match_reasons": ["趋势偏好符合"]}, "style") is True


def test_candidate_matches_preference_false_when_no_signal():
    assert candidate_matches_preference({}, "style") is False


def test_preference_match_status_hit_when_any_row_matches():
    rows = [{"style_match": False}, {"style_match": True}]
    assert preference_match_status(rows, "style") == "hit"


def test_preference_match_status_miss_when_no_row_matches():
    rows = [{"style_match": False}, {}]
    assert preference_match_status(rows, "style") == "miss"


def test_infer_style_match_styles_reads_reason_prefixes():
    row = {"style_match_reasons": ["趋势偏好符合走势", "低吸偏好符合位置", "稳健偏好符合波动"]}
    assert infer_style_match_styles(row) == ["trend", "pullback", "quality"]


def test_infer_style_match_styles_returns_empty_when_no_match():
    assert infer_style_match_styles({"style_match_reasons": ["无关原因"]}) == []


def test_candidate_style_match_styles_prefers_explicit_field():
    row = {"style_match_styles": ["trend", "pullback"]}
    assert candidate_style_match_styles(row, ["trend", "pullback", "quality"]) == ["trend", "pullback"]


def test_candidate_style_match_styles_falls_back_to_inferred_reasons():
    row = {"style_match_reasons": ["趋势偏好符合走势"]}
    assert candidate_style_match_styles(row, ["trend", "pullback"]) == ["trend"]


def test_candidate_style_match_styles_falls_back_to_requested_when_flag_true():
    row = {"style_match": True}
    assert candidate_style_match_styles(row, ["trend", "quality"]) == ["trend", "quality"]


def test_candidate_style_match_styles_filters_out_unrequested():
    row = {"style_match_styles": ["trend", "pullback"]}
    assert candidate_style_match_styles(row, ["trend"]) == ["trend"]


def test_style_preference_match_status_empty_when_no_preference():
    assert style_preference_match_status([{"style_match": True}], {}) == ""


def test_style_preference_match_status_uses_generic_check_when_no_explicit_styles():
    preference = {"raw": "喜欢低吸"}
    assert style_preference_match_status([{"style_match": True}], preference) == "hit"
    assert style_preference_match_status([{}], preference) == "miss"


def test_style_preference_match_status_hit_when_all_requested_styles_matched():
    preference = {"styles": ["trend"]}
    rows = [{"style_match_styles": ["trend"]}]
    assert style_preference_match_status(rows, preference) == "hit"


def test_style_preference_match_status_partial_when_some_styles_matched():
    preference = {"styles": ["trend", "pullback"]}
    rows = [{"style_match_styles": ["trend"]}]
    assert style_preference_match_status(rows, preference) == "partial"


def test_style_preference_match_status_miss_when_no_styles_matched():
    preference = {"styles": ["trend", "pullback"]}
    rows = [{"style_match_styles": []}]
    assert style_preference_match_status(rows, preference) == "miss"


def test_missing_style_preference_labels_returns_unmatched_labels():
    preference = {"styles": ["trend", "pullback", "quality"]}
    row = {"style_match_styles": ["trend"]}
    assert missing_style_preference_labels(row, preference) == ["低吸", "质量"]


def test_missing_style_preference_labels_empty_when_no_requested_styles():
    assert missing_style_preference_labels({}, {}) == []


def test_style_preference_text_joins_labels():
    assert style_preference_text({"styles": ["trend", "pullback"]}) == "趋势,低吸"


def test_style_preference_text_falls_back_to_raw_when_no_styles():
    assert style_preference_text({"raw": "喜欢稳健标的" * 5}) == ("喜欢稳健标的" * 5)[:40]
    assert style_preference_text("not-a-dict") == ""


def test_theme_preference_text_prefers_theme_over_raw():
    assert theme_preference_text({"theme": "半导体", "raw": "raw-text"}) == "半导体"
    assert theme_preference_text({"raw": "只有原始文本"}) == "只有原始文本"
    assert theme_preference_text("not-a-dict") == ""
