from pathlib import Path
from types import SimpleNamespace


def test_cli_entry_prefers_packaged_core(monkeypatch, tmp_path):
    fake_core = tmp_path / "core"
    fake_core.mkdir()
    (fake_core / "__init__.py").write_text("# shadow package\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))

    import cli.__main__  # noqa: F401
    import core.candidate_ranker as candidate_ranker

    expected_root = Path(__file__).resolve().parents[1] / "core"
    assert Path(candidate_ranker.__file__).resolve().parent == expected_root


def test_cli_main_loads_dotenv_at_execution_time(monkeypatch):
    import dotenv

    from cli import __main__ as cli_main

    calls = []
    args = SimpleNamespace(command="test")
    monkeypatch.setattr(dotenv, "load_dotenv", lambda: calls.append("dotenv"))
    monkeypatch.setattr(cli_main, "_set_terminal_title", lambda _title: None)
    monkeypatch.setattr(cli_main, "_build_parser", lambda: SimpleNamespace(parse_args=lambda: args))
    monkeypatch.setattr(cli_main, "_dispatch_command", lambda actual: calls.append(actual))

    cli_main.main()

    assert calls == ["dotenv", args]
