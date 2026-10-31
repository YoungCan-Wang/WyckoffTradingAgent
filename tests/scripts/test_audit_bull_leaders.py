import importlib.util
from pathlib import Path

import pandas as pd


def _load_module():
    path = Path(__file__).resolve().parents[2] / "scripts" / "audit_bull_leaders.py"
    spec = importlib.util.spec_from_file_location("audit_bull_leaders", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _trades(rows):
    return pd.DataFrame(rows)


def test_compare_arms_splits_marginal_sets():
    mod = _load_module()
    live = _trades(
        [
            {"signal_date": "2025-01-02", "code": 1, "ret_pct": 5.0, "trigger": "spring"},
            {"signal_date": "2025-01-03", "code": 2, "ret_pct": -3.0, "trigger": "evr"},
        ]
    )
    control = _trades(
        [
            {"signal_date": "2025-01-02", "code": 1, "ret_pct": 4.0, "trigger": "main_force_entry"},
            {"signal_date": "2025-01-06", "code": 3, "ret_pct": -2.0, "trigger": "trend_breakout"},
        ]
    )
    out = mod.compare_arms(live, control)
    assert out["common_n"] == 1
    assert out["live_only"]["n"] == 1
    assert out["control_only"]["n"] == 1
    assert out["common_live"]["mean"] == 5.0
    assert out["common_control"]["mean"] == 4.0


def test_compare_arms_disjoint_sets():
    mod = _load_module()
    live = _trades([{"signal_date": "2025-01-02", "code": 600001, "ret_pct": 1.0, "trigger": "spring"}])
    control = _trades([{"signal_date": "2025-02-03", "code": 300001, "ret_pct": 2.0, "trigger": "sos"}])
    out = mod.compare_arms(live, control)
    assert out["common_n"] == 0
    assert out["live_only"]["win_rate"] == 100.0
