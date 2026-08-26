"""
Supabase 客户端工厂 — CLI / 脚本 / Web 通用。

所有需要 Supabase 客户端的代码应从此模块获取，而不是各自 create_client。
- 脚本/定时任务：使用 create_admin_client()（service_role key，绕过 RLS）
- CLI：使用 create_read_client() / create_user_client()，默认不得写共享表
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from supabase import Client

logger = logging.getLogger(__name__)

WRITE_CONTEXT_ENV = "WYCKOFF_WRITE_CONTEXT"
SERVER_WRITE_CONTEXT = "server_job"
CLI_WRITE_CONTEXT = "cli"

# 网络超时。
#
# 这是实测踩出来的：桌面端切到持仓页永久卡在「读取中…」。量下来 `account`
# 25ms 返回，`portfolio` 超过 15s 从未结束 —— 它先走 Supabase，而
# `client.auth.set_session` 卡在 TLS 握手上。
#
# supabase-py 的默认值救不了这个场景：
# - `postgrest_client_timeout` 默认 **120s**，等于界面挂两分钟
# - auth 那个 gotrue client **没有任何超时选项**，只能靠注入自己的 httpx client
#
# 所以统一传 `httpx_client`：实测它同时被 auth 和 postgrest 采用（两处
# `is ours` 都为 True），一处设置覆盖两条链路。
#
# 取值：连接 5s、读写 8s。持仓页是交互路径，用户在等；宁可快速失败落到本地库，
# 也不要让他对着转圈猜。定时任务那类后台路径本来就允许重试。
CONNECT_TIMEOUT_SECONDS = 5.0
READ_TIMEOUT_SECONDS = 8.0


def _timeout_client():
    """带超时的 httpx client。

    每次 create_* 都新建一个：httpx.Client 不是线程安全的复用对象，而这里的
    调用方既有 IPC 线程池也有定时任务。共享一个实例省不下多少，却会引入
    「一个请求把连接池占满，另一个跟着挂」这类难查的问题。
    """
    import httpx

    return httpx.Client(timeout=httpx.Timeout(READ_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS))


def _client_options():
    """统一的 ClientOptions —— 只为把超时注进去。"""
    from supabase import ClientOptions

    return ClientOptions(httpx_client=_timeout_client())


def _resolve_credentials() -> tuple[str, str]:
    """解析 Supabase URL 和 Key，统一回退链：环境变量 → 内置 anon key。"""
    url = os.getenv("SUPABASE_URL", "").strip()
    key = os.getenv("SUPABASE_KEY", "").strip()
    if url and key:
        return url, key
    # 内置 anon key（CLI / 无 .env 场景）
    from integrations.supabase_public_config import SUPABASE_ANON_KEY, SUPABASE_ANON_URL

    url = url or SUPABASE_ANON_URL
    key = key or SUPABASE_ANON_KEY
    if url and key:
        return url, key
    return url, key


def create_admin_client() -> Client:
    """Service-role 客户端（写库用，不经过 RLS）。

    仅允许显式 service role。不要回退到 anon key，避免 CLI 误拿
    ``create_admin_client`` 当通用读写入口。
    """
    url = os.getenv("SUPABASE_URL", "").strip()
    service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if not url or not service_key:
        raise ValueError("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY 未配置")
    from supabase import create_client

    return create_client(url, service_key, options=_client_options())


def create_anon_client() -> Client:
    """Anon-key 客户端（RLS 保护）。"""
    from supabase import create_client

    url, key = _resolve_credentials()
    if not url or not key:
        raise ValueError("Missing Supabase credentials. Please set SUPABASE_URL and SUPABASE_KEY.")
    return create_client(url, key, options=_client_options())


def create_read_client() -> Client:
    """只读场景客户端。

    Server job 可用 service role 读取生产表；CLI 默认走 anon/RLS，
    需要用户级权限的场景应显式传入 create_user_client() 的结果。
    """
    if current_write_context() == SERVER_WRITE_CONTEXT and is_admin_configured():
        return create_admin_client()
    return create_anon_client()


def create_user_client(access_token: str, refresh_token: str = "") -> Client:
    """用用户 JWT 创建客户端（通过 RLS）。

    set_session 会消耗 refresh_token 并返回新 token pair，
    调用者应通过 get_session_tokens() 获取刷新后的 token 并回写。
    """
    from supabase import create_client

    url, key = _resolve_credentials()
    if not url or not key:
        raise ValueError("SUPABASE_URL / SUPABASE_KEY 未配置")
    client = create_client(url, key, options=_client_options())
    if refresh_token:
        resp = client.auth.set_session(access_token, refresh_token)
        # set_session 返回新 token pair，用新的 access_token 做 postgrest auth
        new_at = getattr(resp, "access_token", None) or (
            resp.session.access_token if hasattr(resp, "session") and resp.session else None
        )
        if new_at:
            access_token = new_at
    client.postgrest.auth(access_token)
    return client


def get_session_tokens(client: Client) -> tuple[str, str]:
    """从 client 中提取当前有效的 access_token 和 refresh_token。"""
    try:
        session = client.auth.get_session()
        if session:
            return session.access_token or "", session.refresh_token or ""
    except Exception:
        logger.debug("failed to retrieve session tokens", exc_info=True)
    return "", ""


def close_client(client: Client) -> None:
    """关闭 Supabase client 底层的 httpx 连接，避免 CLOSE_WAIT 泄漏。"""
    try:
        rest = getattr(client, "_postgrest", None) or getattr(client, "postgrest", None)
        if rest:
            session = getattr(rest, "session", None) or getattr(rest, "_session", None)
            if session and hasattr(session, "aclose"):
                import asyncio

                try:
                    asyncio.get_event_loop().run_until_complete(session.aclose())
                except Exception:
                    pass
            elif session and hasattr(session, "close"):
                session.close()
    except Exception:
        logger.debug("close_client: postgrest session close failed", exc_info=True)
    try:
        auth = getattr(client, "auth", None)
        if auth:
            http = getattr(auth, "_http_client", None) or getattr(auth, "http_client", None)
            if http and hasattr(http, "close"):
                http.close()
    except Exception:
        logger.debug("close_client: auth http close failed", exc_info=True)


def is_admin_configured() -> bool:
    """检查是否存在显式 Supabase 凭据。

    说明：
    - 这里用于判断“是否完成业务级配置”，不应把内置 anon 凭据视为“已配置”。
    - 因此不走 _resolve_credentials()（该函数会回退到内置 anon）。
    """
    url = os.getenv("SUPABASE_URL", "").strip()
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if url and key:
        return True
    return False


def current_write_context() -> str:
    return os.getenv(WRITE_CONTEXT_ENV, CLI_WRITE_CONTEXT).strip().lower() or CLI_WRITE_CONTEXT


def is_server_write_context() -> bool:
    return current_write_context() == SERVER_WRITE_CONTEXT


def require_server_write_context(operation: str = "Supabase shared write") -> None:
    """共享生产表写入守卫。

    CLI 只有持仓写入可用用户 JWT；信号、推荐、策略等共享表必须由
    GitHub Actions / server job 显式设置 ``WYCKOFF_WRITE_CONTEXT=server_job``。
    """
    if not is_server_write_context():
        raise PermissionError(f"{operation} requires {WRITE_CONTEXT_ENV}={SERVER_WRITE_CONTEXT}")
