from scripts.release_scope import affects_scope


def test_pypi_scope_tracks_distribution_inputs():
    assert affects_scope("pypi", ["core/analyzer.py"])
    assert affects_scope("pypi", ["scripts/daily_job.py"])
    assert affects_scope("pypi", ["config/profiles/a_share_prod.yml"])
    assert affects_scope("pypi", ["data/market_universes/hk.json"])
    assert affects_scope("pypi", ["README.md"])


def test_pypi_scope_ignores_non_package_changes():
    assert not affects_scope("pypi", ["desktop/src/main.js"])
    assert not affects_scope("pypi", ["web/apps/web/src/main.tsx"])
    assert not affects_scope("pypi", ["docs/ARCHITECTURE.md"])
    assert not affects_scope("pypi", ["tests/core/test_analyzer.py"])
    assert not affects_scope("pypi", ["uv.lock"])


def test_worker_scope_tracks_runtime_and_workspace_lock():
    assert affects_scope("worker", ["web/apps/api/src/index.ts"])
    assert affects_scope("worker", ["web/packages/shared/src/constants.ts"])
    assert affects_scope("worker", ["web/pnpm-lock.yaml"])
    assert not affects_scope("worker", ["web/apps/web/src/main.tsx"])
    assert not affects_scope("worker", ["desktop/src/main.js"])
