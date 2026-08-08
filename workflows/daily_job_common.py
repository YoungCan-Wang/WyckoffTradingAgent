"""Shared models and small helpers for daily job workflows."""

from __future__ import annotations

import os
import sys
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, field

from workflows.step4_pipeline import log_line as log_line


@dataclass(frozen=True)
class Step2StageResult:
    ok: bool
    symbols_info: list[dict]
    benchmark_context: dict
    details: dict
    summary_item: dict
    blocking_failure: bool


@dataclass(frozen=True)
class Step3StageResult:
    report_text: str
    springboard_codes: list[str]
    springboard_updates: dict[str, dict]
    summary_item: dict
    # code6 -> invalidated / building / springboard。纯观测字段，用于事后评估
    # LLM 是否真有区分度；不参与任何交易决策。
    verdicts: dict[str, str] = field(default_factory=dict)

    @property
    def blocking_failure(self) -> bool:
        return not bool(self.summary_item.get("ok"))


class TeeStream:
    def __init__(self, console_stream, file_stream):
        self.console_stream = console_stream
        self.file_stream = file_stream

    def write(self, data: str) -> int:
        self.console_stream.write(data)
        self.file_stream.write(data)
        return len(data)

    def flush(self) -> None:
        self.console_stream.flush()
        self.file_stream.flush()


def run_with_stdout_tee(logs_path: str | None, fn, *args, **kwargs):
    if not logs_path:
        return fn(*args, **kwargs)
    os.makedirs(os.path.dirname(logs_path) or ".", exist_ok=True)
    with open(logs_path, "a", encoding="utf-8") as log_file:
        tee = TeeStream(sys.stdout, log_file)
        with redirect_stdout(tee), redirect_stderr(tee):
            return fn(*args, **kwargs)


def stage_summary(step: str, output: str) -> dict:
    return {"step": step, "ok": True, "err": None, "elapsed_s": 0, "output": output}
