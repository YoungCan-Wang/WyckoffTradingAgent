"""CLI compatibility exports for local auth and model configuration."""

from __future__ import annotations

from integrations.local_auth import (
    CONFIG_FILE as CONFIG_FILE,
)
from integrations.local_auth import (
    DEFAULT_STREAM_CHUNK_TIMEOUT_SECONDS as DEFAULT_STREAM_CHUNK_TIMEOUT_SECONDS,
)
from integrations.local_auth import (
    DEFAULT_TOOL_TIMEOUT_SECONDS as DEFAULT_TOOL_TIMEOUT_SECONDS,
)
from integrations.local_auth import (
    SESSION_DIR as SESSION_DIR,
)
from integrations.local_auth import (
    SESSION_FILE as SESSION_FILE,
)
from integrations.local_auth import (
    TIMEOUT_CONFIG_SPECS as TIMEOUT_CONFIG_SPECS,
)
from integrations.local_auth import (
    auto_relogin as auto_relogin,
)
from integrations.local_auth import (
    clear_session as clear_session,
)
from integrations.local_auth import (
    coerce_timeout_config_value as coerce_timeout_config_value,
)
from integrations.local_auth import (
    get_stream_chunk_timeout_seconds as get_stream_chunk_timeout_seconds,
)
from integrations.local_auth import (
    get_tool_timeout_seconds as get_tool_timeout_seconds,
)
from integrations.local_auth import (
    load_config as load_config,
)
from integrations.local_auth import (
    load_default_model_id as load_default_model_id,
)
from integrations.local_auth import (
    load_fallback_model_id as load_fallback_model_id,
)
from integrations.local_auth import (
    load_model_config as load_model_config,
)
from integrations.local_auth import (
    load_model_configs as load_model_configs,
)
from integrations.local_auth import (
    load_session as load_session,
)
from integrations.local_auth import (
    login as login,
)
from integrations.local_auth import (
    logout as logout,
)
from integrations.local_auth import (
    remove_model_entry as remove_model_entry,
)
from integrations.local_auth import (
    restore_session as restore_session,
)
from integrations.local_auth import (
    save_config_key as save_config_key,
)
from integrations.local_auth import (
    save_model_config as save_model_config,
)
from integrations.local_auth import (
    save_model_entry as save_model_entry,
)
from integrations.local_auth import (
    save_session as save_session,
)
from integrations.local_auth import (
    set_default_model as set_default_model,
)
from integrations.local_auth import (
    set_fallback_model as set_fallback_model,
)
from integrations.local_auth import (
    timeout_config_defaults as timeout_config_defaults,
)

_clear_session = clear_session
