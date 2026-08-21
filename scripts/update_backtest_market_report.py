"""CLI entrypoint for updating the market-cycle backtest report."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import _bootstrap  # noqa: F401

from workflows.backtest_market_report_artifacts import GridCell, load_grid_cells
from workflows.backtest_market_report_builder import build_confirmation, build_report
from workflows.backtest_parameter_stability import build_parameter_stability
from workflows.backtest_walk_forward import build_walk_forward_validation


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a market backtest report from backtest grid artifacts.")
    parser.add_argument("--artifacts-dir", default="artifacts", help="Directory containing backtest-grid-* artifacts")
    parser.add_argument("--output", default="artifacts/BACKTEST_MARKET_REPORT.md", help="Report markdown path")
    parser.add_argument("--confirmation-output", default="", help="Optional machine-readable confirmation JSON path")
    parser.add_argument("--run-url", default="", help="GitHub Actions run URL")
    parser.add_argument("--generated-at", default="", help="Override generated timestamp")
    parser.add_argument("--hypothesis-id", default="", help="Optional research hypothesis to receive this evidence")
    parser.add_argument("--evidence-output", default="", help="Optional portable research evidence JSON path")
    parser.add_argument("--stability-output", default="", help="Optional parameter stability JSON path")
    parser.add_argument("--stability-evidence-output", default="", help="Optional portable stability evidence path")
    parser.add_argument("--walk-forward-output", default="", help="Optional walk-forward validation JSON path")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    cells = load_grid_cells(Path(args.artifacts_dir))
    report = build_report(cells, run_url=args.run_url, generated_at=args.generated_at)
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report + "\n", encoding="utf-8")
    print(f"[backtest-report] wrote {out_path} from {len(cells)} grid cells")
    if args.confirmation_output:
        confirmation = build_confirmation(cells, run_url=args.run_url, generated_at=args.generated_at)
        confirmation_path = Path(args.confirmation_output)
        confirmation_path.parent.mkdir(parents=True, exist_ok=True)
        confirmation_path.write_text(json.dumps(confirmation, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"[backtest-report] wrote {confirmation_path}")
        _record_hypothesis_evidence(args, confirmation, confirmation_path, "backtest", args.evidence_output)
        _write_stability(args, cells)
        _write_walk_forward(args, cells)
    return 0


def _record_hypothesis_evidence(
    args: argparse.Namespace,
    confirmation: dict[str, object],
    confirmation_path: Path,
    evidence_type: str,
    evidence_output: str,
) -> None:
    hypothesis_id = str(args.hypothesis_id or "").strip()
    if not hypothesis_id:
        return
    artifact_ref = _artifact_ref(args.run_url, confirmation_path, evidence_type)
    evidence = {
        "hypothesis_id": hypothesis_id,
        "evidence_type": evidence_type,
        "artifact_ref": artifact_ref,
        "verdict": confirmation.get("status", "review"),
        "summary": confirmation.get("summary", ""),
        "metrics": confirmation,
    }
    if evidence_output:
        output = Path(evidence_output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"[backtest-report] wrote {output}")
    _link_local_registry(evidence)


def _write_stability(args: argparse.Namespace, cells: list[GridCell]) -> None:
    if not args.stability_output:
        return
    stability = build_parameter_stability(cells, run_url=args.run_url, generated_at=args.generated_at)
    output = Path(args.stability_output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(stability, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[backtest-report] wrote {output}")
    _record_hypothesis_evidence(
        args,
        stability,
        output,
        "stability",
        args.stability_evidence_output,
    )


def _write_walk_forward(args: argparse.Namespace, cells: list[GridCell]) -> None:
    if not args.walk_forward_output:
        return
    validation = build_walk_forward_validation(cells, run_url=args.run_url, generated_at=args.generated_at)
    output = Path(args.walk_forward_output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(validation, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[backtest-report] wrote {output}")


def _artifact_ref(run_url: str, confirmation_path: Path, evidence_type: str) -> str:
    clean_url = str(run_url or "").strip().rstrip("/")
    artifact_name = "parameter_stability.json" if evidence_type == "stability" else "backtest_confirmation.json"
    return f"{clean_url}#{artifact_name}" if clean_url else str(confirmation_path.resolve())


def _link_local_registry(evidence: dict[str, object]) -> None:
    from integrations import local_db

    hypothesis_id = str(evidence["hypothesis_id"])
    if local_db.load_research_hypothesis(hypothesis_id) is None:
        print(f"[backtest-report] hypothesis {hypothesis_id} not found locally; portable evidence retained")
        return
    local_db.link_research_evidence(evidence)
    print(f"[backtest-report] linked backtest evidence to {hypothesis_id}")


if __name__ == "__main__":
    raise SystemExit(main())
