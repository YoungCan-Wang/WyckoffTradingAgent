from __future__ import annotations

from core.cn_boards import cn_board, is_main_or_chinext, is_star_or_bse, is_supported_cn_board


def test_cn_board_classification_supports_bse_toggle() -> None:
    assert cn_board("000001") == "main"
    assert cn_board("300001") == "chinext"
    assert cn_board("688001") == "star"
    assert cn_board("830000") == "bse"
    assert is_supported_cn_board("830000")
    assert not is_supported_cn_board("830000", include_bse=False)


def test_cn_board_groups_market_mix_helpers() -> None:
    assert is_main_or_chinext("600000")
    assert is_main_or_chinext("300001")
    assert is_star_or_bse("688001")
    assert is_star_or_bse("830000")
