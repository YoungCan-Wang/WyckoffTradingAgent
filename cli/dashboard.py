"""
Wyckoff Dashboard — 本地可视化面板。

stdlib http.server 提供 JSON API + 嵌入式 HTML/CSS/JS SPA。
金融终端风格（Bloomberg 深色主题）。
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

from cli.dashboard_html import DASHBOARD_HTML

logger = logging.getLogger(__name__)
_DASHBOARD_TOKEN = secrets.token_urlsafe(32)

# ---------------------------------------------------------------------------
# Data access layer (thin wrappers over local_db)
# ---------------------------------------------------------------------------


def _get_config() -> dict:
    try:
        from cli.auth import load_config, load_default_model_id, load_fallback_model_id, load_model_configs

        cfg = load_config()
        models = load_model_configs()
        default_id = load_default_model_id()
        fallback_id = load_fallback_model_id()
        # mask sensitive keys
        safe = {}
        for k, v in cfg.items():
            sv = str(v or "")
            if any(s in k.lower() for s in ("key", "token", "secret", "password")):
                safe[k] = (sv[:4] + "****" + sv[-4:]) if len(sv) > 8 else ("****" if sv else "")
            else:
                safe[k] = v
        safe_models = []
        for m in models:
            mc = dict(m)
            ak = str(mc.get("api_key", "") or "")
            mc["api_key"] = (ak[:4] + "****" + ak[-4:]) if len(ak) > 8 else ("****" if ak else "")
            safe_models.append(mc)
        for k in ("models", "default", "fallback", "light"):
            safe.pop(k, None)
        return {"config": safe, "models": safe_models, "default_model": default_id, "fallback_model": fallback_id}
    except Exception as e:
        return {"config": {}, "models": [], "default_model": "", "error": str(e)}


def _get_memory() -> list[dict]:
    try:
        from integrations.local_db import get_recent_memories

        return get_recent_memories(limit=50)
    except Exception:
        return []


def _delete_memory(mem_id: int) -> bool:
    try:
        from integrations.local_db import get_db

        conn = get_db()
        with conn:
            conn.execute("DELETE FROM agent_memory WHERE id=?", (mem_id,))
        return True
    except Exception:
        return False


def _delete_recommendation(code: str) -> int:
    try:
        from integrations.local_db import delete_recommendations

        return delete_recommendations([code])
    except Exception:
        return 0


def _delete_signal(code: str) -> int:
    try:
        from integrations.local_db import delete_signals

        return delete_signals([code])
    except Exception:
        return 0


def _delete_chat_session(session_id: str) -> int:
    try:
        from integrations.local_db import delete_chat_session

        return delete_chat_session(session_id)
    except Exception:
        return 0


def _get_recommendations() -> list[dict]:
    try:
        from integrations.local_db import load_recommendations

        return load_recommendations(limit=100)
    except Exception:
        return []


def _get_signals() -> list[dict]:
    try:
        from integrations.local_db import load_signals

        return load_signals(limit=200)
    except Exception:
        return []


def _get_portfolio() -> dict | None:
    try:
        from integrations.local_db import load_portfolio

        return load_portfolio("USER_LIVE")
    except Exception:
        return None


def _get_sync_status() -> list[dict]:
    try:
        from integrations.local_db import get_sync_meta

        tables = ["recommendation_tracking", "signal_pending", "market_signal_daily", "portfolio"]
        result = []
        for t in tables:
            meta = get_sync_meta(t)
            result.append({"table": t, **(meta or {"row_count": 0, "last_synced_at": None})})
        return result
    except Exception:
        return []


def _get_chat_sessions() -> list[dict]:
    try:
        from integrations.local_db import list_chat_sessions

        return list_chat_sessions(limit=50)
    except Exception:
        return []


def _get_chat_log(session_id: str) -> list[dict]:
    try:
        from integrations.local_db import load_chat_logs

        return load_chat_logs(session_id=session_id)
    except Exception:
        return []


def _get_background_tasks() -> list[dict]:
    try:
        from integrations.local_db import load_background_task_results

        return load_background_task_results(limit=100)
    except Exception:
        return []


def _get_background_task(task_id: str) -> dict:
    try:
        from integrations.local_db import load_background_task_result

        return load_background_task_result(task_id) or {}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# HTTP Handler
# ---------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002
        pass  # silence request logs

    def _json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, html: str):
        html = html.replace("</head>", f'<meta name="wyckoff-dashboard-token" content="{_DASHBOARD_TOKEN}"></head>', 1)
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._trusted_request(allow_page=True):
            return
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path == "/api/config":
            self._json(_get_config())
        elif path == "/api/memory":
            self._json(_get_memory())
        elif path == "/api/recommendations":
            self._json(_get_recommendations())
        elif path == "/api/signals":
            self._json(_get_signals())
        elif path == "/api/portfolio":
            self._json(_get_portfolio() or {})
        elif path == "/api/sync":
            self._json(_get_sync_status())
        elif path == "/api/chat-sessions":
            self._json(_get_chat_sessions())
        elif path.startswith("/api/chat-log/"):
            sid = path.split("/")[-1]
            self._json(_get_chat_log(sid))
        elif path == "/api/background-tasks":
            self._json(_get_background_tasks())
        elif path.startswith("/api/background-tasks/"):
            task_id = path.split("/")[-1]
            self._json(_get_background_task(task_id))
        else:
            self._html(DASHBOARD_HTML)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _trusted_request(self, *, allow_page: bool = False) -> bool:
        port = self.server.server_port
        host = self.headers.get("Host", "")
        allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}", host}
        origin = self.headers.get("Origin", "")
        if origin:
            allowed_origins = {f"http://{h}" for h in allowed_hosts} | {f"https://{h}" for h in allowed_hosts}
            if origin not in allowed_origins:
                self._json({"error": "cross-origin request blocked"}, 403)
                return False
        if allow_page and not self.path.startswith("/api/"):
            return True
        if secrets.compare_digest(self.headers.get("X-Wyckoff-Token", ""), _DASHBOARD_TOKEN):
            return True
        self._json({"error": "invalid dashboard token"}, 403)
        return False

    def _trusted_write(self) -> bool:
        if not self._trusted_request():
            return False
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            self._json({"error": "application/json required"}, 415)
            return False
        return True

    def do_POST(self):
        if not self._trusted_write():
            return
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        if path == "/api/models":
            try:
                body = self._read_body()
                from cli.auth import save_model_entry

                save_model_entry(body)
                self._json({"ok": True})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 400)
        else:
            self._json({"error": "not found"}, 404)

    def do_PUT(self):
        if not self._trusted_write():
            return
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        if path.startswith("/api/models/") and path.endswith("/default"):
            model_id = path.split("/")[-2]
            try:
                from cli.auth import set_default_model

                set_default_model(model_id)
                self._json({"ok": True})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 400)
        elif path.startswith("/api/models/") and path.endswith("/fallback"):
            model_id = path.split("/")[-2]
            try:
                from cli.auth import set_fallback_model

                set_fallback_model(model_id)
                self._json({"ok": True})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 400)
        elif path.startswith("/api/models/"):
            model_id = path.split("/")[-1]
            try:
                body = self._read_body()
                body["id"] = model_id
                if not body.get("api_key"):
                    from cli.auth import load_model_configs

                    for m in load_model_configs():
                        if m["id"] == model_id:
                            body["api_key"] = m["api_key"]
                            break
                from cli.auth import save_model_entry

                save_model_entry(body)
                self._json({"ok": True})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 400)
        elif path.startswith("/api/config/"):
            key = path.split("/")[-1]
            try:
                body = self._read_body()
                from cli.auth import save_config_key

                save_config_key(key, body.get("value", ""))
                self._json({"ok": True})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 400)
        else:
            self._json({"error": "not found"}, 404)

    def do_DELETE(self):
        if not self._trusted_write():
            return
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        if path.startswith("/api/models/"):
            model_id = path.split("/")[-1]
            try:
                from cli.auth import remove_model_entry

                ok = remove_model_entry(model_id)
                self._json({"ok": ok, "error": "" if ok else "cannot delete last model"})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 400)
        elif path.startswith("/api/memory/"):
            try:
                mem_id = int(path.split("/")[-1])
                ok = _delete_memory(mem_id)
                self._json({"ok": ok})
            except ValueError:
                self._json({"ok": False, "error": "invalid id"}, 400)
        elif path.startswith("/api/recommendations/"):
            code = path.split("/")[-1]
            n = _delete_recommendation(code)
            self._json({"ok": n > 0, "deleted": n})
        elif path.startswith("/api/signals/"):
            code = path.split("/")[-1]
            n = _delete_signal(code)
            self._json({"ok": n > 0, "deleted": n})
        elif path.startswith("/api/chat-sessions/"):
            sid = path.split("/")[-1]
            n = _delete_chat_session(sid)
            self._json({"ok": n > 0, "deleted": n})
        else:
            self._json({"error": "not found"}, 404)


# ---------------------------------------------------------------------------
# Server start
# ---------------------------------------------------------------------------


def start_dashboard_background(port: int = 8765):
    """后台静默启动 dashboard（daemon 线程调用，不打开浏览器）。"""
    server = HTTPServer(("0.0.0.0", port), _Handler)
    server.allow_reuse_address = True
    server.serve_forever()


def _port_in_use(port: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def start_dashboard(port: int = 8765):
    """启动 dashboard HTTP 服务并打开浏览器。"""
    url = f"http://127.0.0.1:{port}"

    if _port_in_use(port):
        print(f"Dashboard 已在运行: {url}")
        webbrowser.open(url)
        return

    from integrations.local_db import init_db

    init_db()

    server = HTTPServer(("0.0.0.0", port), _Handler)
    server.allow_reuse_address = True
    print(f"Wyckoff Dashboard: {url}")
    print("按 Ctrl+C 停止")

    threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.debug("dashboard server stopped by user", exc_info=True)
    finally:
        server.server_close()
