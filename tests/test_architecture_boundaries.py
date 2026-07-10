from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_CORE_PREFIXES = ("integrations", "scripts", "tools", "workflows")
FORBIDDEN_CORE_MODULES = {"integrations.backtest_service"}


def _import_names(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


def _imported_members(path: Path, module: str) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    members: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == module:
            members.update(alias.name for alias in node.names)
    return members


def _env_accesses(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id == "os" and node.attr in {"getenv", "environ"}:
                hits.append(f"{path.relative_to(ROOT)} -> os.{node.attr}")
    return hits


def _top_level_env_accesses(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for child in ast.walk(node):
            if isinstance(child, ast.Attribute) and isinstance(child.value, ast.Name):
                if child.value.id == "os" and child.attr in {"getenv", "environ"}:
                    hits.append(f"{path.relative_to(ROOT)}:{child.lineno} -> os.{child.attr}")
    return hits


def _top_level_import_names(path: Path) -> list[str]:
    """Module-level import targets only; skips imports nested inside function bodies,
    which are the sanctioned way to break a layering dependency at call time.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


def _print_calls(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "print":
            hits.append(f"{path.relative_to(ROOT)}:{node.lineno} -> print")
    return hits


def test_core_does_not_depend_on_entrypoint_or_backtest_workflow_adapters():
    violations: list[str] = []
    for path in sorted((ROOT / "core").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        for name in _import_names(path):
            if name in FORBIDDEN_CORE_MODULES or name.split(".", 1)[0] in FORBIDDEN_CORE_PREFIXES:
                violations.append(f"{path.relative_to(ROOT)} -> {name}")

    assert violations == []


def test_core_does_not_read_runtime_environment_directly():
    violations: list[str] = []
    for path in sorted((ROOT / "core").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        violations.extend(_env_accesses(path))

    assert violations == []


def test_core_does_not_write_console_output_directly():
    violations: list[str] = []
    for path in sorted((ROOT / "core").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        violations.extend(_print_calls(path))

    assert violations == []


def test_library_layers_do_not_write_console_output_directly():
    violations: list[str] = []
    for dirname in ("agents", "core", "integrations", "tools"):
        for path in sorted((ROOT / dirname).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            violations.extend(_print_calls(path))

    assert violations == []


def test_step4_library_workflows_do_not_write_console_output_directly():
    violations: list[str] = []
    for path in sorted((ROOT / "workflows").glob("step4_*.py")):
        if path.name == "step4_pipeline.py":
            continue
        violations.extend(_print_calls(path))

    assert violations == []


def test_step4_order_engine_uses_explicit_runtime_config():
    assert _env_accesses(ROOT / "workflows" / "step4_order_engine.py") == []


def test_step4_rebalancer_delegates_result_persistence():
    path = ROOT / "workflows" / "step4_rebalancer.py"
    imports = set(_import_names(path))
    forbidden_members = {
        "cancel_trade_orders",
        "save_ai_trade_orders",
        "update_position_stops",
        "upsert_daily_nav",
    }
    assert "uuid" not in imports
    assert "datetime" not in _imported_members(path, "datetime")
    assert forbidden_members.isdisjoint(_imported_members(path, "integrations.supabase_portfolio"))
    assert {"prepare_step4_result_record", "save_step4_orders_and_nav"}.issubset(
        _imported_members(path, "workflows.step4_results")
    )


def test_step4_rebalancer_delegates_portfolio_loading():
    path = ROOT / "workflows" / "step4_rebalancer.py"
    imports = set(_import_names(path))
    forbidden_members = {
        "compute_portfolio_state_signature",
        "load_portfolio_state",
    }
    assert {"json", "os", "re"}.isdisjoint(imports)
    assert forbidden_members.isdisjoint(_imported_members(path, "integrations.supabase_portfolio"))
    assert "load_step4_portfolio_state" in _imported_members(path, "workflows.step4_portfolio")


def test_step4_rebalancer_delegates_llm_decision_call():
    path = ROOT / "workflows" / "step4_rebalancer.py"
    imports = set(_import_names(path))
    assert "core.prompts" not in imports
    assert "integrations.llm_client" not in imports
    assert "tools.debug_io" not in imports
    assert "call_step4_decision_model" in _imported_members(path, "workflows.step4_llm")


def test_step4_rebalancer_delegates_decision_execution():
    path = ROOT / "workflows" / "step4_rebalancer.py"
    imports = set(_import_names(path))
    assert "concurrent.futures" not in imports
    assert "dataclasses" not in imports
    assert "workflows.step4_order_engine" not in imports
    assert "calc_atr" not in _imported_members(path, "workflows.step4_payload")
    assert "load_qfq_history" not in _imported_members(path, "workflows.step4_payload")
    assert "fetch_latest_real_close" not in _imported_members(path, "workflows.step4_payload")
    assert {
        "backfill_step4_decision_market_data",
        "complete_step4_decisions",
        "execute_step4_decisions",
        "rendered_step4_market_view",
    }.issubset(_imported_members(path, "workflows.step4_decisions"))


def test_step4_rebalancer_does_not_read_runtime_env_at_import_time():
    assert _top_level_env_accesses(ROOT / "workflows" / "step4_rebalancer.py") == []


def test_step3_report_workflow_does_not_read_runtime_env_at_import_time():
    assert _top_level_env_accesses(ROOT / "workflows" / "step3_batch_report.py") == []


def test_step3_report_workflow_delegates_rag_boundary():
    imports = _import_names(ROOT / "workflows" / "step3_batch_report.py")
    assert "integrations.rag_veto" not in imports


def test_step3_report_workflow_delegates_prompt_input_building():
    imports = _import_names(ROOT / "workflows" / "step3_batch_report.py")
    assert "tools.debug_io" not in imports
    assert "tools.report_builder" not in imports


def test_step3_report_workflow_delegates_candidate_data_building():
    imports = set(_import_names(ROOT / "workflows" / "step3_batch_report.py"))
    forbidden = {
        "core.sector_rotation",
        "core.wyckoff_engine",
        "integrations.fetch_a_share_csv",
        "integrations.index_data_source",
        "integrations.market_metadata",
        "tools.data_fetcher",
        "utils.trading_clock",
    }
    assert imports.isdisjoint(forbidden)


def test_step3_selection_does_not_parse_report_operations():
    imports = set(_import_names(ROOT / "workflows" / "step3_selection.py"))
    forbidden = {
        "tools.report_builder",
        "workflows.step3_operation_gate",
    }
    assert imports.isdisjoint(forbidden)


def test_step3_selection_delegates_candidate_compression():
    path = ROOT / "workflows" / "step3_selection.py"
    text = path.read_text(encoding="utf-8")
    assert "DYNAMIC_MAINLINE_" not in text
    assert "_compress_step3_candidates" not in text
    assert "select_compressed_step3_candidates" in _imported_members(path, "workflows.step3_compression")


def test_step3_selection_delegates_upstream_priority_selection():
    path = ROOT / "workflows" / "step3_selection.py"
    imports = set(_import_names(path))
    upstream_members = _imported_members(path, "workflows.step3_upstream_selection")
    assert "core.ai_candidate_allocation" not in imports
    assert "fit_ai_candidate_quotas" not in path.read_text(encoding="utf-8")
    assert {"has_upstream_priority_context", "select_upstream_priority_candidates"}.issubset(upstream_members)


def test_report_builder_does_not_parse_step3_operation_pool():
    path = ROOT / "tools" / "report_builder.py"
    imports = set(_import_names(path))
    text = path.read_text(encoding="utf-8")
    forbidden_tokens = (
        "extract_operation_pool_codes",
        "extract_operation_pool_springboards",
        "extract_ops_codes_from_markdown",
        "try_parse_structured_report",
        "extract_json_block",
        "OPERATION_POOL_KEYS",
        "WATCH_POOL_KEYS",
    )

    assert "utils.json_text" not in imports
    assert "tools.report_parser" not in imports
    assert not any(token in text for token in forbidden_tokens)


def test_report_builder_does_not_build_track_prompt_messages():
    path = ROOT / "tools" / "report_builder.py"
    text = path.read_text(encoding="utf-8")
    imports = set(_import_names(path))

    assert "tools.track_prompt_builder" not in imports
    assert "build_track_user_message" not in text
    assert "_track_execution_requirements" not in text
    assert "_track_scope_text" not in text


def test_step3_inputs_uses_track_prompt_builder_for_track_messages():
    path = ROOT / "workflows" / "step3_inputs.py"
    imports = set(_import_names(path))

    assert "tools.track_prompt_builder" in imports
    assert "tools.report_builder" in imports


def test_data_fetcher_delegates_realtime_spot_patch():
    data_fetcher = ROOT / "tools" / "data_fetcher.py"
    fallback_fetcher = ROOT / "tools" / "ohlcv_fallback_fetcher.py"
    data_imports = set(_import_names(data_fetcher))
    fallback_imports = set(_import_names(fallback_fetcher))
    data_text = data_fetcher.read_text(encoding="utf-8")

    assert "tools.spot_patch" not in data_imports
    assert "tools.spot_patch" in fallback_imports
    assert "integrations.spot_snapshot" not in data_imports
    for token in ("fetch_stock_spot_snapshot", "class SpotPatchBasis", "def _spot_patch_row"):
        assert token not in data_text


def test_data_fetcher_delegates_tickflow_batch_fetching():
    path = ROOT / "tools" / "data_fetcher.py"
    imports = set(_import_names(path))
    text = path.read_text(encoding="utf-8")

    assert "tools.tickflow_batch_fetcher" in imports
    assert "integrations.tickflow_client" not in imports
    for token in ("TickFlowClient", "normalize_cn_symbol", "FUNNEL_ENABLE_TICKFLOW_BATCH"):
        assert token not in text


def test_data_fetcher_delegates_parallel_fallback_fetching():
    path = ROOT / "tools" / "data_fetcher.py"
    imports = set(_import_names(path))
    text = path.read_text(encoding="utf-8")

    assert "tools.ohlcv_fallback_fetcher" in imports
    for token in (
        "ProcessPoolExecutor",
        "ThreadPoolExecutor",
        "fetch_one_with_retry",
        "terminate_executor_processes",
        "integrations.fetch_a_share_csv",
        "integrations.data_source",
    ):
        assert token not in text


def test_ohlcv_fallback_fetcher_uses_explicit_runtime_config():
    path = ROOT / "tools" / "ohlcv_fallback_fetcher.py"
    imports = set(_import_names(path))
    text = path.read_text(encoding="utf-8")

    assert "os" not in imports
    assert _env_accesses(path) == []
    assert "FUNNEL_FETCH_" not in text
    assert "FUNNEL_BATCH_" not in text
    assert "FetchRuntimeConfig" in text


def test_step3_and_step4_payloads_use_spot_patch_directly():
    checked = [
        ROOT / "workflows" / "step3_candidates.py",
        ROOT / "workflows" / "step4_payload.py",
    ]
    for path in checked:
        imports = set(_import_names(path))
        assert "tools.spot_patch" in imports
        assert "tools.data_fetcher" not in imports


def test_step3_report_workflow_delegates_llm_track_execution():
    path = ROOT / "workflows" / "step3_batch_report.py"
    llm_members = _imported_members(path, "workflows.step3_llm")
    assert "call_track_report" not in llm_members
    assert "call_step3_track_reports" in llm_members
    assert "integrations.llm_client" not in set(_import_names(path))


def test_step3_report_workflow_delegates_output_reporting():
    path = ROOT / "workflows" / "step3_batch_report.py"
    imports = set(_import_names(path))
    forbidden = {
        "core.compliance_report",
        "integrations.llm_client",
        "workflows.step3_delivery",
        "workflows.compliance_report_config",
    }
    assert imports.isdisjoint(forbidden)
    assert "build_step3_preview_report" not in _imported_members(path, "workflows.step3_inputs")
    assert {"send_empty_step3_report", "send_step3_final_report"}.issubset(
        _imported_members(path, "workflows.step3_reporting")
    )


def test_daily_job_delegates_signal_observation_runtime():
    paths = [ROOT / "workflows" / "daily_job_step2.py", ROOT / "workflows" / "daily_job_step3.py"]
    forbidden_imports = {
        "core.price_action_footprint",
        "core.signal_feedback",
        "core.tail_buy.strategy",
        "integrations.external_capital_context",
        "integrations.supabase_external_seeds",
        "integrations.supabase_signal_feedback",
    }
    for path in paths:
        imports = set(_import_names(path))
        text = path.read_text(encoding="utf-8")
        assert "workflows.daily_signal_observations" in imports
        assert imports.isdisjoint(forbidden_imports)
        for token in (
            "def _shadow_observation_inputs",
            "def _build_intraday_tail_map",
            "def _build_external_capital_context_map",
            "def _build_external_seed_signal_rows",
            "def _persist_signal_observations",
            "def _persist_external_seed_observations",
            "def _build_springboard_map",
            "build_signal_observations",
            "build_price_action_footprint_map",
            "compute_tail_features",
        ):
            assert token not in text


def test_signal_feedback_entrypoint_delegates_runtime_workflow():
    path = ROOT / "scripts" / "signal_feedback_job.py"
    imports = set(_import_names(path))
    text = path.read_text(encoding="utf-8")

    assert "workflows.signal_feedback_job" in imports
    forbidden_imports = {
        "pandas",
        "core.signal_feedback",
        "core.signal_lifecycle",
        "integrations.supabase_signal_feedback",
    }
    assert imports.isdisjoint(forbidden_imports)
    for token in (
        "evaluate_signal_lifecycle",
        "build_signal_registry_updates",
        "summarize_signal_health",
        "load_recent_signal_observations",
        "upsert_signal_outcomes",
        "def _fetch_history",
        "def _outcome_rows",
        "def refresh_outcomes",
        "def refresh_health",
    ):
        assert token not in text


def test_daily_job_entrypoint_delegates_workflow():
    path = ROOT / "scripts" / "daily_job.py"
    imports = set(_import_names(path))
    text = path.read_text(encoding="utf-8")

    assert "workflows.daily_job" in imports
    forbidden_imports = {
        "contextlib",
        "dataclasses",
        "datetime",
        "typing",
        "workflows.daily_job_persistence",
        "workflows.daily_job_runtime",
        "workflows.daily_signal_observations",
        "workflows.step4_holdings_diagnosis",
        "workflows.step4_pipeline",
    }
    assert imports.isdisjoint(forbidden_imports)
    for token in (
        "Step2StageResult",
        "Step3StageResult",
        "def _run_signal_confirmation",
        "def _run_springboard_scoring",
        "def _run_step2_stage",
        "def _run_step3_stage",
        "def _run_step4_stage",
        "def _run_step2_block",
        "def _run_step3_block",
        "run_step4_pipeline",
        "run_step4_holdings_diagnosis",
        "daily_persistence",
        "signal_observations",
    ):
        assert token not in text


def test_daily_job_workflow_delegates_runtime_config_and_stages():
    path = ROOT / "workflows" / "daily_job.py"
    imports = set(_import_names(path))
    text = path.read_text(encoding="utf-8")

    assert "workflows.daily_job_runtime" in imports
    assert "workflows.daily_job_step2" in imports
    assert "workflows.daily_job_step3" in imports
    assert "workflows.daily_job_step4" in imports
    assert "workflows.daily_job_lifecycle" in imports
    forbidden_imports = {
        "contextlib",
        "dataclasses",
        "datetime",
        "workflows.daily_job_persistence",
        "workflows.daily_signal_observations",
        "workflows.daily_job_stages",
        "workflows.step4_holdings_diagnosis",
        "workflows.step4_pipeline",
    }
    assert imports.isdisjoint(forbidden_imports)
    for token in (
        "Step2StageResult",
        "Step3StageResult",
        "run_step4_pipeline",
        "daily_persistence",
        "signal_observations.",
    ):
        assert token not in text


def test_daily_job_step_modules_delegate_runtime_boundaries():
    step2 = ROOT / "workflows" / "daily_job_step2.py"
    step3 = ROOT / "workflows" / "daily_job_step3.py"
    step4 = ROOT / "workflows" / "daily_job_step4.py"
    step2_imports = set(_import_names(step2))
    step3_imports = set(_import_names(step3))
    step4_imports = set(_import_names(step4))

    assert "workflows.daily_job_persistence" in step2_imports
    assert "workflows.daily_job_persistence" in step3_imports
    assert "workflows.step4_holdings_diagnosis" in step4_imports
    assert "workflows.step4_pipeline" in step4_imports
    for path in (step2, step3, step4):
        imports = set(_import_names(path))
        text = path.read_text(encoding="utf-8")
        assert "workflows.daily_job_runtime" in imports
        for token in (
            "def _resolve_config",
            "def _preflight_exit_code",
            "def _missing_llm_config",
            "def _provider_ready",
            "OPENAI_COMPATIBLE_BASE_URLS",
            "get_provider_credentials",
            "TickFlowClient",
            "tail_buy_strategy_config_from_env",
        ):
            assert token not in text


def test_theme_radar_entrypoint_delegates_runtime_and_rendering():
    path = ROOT / "scripts" / "theme_radar_job.py"
    imports = set(_import_names(path))
    text = path.read_text(encoding="utf-8")

    assert "workflows.theme_radar_runtime" in imports
    forbidden_imports = {
        "core.theme_radar",
        "integrations.supabase_concept_heat",
        "integrations.theme_radar_storage",
        "utils.feishu",
        "workflows.theme_radar_report",
        "workflows.wyckoff_funnel",
    }
    assert imports.isdisjoint(forbidden_imports)
    for token in (
        "build_theme_radar_snapshot",
        "run_funnel_job",
        "persist_theme_radar_snapshot",
        "send_feishu_notification",
        "render_theme_radar_report",
        "render_theme_radar_html",
        "THEME_RADAR_CSS",
        "def _theme_table",
        "def _candidate_table",
        "def _html_shell",
    ):
        assert token not in text


def test_sector_continuity_entrypoint_delegates_runtime_and_report_calculation():
    path = ROOT / "scripts" / "sector_continuity_report.py"
    imports = set(_import_names(path))
    text = path.read_text(encoding="utf-8")

    assert "workflows.sector_continuity_runtime" in imports
    forbidden_imports = {
        "integrations.market_metadata",
        "integrations.supabase_concept_heat",
        "utils.feishu",
        "utils.trading_clock",
        "workflows.sector_continuity_report",
    }
    assert imports.isdisjoint(forbidden_imports)
    for token in (
        "fetch_concept_heat",
        "load_concept_heat_history_from_supabase",
        "upsert_concept_heat_history",
        "send_feishu_notification",
        "build_sector_continuity_report",
        "update_history_with_trade_date",
        "def _compute_streaks",
        "def _render_summary",
        "def _render_advice",
    ):
        assert token not in text


def test_dashboard_server_does_not_embed_static_spa_asset():
    path = ROOT / "cli" / "dashboard.py"
    imports = set(_import_names(path))
    text = path.read_text(encoding="utf-8")
    asset_text = (ROOT / "cli" / "dashboard_html.py").read_text(encoding="utf-8")
    assert "cli.dashboard_html" in imports
    assert "_DASHBOARD_HTML" not in text
    assert "<style>" not in text
    assert "<script>" not in text
    assert 'DASHBOARD_HTML = r"""<!DOCTYPE html>' in asset_text
    assert "loadPage('overview')" in asset_text


def test_funnel_render_does_not_send_notifications_directly():
    imports = set(_import_names(ROOT / "workflows" / "funnel_render.py"))
    assert "utils.feishu" not in imports


def test_funnel_render_delegates_structured_report_payload():
    imports = set(_import_names(ROOT / "workflows" / "funnel_render.py"))
    assert "core.funnel_report" not in imports
    assert "workflows.funnel_report_payload" in imports


def test_wyckoff_funnel_delegates_notification_delivery():
    imports = set(_import_names(ROOT / "workflows" / "wyckoff_funnel.py"))
    assert "workflows.funnel_render" not in imports
    assert "workflows.funnel_delivery" in imports


def test_wyckoff_funnel_delegates_benchmark_gate_logging_to_data_workflow():
    orchestration = (ROOT / "workflows" / "wyckoff_funnel.py").read_text(encoding="utf-8")
    data_workflow = (ROOT / "workflows" / "funnel_data.py").read_text(encoding="utf-8")
    assert "_print_benchmark_gate" not in orchestration
    assert "_print_benchmark_gate" in data_workflow


def test_funnel_etf_workflow_does_not_write_console_output_directly():
    assert _print_calls(ROOT / "workflows" / "funnel_etf.py") == []


def test_market_regime_delegates_liquidity_metrics():
    path = ROOT / "tools" / "market_regime.py"
    imports = set(_import_names(path))
    text = path.read_text(encoding="utf-8")

    assert "tools.market_liquidity" in imports
    for token in (
        "MONEY_FLOW_EXPAND_RATIO",
        "MONEY_FLOW_CONTRACT_RATIO",
        "AMOUNT_DISTRIBUTION_SKEW_THRESHOLD",
        "def _symbol_money_snapshot",
        "def _money_flow_totals",
        "def _classify_money_flow",
        "def _symbol_avg_amount",
        "def _empty_amount_distribution",
        "def _amount_distribution_summary",
    ):
        assert token not in text


def test_market_regime_thresholds_use_explicit_runtime_config():
    path = ROOT / "tools" / "market_regime.py"
    text = path.read_text(encoding="utf-8")
    imports = set(_import_names(path))
    workflow_imports = set(_import_names(ROOT / "workflows" / "funnel_data.py"))

    assert "os" not in imports
    assert _env_accesses(path) == []
    assert "MarketRegimeConfig" in text
    assert "workflows.market_regime_config" in workflow_imports
    for token in (
        "FUNNEL_BREADTH_",
        "FUNNEL_CRASH_",
        "FUNNEL_PANIC_REPAIR_",
        "FUNNEL_RISK_OFF_",
        "FUNNEL_EVR_POLICY",
    ):
        assert token not in text


def test_market_liquidity_metrics_use_explicit_runtime_config():
    path = ROOT / "tools" / "market_liquidity.py"
    imports = set(_import_names(path))
    text = path.read_text(encoding="utf-8")
    workflow_imports = set(_import_names(ROOT / "workflows" / "funnel_data.py"))

    assert "os" not in imports
    assert _env_accesses(path) == []
    assert "FUNNEL_MONEY_FLOW_" not in text
    assert "FUNNEL_AMOUNT_DISTRIBUTION_" not in text
    assert "workflows.market_liquidity_config" in workflow_imports


def test_market_funnel_entrypoint_delegates_workflow():
    path = ROOT / "scripts" / "market_funnel_job.py"
    imports = set(_import_names(path))
    text = path.read_text(encoding="utf-8")

    assert "workflows.market_funnel_job" in imports
    forbidden_imports = {
        "json",
        "pandas",
        "pathlib",
        "typing",
        "core.candidate_ranker",
        "core.wyckoff_engine",
        "integrations.tickflow_client",
        "integrations.tickflow_notice",
        "workflows.market_funnel_config",
        "workflows.market_funnel_data",
        "workflows.market_funnel_runtime",
    }
    assert imports.isdisjoint(forbidden_imports)
    for token in (
        "def _run_layers",
        "def _candidate_rows",
        "def _render_markdown_report",
        "def _write_output",
        "def _upsert_funnel_to_tracking",
        "def run_market_funnel",
        "TickFlowClient",
        "fetch_market_inputs",
        "runtime_config_from_env",
    ):
        assert token not in text


def test_market_funnel_workflow_delegates_runtime_and_data_loading():
    path = ROOT / "workflows" / "market_funnel_job.py"
    imports = set(_import_names(path))
    text = path.read_text(encoding="utf-8")

    assert "workflows.market_funnel_runtime" in imports
    assert "workflows.market_funnel_data" in imports
    assert "workflows.market_funnel_report" in imports
    assert "workflows.market_funnel_tracking" in imports
    for token in (
        "def _runtime_config",
        "def _load_symbols",
        "def _rank_quotes",
        "def _fetch_quotes",
        "def _fetch_daily_histories",
        "def _fetch_benchmark_history",
        "def _fetch_market_inputs",
        "normalize_hist_from_fetch",
        "MARKET_FUNNEL_MAX_SYMBOLS",
        "MARKET_FUNNEL_SYMBOL_FILE",
        "get_quotes(",
        "get_klines_batch(",
        "def _render_markdown_report",
        "def _leader_radar_markdown_block",
        "def _fmt_number",
        "def _write_output",
        "def _write_report",
        "def _candidate_trade_date",
        "def _tracking_row",
        "def _upsert_funnel_to_tracking",
        "upsert_global_recommendations",
    ):
        assert token not in text


def test_premarket_risk_entrypoint_delegates_external_risk_inputs():
    path = ROOT / "scripts" / "premarket_risk_job.py"
    imports = set(_import_names(path))
    text = path.read_text(encoding="utf-8")

    assert "workflows.premarket_risk_job" in imports
    forbidden_imports = {
        "csv",
        "requests",
        "time",
        "json",
        "dataclasses",
        "core.premarket_public_brief",
        "integrations.llm_client",
        "integrations.supabase_market_signal",
        "utils.feishu",
        "utils.trading_clock",
        "workflows.premarket_public_brief_config",
        "workflows.premarket_risk_inputs",
    }
    assert imports.isdisjoint(forbidden_imports)
    for token in (
        "StringIO",
        "PremarketSnapshot",
        "generate_public_premarket_brief",
        "call_llm",
        "load_latest_market_signal_daily",
        "upsert_market_signal_daily",
        "send_feishu_notification",
        "fetch_a50",
        "fetch_vix_until_ready",
        "judge_regime",
        "build_action_matrix",
        "def _fetch_a50",
        "def _collect_premarket_snapshot",
        "def _build_premarket_content",
        "def _build_market_signal_patch",
        "def _persist_premarket_signal",
        "def _send_premarket_notification",
        "PREMARKET_VIX_",
        "PREMARKET_A50_",
    ):
        assert token not in text


def test_holding_diagnosis_entrypoint_delegates_workflow():
    path = ROOT / "scripts" / "holding_diagnosis_job.py"
    imports = set(_import_names(path))
    text = path.read_text(encoding="utf-8")

    assert "workflows.holding_diagnosis_job" in imports
    forbidden_imports = {
        "json",
        "re",
        "time",
        "concurrent.futures",
        "dataclasses",
        "datetime",
        "integrations.llm_client",
        "integrations.tickflow_client",
        "integrations.supabase_portfolio",
        "utils.telegram",
        "workflows.holding_diagnosis_llm",
        "workflows.tail_buy_config",
        "workflows.tail_buy_holdings",
    }
    assert imports.isdisjoint(forbidden_imports)
    for token in (
        "TickFlowClient",
        "SYSTEM_PROMPT",
        "HOLDING_ACTIONS",
        "run_holding_llm_report",
        "tail_buy_strategy_config_from_env",
        "analyze_holdings_actions",
        "build_holdings_markdown",
        "send_to_telegram",
        "def _build_holding_llm_prompt",
        "def _parse_holding_llm",
        "def _run_holdings_llm",
        "def _build_report",
        "def _build_llm_routes",
        "def _run_llm_and_report",
        "def runtime_from_env",
        "def _send_holding_report",
        "load_portfolio_state",
        "call_llm(",
    ):
        assert token not in text


def test_strategy_reflection_entrypoint_delegates_workflow():
    path = ROOT / "scripts" / "strategy_reflection_job.py"
    imports = set(_import_names(path))
    text = path.read_text(encoding="utf-8")

    assert "workflows.strategy_reflection_job" in imports
    forbidden_imports = {
        "json",
        "core.strategy_reflection",
        "integrations.supabase_signal_feedback",
        "integrations.supabase_strategy_reflection",
    }
    assert imports.isdisjoint(forbidden_imports)
    for token in (
        "build_policy_candidate",
        "build_strategy_reflection",
        "load_policy_shadow_runs",
        "load_recent_signal_outcomes",
        "upsert_strategy_policy_candidate",
        "upsert_strategy_reflection",
        "def _build_payloads",
        "def _enabled",
    ):
        assert token not in text


def test_backtest_market_report_entrypoint_delegates_artifact_parsing():
    path = ROOT / "scripts" / "update_backtest_market_report.py"
    imports = set(_import_names(path))
    text = path.read_text(encoding="utf-8")

    assert "workflows.backtest_market_report_artifacts" in imports
    assert "workflows.backtest_market_report_builder" in imports
    for token in (
        "def load_grid_cells",
        "def _read_trades",
        "def _parse_params",
        "def _parse_cash_style_rows",
        "def _to_float",
        "def build_report",
        "def _build_period_best_table",
        "def _build_trade_diagnostics",
        "rank_robust_params",
        "weak_period_guardrails",
        "csv",
        "glob",
        "dataclass",
        "Counter",
        "defaultdict",
        "statistics",
    ):
        assert token not in text


def test_backtest_snapshot_fetch_entrypoint_delegates_runtime_workflow():
    path = ROOT / "scripts" / "backtest_snapshot_fetch.py"
    imports = set(_import_names(path))
    text = path.read_text(encoding="utf-8")

    assert "workflows.backtest_snapshot_fetch" in imports
    forbidden_imports = {
        "concurrent.futures",
        "dataclasses",
        "pandas",
        "core.wyckoff_engine",
        "integrations.data_source",
        "integrations.fetch_a_share_csv",
        "integrations.index_data_source",
        "integrations.market_metadata",
    }
    assert imports.isdisjoint(forbidden_imports)
    for token in (
        "ThreadPoolExecutor",
        "fetch_stock_hist",
        "get_stocks_by_board",
        "fetch_index_akshare",
        "fetch_sector_map",
        "fetch_market_cap_map",
        "normalize_hist_from_fetch",
        "def _fetch_one",
        "def _fetch_batch_tickflow",
        "def _write_snapshot_outputs",
        "def _load_symbols",
    ):
        assert token not in text


def test_benchmark_funnel_fetch_entrypoint_delegates_runtime_workflow():
    path = ROOT / "scripts" / "benchmark_funnel_fetch.py"
    imports = set(_import_names(path))
    text = path.read_text(encoding="utf-8")

    assert "workflows.benchmark_funnel_fetch" in imports
    forbidden_imports = {
        "json",
        "time",
        "collections",
        "concurrent.futures",
        "pandas",
        "core.wyckoff_engine",
        "integrations.fetch_a_share_csv",
        "utils.trading_clock",
    }
    assert imports.isdisjoint(forbidden_imports)
    for token in (
        "normalize_hist_from_fetch",
        "fetch_all_ohlcv",
        "get_stocks_by_board",
        "resolve_trading_window",
        "ThreadPoolExecutor",
        "ProcessPoolExecutor",
        "Counter(",
        "def _fetch_one",
        "def _run_single",
        "def _run_batch",
        "def _summarize",
    ):
        assert token not in text


def test_backtest_portfolio_entrypoint_delegates_runtime_workflow():
    path = ROOT / "scripts" / "backtest_portfolio.py"
    imports = set(_import_names(path))
    text = path.read_text(encoding="utf-8")

    assert "workflows.backtest_portfolio" in imports
    forbidden_imports = {"pandas", "datetime", "core.backtest_metrics", "core.backtest_portfolio"}
    assert imports.isdisjoint(forbidden_imports)
    for token in (
        "build_portfolio_nav",
        "calc_portfolio_metrics",
        "fmt_metric",
        "pd.",
        "to_csv",
        "write_text",
        "def _build_portfolio_md",
    ):
        assert token not in text


def test_diagnose_holdings_entrypoint_delegates_runtime_workflow():
    path = ROOT / "scripts" / "diagnose_holdings.py"
    imports = set(_import_names(path))
    text = path.read_text(encoding="utf-8")

    assert "workflows.diagnose_holdings_cli" in imports
    forbidden_imports = {
        "json",
        "pandas",
        "dataclasses",
        "core.holding_diagnostic",
        "core.wyckoff_engine",
        "integrations.fetch_a_share_csv",
        "integrations.index_data_source",
        "utils.trading_clock",
    }
    assert imports.isdisjoint(forbidden_imports)
    for token in (
        "format_diagnostic_text",
        "normalize_hist_from_fetch",
        "fetch_hist",
        "fetch_index_hist",
        "resolve_trading_window",
        "def _fetch_stock_data",
        "def _fetch_benchmark",
        "def _load_from_supabase",
        "def _format_json",
        "def _format_markdown",
        "def _format_text",
        "def _diagnose",
    ):
        assert token not in text


def test_export_a_share_csv_entrypoint_delegates_workflow():
    path = ROOT / "scripts" / "export_a_share_csv.py"
    imports = set(_import_names(path))
    text = path.read_text(encoding="utf-8")
    forbidden_imports = {
        "akshare",
        "pandas",
        "datetime",
        "integrations.fetch_a_share_csv",
        "utils",
    }

    assert "workflows.export_a_share_csv" in imports
    assert imports.isdisjoint(forbidden_imports)
    for token in (
        "fetch_hist",
        "normalize_symbols",
        "resolve_trading_window",
        "stock_sector_em",
        "safe_filename_part",
        "def _build_export",
        "def _load_code_name_map",
        "def _write_export_batch",
    ):
        assert token not in text


def test_db_maintenance_entrypoint_delegates_workflow():
    path = ROOT / "scripts" / "db_maintenance.py"
    imports = set(_import_names(path))
    text = path.read_text(encoding="utf-8")
    forbidden_imports = {
        "datetime",
        "core.constants",
        "integrations.supabase_base",
    }

    assert "workflows.db_maintenance" in imports
    assert imports.isdisjoint(forbidden_imports)
    for token in (
        "create_admin_client",
        "cleanup_table",
        "cleanup_recommendation_table",
        "cleanup_recommendation_tracking",
        "CLEANUP_RULES",
        "RECOMMENDATION_TRACKING_TABLES",
        "def _cutoff_value",
        "def _latest_recommend_dates",
    ):
        assert token not in text


def test_market_universe_meta_entrypoint_delegates_workflow():
    path = ROOT / "scripts" / "build_market_universe_meta.py"
    imports = set(_import_names(path))
    text = path.read_text(encoding="utf-8")
    forbidden_imports = {
        "json",
        "typing",
    }

    assert "workflows.market_universe_meta" in imports
    assert imports.isdisjoint(forbidden_imports)
    for token in (
        "def _clean_symbol_lines",
        "def _market_entry",
        "def _cn_fund_symbol",
        "def _etf_entries",
        "def build_metadata",
        "def _write_json",
    ):
        assert token not in text


def test_param_sensitivity_entrypoint_delegates_grid_workflow():
    path = ROOT / "scripts" / "param_sensitivity.py"
    imports = set(_import_names(path))
    text = path.read_text(encoding="utf-8")

    assert "workflows.param_sensitivity" in imports
    forbidden_imports = {
        "itertools",
        "json",
        "traceback",
        "dataclasses",
        "datetime",
        "pathlib",
        "pandas",
        "core.backtest_run",
        "workflows.backtest",
    }
    assert imports.isdisjoint(forbidden_imports)
    for token in (
        "SensitivityCombo",
        "SensitivityRunConfig",
        "BacktestWorkflowRequest",
        "build_sensitivity_markdown",
        "run_sensitivity",
        "run_backtest_request",
        "def _load_grid",
        "def _build_sensitivity_combos",
        "def _summary_to_row",
        "def _run_sensitivity_combo",
        ".to_csv(",
        ".write_text(",
        "groupby(",
    ):
        assert token not in text


def test_backtest_runner_entrypoint_delegates_workflow():
    path = ROOT / "scripts" / "backtest_runner.py"
    imports = set(_import_names(path))
    text = path.read_text(encoding="utf-8")
    forbidden_imports = {
        "core.backtest_run",
        "workflows.backtest",
        "workflows.backtest_artifacts",
        "workflows.backtest_defaults",
        "utils.progress",
    }

    assert "workflows.backtest_cli" in imports
    assert "workflows.backtest_runner" in imports
    assert imports.isdisjoint(forbidden_imports)
    for token in (
        "BacktestWorkflowRequest",
        "run_backtest_request",
        "write_backtest_artifacts",
        "write_suite_summary",
        "parse_hold_days_list",
        "def _request_from_args",
        "def _run_one_hold_days",
    ):
        assert token not in text


def test_us_backtest_strategy_replay_entrypoint_delegates_runtime_workflow():
    path = ROOT / "scripts" / "us_backtest_strategy_replay.py"
    imports = set(_import_names(path))
    text = path.read_text(encoding="utf-8")

    assert "workflows.backtest_strategy_replay" in imports
    forbidden_imports = {"csv", "json", "math", "dataclasses", "pandas", "statistics"}
    assert imports.isdisjoint(forbidden_imports)
    for token in (
        "StrategySpec",
        "ReplayTrade",
        "SignalCandidate",
        "STRATEGIES",
        "def _load_hist_map",
        "def _generate_signal_rows",
        "def _entry",
        "def _exit",
        "def _replay_one",
        "def _summary",
        "def _write_outputs",
    ):
        assert token not in text


def test_us_backtest_snapshot_fetch_entrypoint_delegates_runtime_workflow():
    path = ROOT / "scripts" / "backtest_snapshot_fetch_us.py"
    imports = set(_import_names(path))
    text = path.read_text(encoding="utf-8")

    assert "workflows.backtest_snapshot_fetch_hk_us" in imports
    forbidden_imports = {
        "json",
        "time",
        "datetime",
        "pandas",
        "core.wyckoff_engine",
        "integrations.market_universe",
        "integrations.tickflow_client",
    }
    assert imports.isdisjoint(forbidden_imports)
    for token in (
        "TickFlowClient",
        "normalize_hist_from_fetch",
        "load_us_symbols",
        "def _fetch_klines_batched",
        "def _fetch_benchmark",
        "def _save_snapshot",
    ):
        assert token not in text


def test_hk_backtest_strategy_replay_entrypoint_delegates_runtime_workflow():
    path = ROOT / "scripts" / "hk_backtest_strategy_replay.py"
    imports = set(_import_names(path))
    text = path.read_text(encoding="utf-8")

    assert "workflows.backtest_strategy_replay" in imports
    forbidden_imports = {"csv", "json", "math", "dataclasses", "pandas", "statistics", "core.hk_risk_filter"}
    assert imports.isdisjoint(forbidden_imports)
    for token in (
        "StrategySpec",
        "ReplayTrade",
        "SignalCandidate",
        "STRATEGIES",
        "def _load_hist_map",
        "def _generate_signal_rows",
        "def _entry",
        "def _exit",
        "def _replay_one",
        "def _summary",
        "def _write_outputs",
        "classify_hk_risk",
    ):
        assert token not in text


def test_hk_backtest_snapshot_fetch_entrypoint_delegates_runtime_workflow():
    path = ROOT / "scripts" / "backtest_snapshot_fetch_hk.py"
    imports = set(_import_names(path))
    text = path.read_text(encoding="utf-8")

    assert "workflows.backtest_snapshot_fetch_hk_us" in imports
    forbidden_imports = {
        "json",
        "time",
        "datetime",
        "pandas",
        "core.wyckoff_engine",
        "integrations.market_universe",
        "integrations.tickflow_client",
    }
    assert imports.isdisjoint(forbidden_imports)
    for token in (
        "TickFlowClient",
        "normalize_hist_from_fetch",
        "load_hk_symbols",
        "def _fetch_klines_batched",
        "def _fetch_benchmark",
        "def _save_snapshot",
    ):
        assert token not in text


def test_hk_backtest_notify_entrypoint_delegates_report_builders():
    path = ROOT / "scripts" / "notify_hk_backtest.py"
    imports = set(_import_names(path))
    text = path.read_text(encoding="utf-8")

    assert "workflows.backtest_notification" in imports
    forbidden_imports = {"json", "math", "collections", "dataclasses", "pathlib", "requests", "typing"}
    assert imports.isdisjoint(forbidden_imports)
    for token in (
        "BacktestCell",
        "send_feishu",
        "def _cell_from_summary",
        "def _group_by_period",
        "def _period_elements",
        "def _strategy_row",
        "def build_card",
        "def write_report",
        "lark_md",
    ):
        assert token not in text


def test_web_background_entrypoint_delegates_runtime_workflow():
    path = ROOT / "scripts" / "web_background_job.py"
    imports = set(_import_names(path))
    text = path.read_text(encoding="utf-8")

    assert "workflows.web_background_job" in imports
    forbidden_imports = {"json", "logging", "traceback", "datetime", "pathlib", "tools.funnel_public"}
    assert imports.isdisjoint(forbidden_imports)
    for token in (
        "public_funnel_metrics",
        "run_funnel",
        "run_step3",
        "load_user_settings_admin",
        "def _sanitize",
        "def _write_result",
        "def _load_payload",
        "def _apply_funnel_env",
        "def _run_funnel_screen",
        "def _resolve_model_credentials",
        "def _run_batch_ai_report",
    ):
        assert token not in text


def test_us_backtest_notify_entrypoint_delegates_report_builders():
    path = ROOT / "scripts" / "notify_us_backtest.py"
    imports = set(_import_names(path))
    text = path.read_text(encoding="utf-8")

    assert "workflows.backtest_notification" in imports
    forbidden_imports = {"json", "math", "collections", "dataclasses", "pathlib", "requests", "typing"}
    assert imports.isdisjoint(forbidden_imports)
    for token in (
        "BacktestCell",
        "send_feishu",
        "def _cell_from_summary",
        "def _group_by_period",
        "def _period_elements",
        "def _strategy_row",
        "def build_card",
        "def write_report",
        "lark_md",
    ):
        assert token not in text


def test_single_symbol_diagnosis_entrypoint_delegates_workflow():
    path = ROOT / "scripts" / "single_symbol_funnel_diagnosis.py"
    imports = set(_import_names(path))
    text = path.read_text(encoding="utf-8")

    assert "workflows.single_symbol_diagnosis" in imports
    forbidden_imports = {
        "csv",
        "json",
        "dataclasses",
        "pandas",
        "core.candidate_ranker",
        "core.signal_confirmation",
        "core.wyckoff_engine",
        "utils.feishu",
        "workflows.market_funnel_config",
        "workflows.single_symbol_diagnosis_data",
        "workflows.single_symbol_diagnosis_outputs",
    }
    assert imports.isdisjoint(forbidden_imports)
    for token in (
        "SymbolSpec",
        "ReplayContext",
        "DayDiagnostic",
        "layer1_filter",
        "layer2_strength_detailed",
        "layer3_sector_resonance",
        "layer4_triggers",
        "score_springboard_abc",
        "def write_outputs",
        "def _write_csv",
        "def _json_payload",
        "def build_report",
        "def _report_row",
        "def _fmt_counts",
        "def notify_feishu",
        "send_feishu_notification",
        "notify_single_symbol_feishu",
        "write_single_symbol_outputs",
        "normalize_hist_from_fetch",
        "fetch_symbol_history",
        "load_rps_universe_histories",
        "load_symbol_context",
        "def _fetch_tickflow_daily",
        "def _prepare_history",
        "def _first_index_on_or_after",
        "def _date_to_utc_ms",
        "def _name_map",
        "def _safe_fetch_market_cap_map",
        "def _safe_fetch_sector_map",
        "def _safe_fetch_benchmark",
        "get_klines_batch",
        "get_stocks_by_board",
        "fetch_stock_hist",
        "fetch_index_hist",
        "TickFlowClient",
        "def evaluate_day",
        "def replay_symbol",
        "def summarize_diagnostics",
        "def load_required_rps_histories",
    ):
        assert token not in text


def test_strategy_attribution_entrypoint_delegates_stats():
    path = ROOT / "scripts" / "strategy_attribution_report.py"
    imports = set(_import_names(path))
    text = path.read_text(encoding="utf-8")

    assert "workflows.strategy_attribution_report" in imports
    forbidden_imports = {
        "datetime",
        "core.constants",
        "integrations.supabase_base",
        "workflows.strategy_attribution_stats",
    }
    assert imports.isdisjoint(forbidden_imports)
    for token in (
        "TABLE_STRATEGY_ATTRIBUTION_REPORTS",
        "create_admin_client",
        "create_read_client",
        "require_server_write_context",
        "build_strategy_attribution_payload",
        "report.md",
        "report.json",
        ".upsert(",
        "def _fetch_all",
        "def build_report",
        "def _write_artifacts",
        "def _create_user_read_client",
        "def _create_report_client",
        "def _num",
        "def _json_map",
        "def _str_list",
        "def _candidate_shadow_fields",
        "def _data_lineage_fields",
        "def _stats",
        "def _join_outcomes",
        "def _group_stats",
        "def _score_bucket_stats",
        "def _candidate_shadow_stats",
        "def _coverage_grade_stats",
        "def _evidence_key_stats",
        "def _coverage_summary",
        "def _data_lineage_stats",
        "def _score_stats_json",
        "def _ranked",
        "def _shadow_stats",
        "def _recommendations",
    ):
        assert token not in text


def test_review_list_replay_delegates_recommendation_lookup():
    path = ROOT / "scripts" / "review_list_replay.py"
    imports = set(_import_names(path))
    text = path.read_text(encoding="utf-8")

    assert "workflows.review_list_replay" in imports
    assert "workflows.review_recommendation_lookup" not in imports
    for token in (
        "TABLE_RECOMMENDATION_TRACKING",
        "integrations.supabase_base",
        "def _normalize_code6",
        "def _normalize_recommend_date",
        "def _load_recommendation_lookup",
        "def _format_recommendation_history",
        '.select("code,name,recommend_date,recommend_count,is_ai_recommended")',
    ):
        assert token not in text


def test_review_list_replay_delegates_big_gainer_discovery():
    path = ROOT / "scripts" / "review_list_replay.py"
    imports = set(_import_names(path))
    text = path.read_text(encoding="utf-8")

    assert "workflows.review_list_replay" in imports
    assert "workflows.review_big_gainers" not in imports
    forbidden_imports = {
        "integrations.spot_snapshot",
        "tools.data_fetcher",
    }
    assert imports.isdisjoint(forbidden_imports)
    for token in (
        "TODAY_REVIEW_MIN_PCT",
        "TODAY_OPEN_MAX_PCT",
        "PREVIOUS_REVIEW_MAX_PCT",
        "def _latest_pct_and_open",
        "def _find_big_gainers",
        "def _find_big_gainers_from_spot",
        "def _fetch_and_filter_review_codes",
        "def _review_spot_min_coverage",
        "def _load_today_review_codes",
        "load_spot_snapshot_map",
        "fetch_all_ohlcv",
    ):
        assert token not in text


def test_review_list_replay_delegates_report_rendering():
    path = ROOT / "scripts" / "review_list_replay.py"
    imports = set(_import_names(path))
    text = path.read_text(encoding="utf-8")

    assert "workflows.review_list_replay" in imports
    assert "workflows.review_report_render" not in imports
    for token in (
        "def _short_code_list",
        "def _build_focus_lines",
        "def _build_report_lines",
        "**重点归因**",
        "**逐票复盘（在前一日漏斗中止步层级与原因）**",
        "推荐表交叉检查",
    ):
        assert token not in text


def test_review_list_replay_entrypoint_is_thin_cli():
    path = ROOT / "scripts" / "review_list_replay.py"
    imports = set(_import_names(path))
    text = path.read_text(encoding="utf-8")

    forbidden_imports = {
        "collections",
        "contextlib",
        "dataclasses",
        "datetime",
        "pandas",
        "core.candidate_ranker",
        "core.wyckoff_engine",
        "utils.feishu",
        "workflows.wyckoff_funnel",
    }
    assert imports.isdisjoint(forbidden_imports)
    for token in (
        "ReplayContext",
        "run_funnel_job",
        "FunnelConfig",
        "sort_by_date_if_needed",
        "send_feishu_notification",
        "def _classify_review_code",
        "def _run_previous_funnel",
        "def _replay_context",
        "def _build_replay_rows",
        "def _send_replay_report",
    ):
        assert token not in text


def test_tail_buy_holdings_delegates_portfolio_and_market_data():
    path = ROOT / "workflows" / "tail_buy_holdings.py"
    imports = set(_import_names(path))
    text = path.read_text(encoding="utf-8")

    assert "workflows.tail_buy_holding_portfolio" in imports
    assert "workflows.tail_buy_holding_data" in imports
    assert "workflows.tail_buy_holding_models" in imports
    assert "workflows.tail_buy_utils" in imports
    for token in (
        "core.constants",
        "integrations.supabase_base",
        "integrations.supabase_portfolio",
        "integrations.tickflow_notice",
        "def _normalize_effective_positions",
        "def _discover_user_live_portfolios",
        "def _fetch_holding_quotes",
        "def _fetch_holding_intraday",
        "def _fetch_holding_market_data",
        "def current_time",
        "def log_line",
        "ZoneInfo",
    ):
        assert token not in text


def test_tail_buy_intraday_job_delegates_candidates_and_rule_scan():
    path = ROOT / "scripts" / "tail_buy_intraday_job.py"
    imports = set(_import_names(path))
    text = path.read_text(encoding="utf-8")

    assert "workflows.tail_buy_intraday_job" in imports
    assert "workflows.tail_buy_candidates" not in imports
    assert "workflows.tail_buy_rule_scan" not in imports
    forbidden_imports = {
        "collections",
        "core.tail_buy.strategy",
        "integrations.tickflow_client",
        "integrations.tickflow_notice",
        "utils.trading_clock",
        "workflows.tail_buy_delivery",
        "workflows.tail_buy_holdings",
        "workflows.tail_buy_llm_overlay",
        "workflows.tail_buy_runtime",
        "workflows.tail_buy_utils",
    }
    assert imports.isdisjoint(forbidden_imports)
    for token in (
        "core.constants",
        "integrations.fetch_a_share_csv",
        "integrations.supabase_base",
        "integrations.supabase_portfolio",
        "ThreadPoolExecutor",
        "FutureTimeout",
        "evaluate_rule_decision",
        "pick_tail_candidates",
        "def _resolve_trade_dates",
        "def _load_signal_pending_candidates",
        "def _load_tail_candidates",
        "def _run_rule_scan",
        "def _run_rule_scan_batch",
        "def _run_tail_buy_candidate_flow",
        "def _build_tail_buy_holdings_section",
        "merge_rule_and_llm",
        "TickFlowClient",
        "notify_tail_buy_non_trading_day",
        "persist_tail_buy_results",
        "send_tail_buy_report",
    ):
        assert token not in text


def test_step4_action_does_not_expose_feishu_webhook():
    workflow = (ROOT / ".github" / "workflows" / "step4_from_supabase.yml").read_text(encoding="utf-8")
    assert "FEISHU" not in workflow.upper()


def test_step4_from_supabase_entrypoint_delegates_workflow():
    path = ROOT / "scripts" / "step4_from_supabase.py"
    imports = set(_import_names(path))
    text = path.read_text(encoding="utf-8")

    assert "workflows.step4_from_supabase" in imports
    forbidden_imports = {
        "json",
        "datetime",
        "core.constants",
        "integrations.llm_client",
        "integrations.supabase_base",
        "workflows.step4_pipeline",
    }
    assert imports.isdisjoint(forbidden_imports)
    for token in (
        "TABLE_RECOMMENDATION_TRACKING",
        "create_admin_client",
        "close_client",
        "get_provider_credentials",
        "resolve_provider_name",
        "run_step4_pipeline",
        "latest_trade_date_str",
        "def _load_recommendations",
        "def _build_external_report",
        "def _resolve_recommend_date",
    ):
        assert token not in text


def test_step4_workflows_do_not_depend_on_feishu_delivery():
    violations: list[str] = []
    for path in sorted((ROOT / "workflows").glob("step4*.py")):
        if "utils.feishu" in _import_names(path):
            violations.append(f"{path.relative_to(ROOT)} -> utils.feishu")

    assert violations == []


def test_oms_entrypoints_are_telegram_only():
    violations: list[str] = []
    paths = [ROOT / "scripts" / "step4_from_supabase.py", ROOT / "agents" / "strategy_tools.py"]
    paths.extend(sorted((ROOT / "workflows").glob("step4*.py")))
    forbidden_text = ("FEISHU", "feishu", "飞书", "send_feishu_notification", "notify_all")
    for path in paths:
        imports = set(_import_names(path))
        imported_notify = _imported_members(path, "utils.notify")
        text = path.read_text(encoding="utf-8")
        if "utils.feishu" in imports:
            violations.append(f"{path.relative_to(ROOT)} -> utils.feishu")
        if "notify_all" in imported_notify:
            violations.append(f"{path.relative_to(ROOT)} -> utils.notify.notify_all")
        if any(token in text for token in forbidden_text):
            violations.append(f"{path.relative_to(ROOT)} -> forbidden Feishu token")

    assert violations == []


def test_feishu_delivery_delegates_rich_card_builders():
    imports = set(_import_names(ROOT / "utils" / "feishu.py"))
    text = (ROOT / "utils" / "feishu.py").read_text(encoding="utf-8")

    assert "utils.feishu_backtest_card" in imports
    assert "utils.feishu_tail_buy_card" in imports
    assert "utils.feishu_text" in imports
    assert "def _post_rich_card" in text
    for token in (
        "class BacktestCardData",
        "class TailBuyCardLimits",
        "class TailBuyReportSections",
        "def _parse_backtest",
        "def _build_backtest",
        "def _tail_buy_report",
        "def _build_tail_buy",
    ):
        assert token not in text


def test_notification_callers_use_channel_specific_modules():
    checked_paths = [ROOT / "scripts", ROOT / "workflows", ROOT / "agents"]
    forbidden = {"send_to_telegram", "send_wecom_notification", "send_dingtalk_notification"}
    violations: list[str] = []
    for root in checked_paths:
        for path in sorted(root.rglob("*.py")):
            imported = _imported_members(path, "utils.notify")
            bad = sorted(imported & forbidden)
            if bad:
                violations.append(f"{path.relative_to(ROOT)} -> utils.notify.{','.join(bad)}")

    assert violations == []


def test_runtime_layers_do_not_depend_on_script_entrypoints():
    checked_paths = [ROOT / "mcp_server.py"]
    for dirname in ("agents", "cli", "core", "workflows"):
        checked_paths.extend(sorted((ROOT / dirname).rglob("*.py")))

    violations: list[str] = []
    for path in checked_paths:
        if "__pycache__" in path.parts:
            continue
        for name in _import_names(path):
            if name == "scripts" or name.startswith("scripts."):
                violations.append(f"{path.relative_to(ROOT)} -> {name}")

    assert violations == []


def test_integrations_do_not_depend_on_runtime_or_entrypoint_layers():
    forbidden = {"agents", "scripts", "tools", "workflows", "web"}
    violations: list[str] = []
    for path in sorted((ROOT / "integrations").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        for name in _import_names(path):
            if name.split(".", 1)[0] in forbidden:
                violations.append(f"{path.relative_to(ROOT)} -> {name}")

    assert violations == []


def test_integrations_do_not_define_cli_entrypoints():
    violations: list[str] = []
    for path in sorted((ROOT / "integrations").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        imports = set(_import_names(path))
        text = path.read_text(encoding="utf-8")
        if "argparse" in imports:
            violations.append(f"{path.relative_to(ROOT)} -> argparse")
        if "__main__" in text:
            violations.append(f"{path.relative_to(ROOT)} -> __main__")

    assert violations == []


def test_signal_pending_adapter_does_not_own_confirmation_workflow():
    path = ROOT / "integrations" / "supabase_signal_pending.py"
    imports = set(_import_names(path))
    text = path.read_text(encoding="utf-8")
    assert "core.signal_confirmation" not in imports
    assert "pandas" not in imports
    assert "run_step2_5" not in text
    workflow_imports = set(_import_names(ROOT / "workflows" / "step2_signal_confirmation.py"))
    assert "core.signal_confirmation" in workflow_imports
    assert "integrations.supabase_signal_pending" in workflow_imports


def test_recommendation_tracking_adapter_does_not_own_market_reprice_workflow():
    path = ROOT / "integrations" / "supabase_recommendation.py"
    imports = set(_import_names(path))
    text = path.read_text(encoding="utf-8")
    forbidden_imports = {
        "pandas",
        "integrations.data_source",
        "integrations.fetch_a_share_csv",
        "integrations.spot_snapshot",
        "integrations.tickflow_client",
        "integrations.tushare_client",
    }
    assert imports.isdisjoint(forbidden_imports)
    assert "refresh_tracking_prices" not in text
    assert "sync_all_tracking_prices" not in text
    workflow_imports = set(_import_names(ROOT / "workflows" / "recommendation_tracking_reprice.py"))
    assert "integrations.supabase_recommendation" in workflow_imports


def test_recommendation_payload_adapter_delegates_domain_payload_building():
    path = ROOT / "integrations" / "recommendation_payload.py"
    imports = set(_import_names(path))
    text = path.read_text(encoding="utf-8")

    assert "core.recommendation_payload" in imports
    assert "pandas" not in imports
    assert {
        "build_recommendation_payload",
        "recommendation_backup_rows",
        "recommendation_restore_sql",
        "springboard_ai_payload",
    }.issubset(_imported_members(path, "core.recommendation_payload"))
    for token in (
        "def _extract_recommendation_attribution",
        "def _build_recommendation_payload",
        "def _merge_recommendation_payload_row",
        "def _recommendation_restore_sql",
        "def _sql_literal",
        "def _ai_code_ints",
        "SPRINGBOARD_AI_UPDATE_COLUMNS",
    ):
        assert token not in text


def test_global_recommendation_adapter_does_not_own_market_reprice_workflow():
    path = ROOT / "integrations" / "recommendation_global.py"
    imports = set(_import_names(path))
    text = path.read_text(encoding="utf-8")
    forbidden_imports = {
        "os",
        "pandas",
        "integrations.tickflow_client",
    }
    assert imports.isdisjoint(forbidden_imports)
    assert "refresh_global_tracking_prices" not in text
    assert "build_global_tickflow_tracking_updates" not in text
    workflow_imports = set(_import_names(ROOT / "workflows" / "recommendation_tracking_reprice.py"))
    assert "integrations.recommendation_global" in workflow_imports


def test_recommendation_reprice_entrypoint_delegates_workflow():
    path = ROOT / "scripts" / "recommendation_tracking_reprice_job.py"
    imports = set(_import_names(path))
    text = path.read_text(encoding="utf-8")

    assert "workflows.recommendation_tracking_reprice_job" in imports
    forbidden_imports = {
        "datetime",
        "zoneinfo",
        "integrations.supabase_tail_buy",
        "workflows.recommendation_tracking_reprice",
    }
    assert imports.isdisjoint(forbidden_imports)
    for token in (
        "refresh_tail_buy_prices_with_tickflow_realtime",
        "refresh_global_tracking_prices",
        "refresh_tracking_prices_with_tickflow_realtime",
        "def _log",
        "def _now",
    ):
        assert token not in text


def test_us_recommendation_performance_entrypoint_delegates_workflow():
    path = ROOT / "scripts" / "us_recommendation_performance_job.py"
    imports = set(_import_names(path))
    text = path.read_text(encoding="utf-8")
    forbidden_imports = {
        "datetime",
        "zoneinfo",
        "integrations.recommendation_performance",
    }

    assert "workflows.us_recommendation_performance_job" in imports
    assert imports.isdisjoint(forbidden_imports)
    for token in (
        "refresh_us_tracking_performance",
        "def _log",
        "def _now",
    ):
        assert token not in text


def test_data_source_formatting_helpers_live_outside_fetch_orchestration():
    path = ROOT / "integrations" / "data_source.py"
    imports = set(_import_names(path))
    text = path.read_text(encoding="utf-8")
    assert "integrations.data_source_format" in imports
    for name in (
        "normalize_efinance_columns",
        "tickflow_daily_frame",
        "tickflow_daily_window",
        "to_ts_code",
    ):
        assert f"def {name}" not in text


def test_data_source_baostock_provider_owns_session_state():
    path = ROOT / "integrations" / "data_source.py"
    imports = set(_import_names(path))
    text = path.read_text(encoding="utf-8")
    assert "integrations.data_source_baostock" in imports
    assert "atexit" not in imports
    assert "socket" not in imports
    for token in ("_BAOSTOCK_LOGGED", "_BAOSTOCK_LOCK", "_BAOSTOCK_MODULE", "BAOSTOCK_MAX_SECONDS"):
        assert token not in text
    assert "query_history_k_data_plus" not in text


def test_data_source_efinance_provider_owns_cache_patch():
    path = ROOT / "integrations" / "data_source.py"
    imports = set(_import_names(path))
    text = path.read_text(encoding="utf-8")
    format_members = _imported_members(path, "integrations.data_source_format")
    assert "integrations.data_source_efinance" in imports
    assert "pathlib" not in imports
    assert "tempfile" not in imports
    assert "normalize_efinance_columns" not in format_members
    assert "_import_efinance_with_cache_patch" not in text
    assert "efinance-cache" not in text


def test_data_source_tickflow_provider_owns_client_state():
    path = ROOT / "integrations" / "data_source.py"
    imports = set(_import_names(path))
    text = path.read_text(encoding="utf-8")
    format_members = _imported_members(path, "integrations.data_source_format")
    assert "integrations.data_source_tickflow" in imports
    assert "threading" not in imports
    assert "integrations.tickflow_client" not in imports
    for name in (
        "tickflow_daily_frame",
        "tickflow_daily_window",
        "tickflow_daily_count",
        "tickflow_adjust_mode",
    ):
        assert name not in format_members
    for token in ("_TICKFLOW_CLIENT", "_TICKFLOW_DAILY_MAX_COUNT", "_TICKFLOW_LIMIT_NOTICE_LOCK"):
        assert token not in text


def test_data_source_vendor_providers_own_vendor_fetching():
    path = ROOT / "integrations" / "data_source.py"
    imports = set(_import_names(path))
    text = path.read_text(encoding="utf-8")
    format_members = _imported_members(path, "integrations.data_source_format")
    assert "integrations.data_source_akshare" in imports
    assert "integrations.data_source_tushare" in imports
    assert "http.client" not in imports
    assert "time" not in imports
    assert "to_ts_code" not in format_members
    for token in ("stock_zh_a_hist", "pro_bar", "_fetch_stock_akshare", "_fetch_stock_tushare", "AKSHARE_RETRY"):
        assert token not in text


def test_backtest_data_preparation_belongs_to_workflows():
    assert not (ROOT / "integrations" / "backtest_data.py").exists()
    imports = set(_import_names(ROOT / "workflows" / "backtest.py"))
    assert "workflows.backtest_data" in imports
    assert "integrations.backtest_data" not in imports


def test_tools_do_not_depend_on_runtime_or_entrypoint_layers():
    forbidden = {"agents", "scripts", "workflows", "web"}
    violations: list[str] = []
    for path in sorted((ROOT / "tools").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        for name in _import_names(path):
            if name.split(".", 1)[0] in forbidden:
                violations.append(f"{path.relative_to(ROOT)} -> {name}")

    assert violations == []


def test_non_cli_runtime_layers_do_not_depend_on_cli():
    checked_paths = [ROOT / "mcp_server.py"]
    for dirname in ("agents", "core", "integrations", "tools", "workflows"):
        checked_paths.extend(sorted((ROOT / dirname).rglob("*.py")))

    violations: list[str] = []
    for path in checked_paths:
        if "__pycache__" in path.parts:
            continue
        for name in _import_names(path):
            if name == "cli" or name.startswith("cli."):
                violations.append(f"{path.relative_to(ROOT)} -> {name}")

    assert violations == []


def test_workflows_do_not_depend_on_agents_or_script_entrypoints():
    forbidden = {"agents", "scripts", "web"}
    violations: list[str] = []
    for path in sorted((ROOT / "workflows").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        for name in _import_names(path):
            if name.split(".", 1)[0] in forbidden:
                violations.append(f"{path.relative_to(ROOT)} -> {name}")

    assert violations == []


def test_agents_do_not_import_workflows_at_module_scope():
    """agents/ is a tool-layer library; it must not hard-depend on workflows/ at import
    time. Reaching into a workflow for a specific helper is only allowed via a
    function-local import at the call site (see agents/history_tools.py).
    """
    violations: list[str] = []
    for path in sorted((ROOT / "agents").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        for name in _top_level_import_names(path):
            if name == "workflows" or name.startswith("workflows."):
                violations.append(f"{path.relative_to(ROOT)} -> {name}")

    assert violations == []


def test_runtime_layers_do_not_import_private_runtime_members():
    checked_paths = [ROOT / "mcp_server.py"]
    for dirname in ("agents", "cli", "integrations", "scripts", "workflows"):
        checked_paths.extend(sorted((ROOT / dirname).rglob("*.py")))

    runtime_prefixes = ("cli.", "core.", "integrations.", "tools.", "workflows.")
    violations: list[str] = []
    for path in checked_paths:
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith(runtime_prefixes):
                violations.extend(
                    f"{path.relative_to(ROOT)} -> {node.module}.{alias.name}"
                    for alias in node.names
                    if alias.name.startswith("_")
                )

    assert violations == []


def test_candidate_ranker_lives_in_core_not_tools():
    assert not (ROOT / "tools" / "candidate_ranker.py").exists()

    checked_paths = [ROOT / "mcp_server.py"]
    for dirname in ("agents", "cli", "core", "integrations", "scripts", "tests", "workflows"):
        checked_paths.extend(sorted((ROOT / dirname).rglob("*.py")))

    violations: list[str] = []
    for path in checked_paths:
        if "__pycache__" in path.parts:
            continue
        for name in _import_names(path):
            if name == "tools.candidate_ranker":
                violations.append(f"{path.relative_to(ROOT)} -> {name}")

    assert violations == []
