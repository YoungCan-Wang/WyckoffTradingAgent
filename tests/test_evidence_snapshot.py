from __future__ import annotations

import pytest

from core.evidence_snapshot import freeze_evidence, merge_llm_prose, numbers_unchanged, write_evidence_snapshot


def test_llm_prose_cannot_overwrite_numbers(tmp_path):
    evidence = freeze_evidence(
        {"close": 10.5, "score": 2.0, "codes": ["000001"], "veto_lines": ["x"], "note": "ignored"}
    )
    merged = merge_llm_prose(evidence, "模型认为涨了", {"tone": "calm"})
    assert merged["prose"] == "模型认为涨了"
    assert merged["tone"] == "calm"
    assert merged["numbers"]["close"] == 10.5
    assert numbers_unchanged(evidence, merged)
    path = write_evidence_snapshot(merged, tmp_path / "snapshot.json")
    assert path.is_file()
    with pytest.raises(ValueError):
        merge_llm_prose(evidence, "x", {"numbers": {"close": 99}})
