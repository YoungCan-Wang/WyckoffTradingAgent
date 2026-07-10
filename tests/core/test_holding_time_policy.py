from core.holding_time_policy import holding_time_action, is_mainline_track


def test_swing_time_exit_at_five_days() -> None:
    advice = holding_time_action(5, is_mainline=False)
    assert advice.action == "TIME_EXIT"
    assert "时间" in advice.reason


def test_mainline_holds_until_structure_breaks() -> None:
    hold = holding_time_action(16, is_mainline=True, below_ma20=False, theme_dry_down=False)
    assert hold.action == "HOLD"
    trim = holding_time_action(16, is_mainline=True, below_ma20=True)
    assert trim.action == "REVIEW_TRIM"


def test_mainline_track_detection() -> None:
    assert is_mainline_track("mainline", "", "") is True
    assert is_mainline_track("主升通道", "Markup", "") is True
    assert is_mainline_track("吸筹", "accum", "spring") is False
