from core.deepseek import (
    deepseek_chat_extra_body,
    is_deepseek_v4_model,
    is_official_deepseek_url,
    normalize_deepseek_reasoning_level,
    resolve_official_deepseek_model,
)


def test_deepseek_v4_capability_detection():
    assert is_deepseek_v4_model("deepseek-v4-flash")
    assert is_deepseek_v4_model("deepseek-v4-pro-0813")
    assert not is_deepseek_v4_model("deepseek-chat")
    assert is_official_deepseek_url("https://api.deepseek.com/v1")
    assert not is_official_deepseek_url("https://example.com/v1")


def test_retired_aliases_are_migrated_only_on_the_official_endpoint():
    assert resolve_official_deepseek_model("deepseek-chat", "https://api.deepseek.com/v1") == (
        "deepseek-v4-flash",
        "off",
    )
    assert resolve_official_deepseek_model("deepseek-reasoner", "https://api.deepseek.com/v1") == (
        "deepseek-v4-flash",
        "high",
    )
    assert resolve_official_deepseek_model("deepseek-chat", "https://proxy.example.com/v1") == (
        "deepseek-chat",
        None,
    )


def test_deepseek_reasoning_policy_normalization():
    assert normalize_deepseek_reasoning_level("xhigh") == "high"
    assert deepseek_chat_extra_body("off") == {"thinking": {"type": "disabled"}}
    assert deepseek_chat_extra_body("max") == {
        "thinking": {"type": "enabled"},
        "reasoning_effort": "max",
    }
