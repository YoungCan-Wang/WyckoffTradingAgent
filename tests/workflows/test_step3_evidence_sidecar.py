from __future__ import annotations

from workflows.step3_delivery import write_step3_evidence_sidecar


def test_evidence_sidecar_keeps_counts(tmp_path, monkeypatch):
    monkeypatch.setenv("STEP3_EVIDENCE_PATH", str(tmp_path / "snap.json"))
    path = write_step3_evidence_sidecar(
        codes=["000001"],
        veto_lines=["news"],
        prose="模型说要买",
        selected_count=1,
    )
    text = (tmp_path / "snap.json").read_text(encoding="utf-8")
    assert path.endswith("snap.json")
    assert '"selected_count": 1' in text
    assert "模型说要买" in text
