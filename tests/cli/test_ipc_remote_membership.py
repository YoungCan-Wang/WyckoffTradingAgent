"""远程遥控 403：报错要带上加入入口，而不是只说「暂时不能使用」。"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from cli.ipc import methods
from integrations.planet_membership import PLANET_MEMBERSHIP_URL


class _Forbidden:
    def __init__(self, *_a, **_k) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_a) -> None:
        return None

    def request(self, *_a, **_k):
        return SimpleNamespace(status_code=403, json=dict)


def test_remote_403_carries_the_membership_entry(monkeypatch) -> None:
    monkeypatch.setattr(methods, "_remote_credentials", lambda: ("access", "u-1"))
    monkeypatch.setattr(httpx, "Client", _Forbidden)

    with pytest.raises(methods.MethodError) as info:
        methods._remote_http("devices")

    assert info.value.code == "planet_membership_required"
    assert PLANET_MEMBERSHIP_URL in info.value.message
