import pytest

from integrations.local_auth import (
    DEFAULT_STREAM_CHUNK_TIMEOUT_SECONDS,
    DEFAULT_TOOL_TIMEOUT_SECONDS,
    coerce_timeout_config_value,
    get_stream_chunk_timeout_seconds,
    get_tool_timeout_seconds,
    timeout_config_defaults,
)


def test_timeout_defaults():
    assert timeout_config_defaults() == {
        "stream_chunk_timeout_seconds": DEFAULT_STREAM_CHUNK_TIMEOUT_SECONDS,
        "tool_timeout_seconds": DEFAULT_TOOL_TIMEOUT_SECONDS,
    }
    assert get_stream_chunk_timeout_seconds({}) == float(DEFAULT_STREAM_CHUNK_TIMEOUT_SECONDS)
    assert get_tool_timeout_seconds({}) == float(DEFAULT_TOOL_TIMEOUT_SECONDS)


def test_timeout_reads_config_and_clamps():
    assert get_stream_chunk_timeout_seconds({"stream_chunk_timeout_seconds": 180}) == 180.0
    assert get_tool_timeout_seconds({"tool_timeout_seconds": "45"}) == 45.0
    assert get_stream_chunk_timeout_seconds({"stream_chunk_timeout_seconds": 9999}) == 600.0
    assert get_tool_timeout_seconds({"tool_timeout_seconds": 1}) == 5.0


def test_coerce_timeout_rejects_invalid():
    assert coerce_timeout_config_value("stream_chunk_timeout_seconds", "90") == 90
    with pytest.raises(ValueError, match="整数"):
        coerce_timeout_config_value("tool_timeout_seconds", "abc")
    with pytest.raises(ValueError, match="之间"):
        coerce_timeout_config_value("tool_timeout_seconds", 1000)
