"""官方 DeepSeek 推理预算与兼容端点边界回归。"""

from __future__ import annotations

import pytest

from integrations import llm_client


class _Resp:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status
        self.text = "err"

    def json(self) -> dict:
        return self._payload


def _body(content: str, finish: str = "stop", reasoning_tokens: int = 0, reasoning: str = "") -> dict:
    return {
        "choices": [{"finish_reason": finish, "message": {"content": content, "reasoning_content": reasoning}}],
        "usage": {
            "completion_tokens": reasoning_tokens + len(content),
            "completion_tokens_details": {"reasoning_tokens": reasoning_tokens},
        },
    }


def _patch_post(monkeypatch: pytest.MonkeyPatch, responses: list[_Resp]) -> list[dict]:
    calls: list[dict] = []

    def _post(url, headers=None, json=None, timeout=None):  # noqa: A002
        calls.append(dict(json or {}))
        return responses[min(len(calls) - 1, len(responses) - 1)]

    monkeypatch.setattr("requests.post", _post)
    return calls


def _call(
    max_output_tokens: int | None = 6000,
    *,
    allow_truncated_text: bool = False,
    base_url: str = "https://api.deepseek.com/v1",
    provider: str = "deepseek",
) -> str:
    return llm_client._call_openai_compatible(
        base_url,
        "k",
        "deepseek-v4-flash",
        "sys",
        "user",
        60,
        max_output_tokens,
        provider=provider,
        allow_truncated_text=allow_truncated_text,
    )


def test_normal_response_returns_content_without_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_post(monkeypatch, [_Resp(_body("正文"))])

    assert _call() == "正文"
    assert len(calls) == 1
    assert calls[0]["thinking"] == {"type": "enabled"}
    assert calls[0]["reasoning_effort"] == "low"
    assert "temperature" not in calls[0]


def test_reasoning_exhausted_budget_retries_with_larger_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """回归：flash 在 6000 下推理占满、content 空；放大到 12000 后应拿到正文。"""
    calls = _patch_post(
        monkeypatch,
        [_Resp(_body("", finish="length", reasoning_tokens=6000, reasoning="想" * 500)), _Resp(_body("正文"))],
    )

    assert _call(6000) == "正文"
    assert [c["max_tokens"] for c in calls] == [6000, 12000]


def test_reasoning_text_triggers_retry_when_token_breakdown_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_post(
        monkeypatch,
        [_Resp(_body("", finish="length", reasoning="仍在推理")), _Resp(_body("正文"))],
    )

    assert _call(6000) == "正文"
    assert [c["max_tokens"] for c in calls] == [6000, 12000]


def test_retry_happens_only_once(monkeypatch: pytest.MonkeyPatch) -> None:
    empty = _Resp(_body("", finish="length", reasoning_tokens=6000, reasoning="想"))
    calls = _patch_post(monkeypatch, [empty])

    with pytest.raises(RuntimeError, match="返回内容为空"):
        _call(6000)
    assert len(calls) == 2


def test_partial_truncation_retries_for_complete_text(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_post(
        monkeypatch,
        [_Resp(_body("半段", finish="length", reasoning_tokens=100)), _Resp(_body("完整正文"))],
    )

    assert _call(6000) == "完整正文"
    assert [call["max_tokens"] for call in calls] == [6000, 12000]


def test_partial_truncation_can_be_explicitly_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_post(monkeypatch, [_Resp(_body("允许的半段", finish="length", reasoning_tokens=100))])

    assert _call(6000, allow_truncated_text=True) == "允许的半段"
    assert len(calls) == 1


def test_proxy_endpoint_keeps_generic_truncation_semantics(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_post(monkeypatch, [_Resp(_body("代理返回的半段", finish="length", reasoning_tokens=100))])

    assert _call(base_url="https://api.example.com/v1") == "代理返回的半段"
    assert len(calls) == 1
    assert "thinking" not in calls[0]
    assert calls[0]["temperature"] == 0.4


def test_model_name_does_not_enable_official_fields_on_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_post(monkeypatch, [_Resp(_body("网关正文"))])

    assert _call(base_url="https://api.1route.dev/v1", provider="1route") == "网关正文"
    assert "thinking" not in calls[0]


def test_no_retry_when_finish_reason_is_not_length(monkeypatch: pytest.MonkeyPatch) -> None:
    """空正文但正常结束属别的故障，放大预算无意义，不该多花一次调用。"""
    calls = _patch_post(monkeypatch, [_Resp(_body("", finish="stop", reasoning_tokens=0))])

    with pytest.raises(RuntimeError, match="返回内容为空"):
        _call(6000)
    assert len(calls) == 1


def test_no_retry_when_model_is_not_reasoning(monkeypatch: pytest.MonkeyPatch) -> None:
    """非推理模型截断说明正文本身太长，加预算前应先怀疑提示，不自动放大。"""
    calls = _patch_post(monkeypatch, [_Resp(_body("", finish="length", reasoning_tokens=0))])

    with pytest.raises(RuntimeError, match="返回内容为空"):
        _call(6000)
    assert len(calls) == 1


def test_budget_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    """已达上限时不再放大，避免无界抬预算。"""
    cap = llm_client.OPENAI_COMPATIBLE_MAX_BUDGET
    calls = _patch_post(monkeypatch, [_Resp(_body("", finish="length", reasoning_tokens=cap))])

    with pytest.raises(RuntimeError, match="返回内容为空"):
        _call(cap)
    assert len(calls) == 1


def test_diagnosis_is_logged(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """报错必须带 finish_reason 与推理占用——缺了它们无法定性，这正是本次排查的痛点。"""
    _patch_post(monkeypatch, [_Resp(_body("", finish="length", reasoning_tokens=6000, reasoning="想" * 10))])

    with caplog.at_level("ERROR"), pytest.raises(RuntimeError):
        _call(6000)

    blob = caplog.text
    assert "finish_reason=length" in blob
    assert "reasoning_tokens=6000" in blob


def test_http_error_still_raises_with_status(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_post(monkeypatch, [_Resp({}, status=500)])

    with pytest.raises(RuntimeError, match="HTTP 500"):
        _call()


def test_missing_choices_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_post(monkeypatch, [_Resp({"choices": []})])

    with pytest.raises(RuntimeError, match="无 choices"):
        _call()
