from __future__ import annotations

from tools.funnel_public import public_funnel_details, public_funnel_metrics


def test_public_funnel_metrics_drop_heavy_and_debug_fields():
    metrics = {
        "layer1": 10,
        "all_df_map": {"000001": object()},
        "financial_map": {"000001": {"roe": 5}},
        "_debug": {"cfg": object()},
    }

    assert public_funnel_metrics(metrics) == {"layer1": 10}


def test_public_funnel_details_drop_heavy_top_level_and_metrics_fields():
    details = {
        "selected_for_ai": ["000001"],
        "all_df_map": {"000001": object()},
        "metrics": {"layer2": 5, "all_df_map": {"000001": object()}},
    }

    assert public_funnel_details(details) == {
        "selected_for_ai": ["000001"],
        "metrics": {"layer2": 5},
    }


def test_public_funnel_metrics_drop_private_external_seed_fields():
    metrics = {
        "external_seed_count": 2,
        "external_seed_source": "private-list",
        "external_seed_l1_codes": ["000001"],
        "external_seed_l4_triggers": {"spring": [("000002", 1.0)]},
        "external_seed_observation_rows": [{"code": "000001", "watch_status": "WATCH"}],
    }

    assert public_funnel_metrics(metrics) == {
        "external_seed_count": 2,
    }


def test_public_funnel_details_drop_private_external_seed_top_level_fields():
    details = {
        "selected_for_ai": ["000001"],
        "external_seed_triggers": {"spring": [("000002", 1.0)]},
        "external_seed_selected": ["000002"],
        "metrics": {
            "external_seed_count": 1,
            "external_seed_watch_codes": ["000003"],
        },
    }

    assert public_funnel_details(details) == {
        "selected_for_ai": ["000001"],
        "metrics": {"external_seed_count": 1},
    }
