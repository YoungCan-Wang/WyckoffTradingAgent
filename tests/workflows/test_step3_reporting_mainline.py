import pandas as pd

from workflows.step3_reporting import _mainline_preview


def test_mainline_preview_is_deterministic_and_optional():
    selected = pd.DataFrame(
        [
            {
                "code": "300308",
                "name": "中际旭创",
                "candidate_lane": "mainline",
                "candidate_status": "主线买点候选",
                "candidate_reasons": {"theme": "光模块"},
                "stock_role_score": 0.8,
            },
            {"code": "000001", "name": "平安银行"},
        ]
    )

    preview = _mainline_preview(selected)

    assert "主线定位（确定性字段）" in preview
    assert "300308 中际旭创 | 光模块 / 主升候选 / 主线核心" in preview
    assert "000001" not in preview
    assert _mainline_preview(pd.DataFrame()) == ""
