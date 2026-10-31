"""桌面端登录，以及登录后把云端配置拉到本地。

## 为什么需要这些

桌面端原来**根本没有登录路径**：`AccountMenu` 里只有「退出登录」，未登录时那个
菜单等于只剩设置 —— 点开是死路。后端 `local_auth.login` 一直存在（CLI 在用），
但 IPC 层没暴露。

云端配置拉取是一条**新链路**：Python 侧从来没读过 `user_settings` 表，本地配置和
云端配置一直是两套独立的东西。用户可能在 web 端配好了模型和数据源，桌面端登录后
不拉下来就仍然显示「未配置模型」。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import integrations.local_auth as la
from cli.ipc import cloud_config as cc


@pytest.fixture
def store(tmp_path, monkeypatch):
    """把 session 与 config 指到临时目录。"""
    monkeypatch.setattr(la, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(la, "SESSION_FILE", tmp_path / "session.json")
    monkeypatch.setattr(la, "CONFIG_FILE", tmp_path / "wyckoff.json")
    return tmp_path


CLOUD_ROW = {
    "chat_provider": "gemini",
    "gemini_api_key": "G-KEY",
    "gemini_model": "gemini-2.5-pro",
    "gemini_base_url": "",
    "openai_api_key": "O-KEY",
    "openai_model": "gpt-4o",
    "openai_base_url": "https://proxy.example",
    "deepseek_api_key": "",
    "deepseek_model": "",
    "anthropic_api_key": "A-KEY",
    "anthropic_model": "claude-4",
    "anthropic_base_url": "",
    "tickflow_api_key": "TF-KEY",
    "tushare_token": "TS-TOKEN",
}


@pytest.fixture
def cloud(monkeypatch):
    monkeypatch.setattr(cc, "_fetch_settings", lambda *_a: dict(CLOUD_ROW))


@pytest.fixture
def signed_in(store):
    """pull_cloud_config 只在磁盘仍是同一 user_id 时落盘；测试需先有匹配会话。"""
    la.save_session({"user_id": "u1", "email": "a@b.c", "access_token": "tok", "refresh_token": "r"})
    return store


class TestLogoutClearsCredentials:
    """logout 必须真的退出登录。"""

    def test_logout_clears_auto_relogin_credentials(self, store):
        """只删 session 是不够的 —— auto_relogin 会把人静默登回去。

        `login()` 把邮箱和明文密码写进 config 供 `auto_relogin()` 用，而
        `agents/tool_context.py` 在 token 失效时会调它。实测复现过：退出登录后
        下一次工具调用又变成已登录。
        """
        la.save_session({"user_id": "u1", "email": "a@b.c", "access_token": "t", "refresh_token": "r"})
        la.save_config_key("email", "a@b.c")
        la.save_config_key("password", "secret")

        la.logout()

        assert not la.SESSION_FILE.exists()
        assert la.auto_relogin() is None, "凭据没清干净，会被自动登回去"

    def test_logout_keeps_last_email_for_prefill(self, store):
        """`last_email` 刻意保留 —— 它是「你是谁」，不是凭据。

        拆成两个键才能让「清凭据」和「记住是谁」互不干扰：`email` 参与
        auto_relogin 所以必须清，`last_email` 只用于下次登录页预填邮箱。
        拿到它并不代表能登录。
        """
        la.save_config_key("email", "a@b.c")
        la.save_config_key("password", "secret")
        la.save_config_key("last_email", "a@b.c")

        la.logout()

        config = json.loads(la.CONFIG_FILE.read_text(encoding="utf-8"))
        assert config.get("last_email") == "a@b.c", "预填邮箱不该被清"
        assert not config.get("email"), "email 必须清 —— 它能自动登录"
        assert la.auto_relogin() is None, "保留 last_email 不能让退出失效"

    def test_account_exposes_last_email_but_never_password(self, store, monkeypatch):
        """登录页要拿到预填邮箱；密码绝不能经这条路出去。"""
        import cli.ipc.methods as m

        la.save_config_key("last_email", "a@b.c")
        la.save_config_key("password", "top-secret")
        monkeypatch.setattr("integrations.local_auth.restore_session", lambda: None)

        event = list(m.account({}))[0]
        assert event["last_email"] == "a@b.c"
        assert "top-secret" not in json.dumps(event)

    def test_logout_keeps_unrelated_config(self, store):
        """清凭据不该顺手清掉主题、模型这些无关配置。"""
        la.save_config_key("theme", "dark")
        la.save_config_key("password", "secret")
        la.logout()
        assert json.loads(la.CONFIG_FILE.read_text(encoding="utf-8")).get("theme") == "dark"


class TestCloudConfigPull:
    def test_blank_machine_gets_everything(self, signed_in, cloud):
        out = cc.pull_cloud_config("u1", "tok")
        assert out["available"] is True
        assert sorted(out["models"]) == ["claude/claude-4", "gemini/gemini-2.5-pro", "openai/gpt-4o"]
        assert sorted(out["data_sources"]) == ["tickflow_api_key", "tushare_token"]

    def test_provider_without_key_or_model_is_skipped(self, signed_in, cloud):
        """云端只配了一半（有 key 没 model）不该产出半个条目。"""
        assert not any(m.startswith("deepseek/") for m in cc.pull_cloud_config("u1", "tok")["models"])

    def test_default_model_follows_cloud_chat_provider(self, signed_in, cloud):
        """否则界面仍显示「未配置模型」，用户得自己再点一次默认。"""
        cc.pull_cloud_config("u1", "tok")
        config = json.loads(la.CONFIG_FILE.read_text(encoding="utf-8"))
        assert config["default"] == "cloud:gemini:gemini-2.5-pro"

    def test_second_login_is_idempotent(self, signed_in, cloud):
        """重复登录不该把同一批模型再加一遍。"""
        cc.pull_cloud_config("u1", "tok")
        again = cc.pull_cloud_config("u1", "tok")
        assert again["models"] == []
        assert len(la.load_model_configs()) == 3

    def test_local_value_wins(self, signed_in, cloud):
        """云端不覆盖本地 —— 否则「我明明改了，重启又变回去」，而且无声。"""
        la.save_config_key("tickflow_api_key", "LOCAL-TF")
        out = cc.pull_cloud_config("u1", "tok")
        config = json.loads(la.CONFIG_FILE.read_text(encoding="utf-8"))
        assert config["tickflow_api_key"] == "LOCAL-TF"
        assert "tickflow_api_key" not in out["data_sources"]

    def test_local_model_is_not_replaced(self, signed_in, cloud):
        """本地已有同 provider+model 时跳过；不同 model 仍要补上。"""
        la.save_model_entry({"id": "local-1", "provider_name": "gemini", "model": "gemini-2.5-pro", "api_key": "LOCAL"})
        cc.pull_cloud_config("u1", "tok")
        entries = [c for c in la.load_model_configs() if c.get("model") == "gemini-2.5-pro"]
        assert len(entries) == 1, "同 provider+model 被加了两遍"
        assert entries[0]["api_key"] == "LOCAL", "本地的 key 被云端覆盖了"

    def test_merge_is_a_union_not_a_replace(self, signed_in, cloud):
        """本地模型一个都不能丢。

        用真实数据验过：本地 13 个 + 云端 4 个 = 17 个。这里用一个缩小版守住
        同一条性质 —— 「拉云端配置」听起来很容易被实现成「用云端覆盖本地」，
        那会让用户在这台机器上攒的模型列表凭空消失。
        """
        for i in range(3):
            la.save_model_entry(
                {"id": f"local-{i}", "provider_name": "openai", "model": f"local-model-{i}", "api_key": "K"}
            )
        before = {(c["provider_name"], c["model"]) for c in la.load_model_configs()}

        cc.pull_cloud_config("u1", "tok")

        after = {(c["provider_name"], c["model"]) for c in la.load_model_configs()}
        assert before <= after, f"本地模型丢了: {before - after}"
        assert len(after) == len(before) + 3, "云端的三个（deepseek 半配置除外）该都进来"

    def test_same_model_under_different_provider_is_kept(self, signed_in, cloud):
        """同一个模型名跑在不同 provider 下是两条：端点和 key 都不一样。

        真实例子（来自实际配置）：`openai/deepseek-v4-flash` 走 OpenAI 兼容端点，
        `deepseek/deepseek-v4-flash` 走原生端点。判重键必须含 provider，
        只按 model 名去重会把其中一条吞掉。

        这里用 fixture 里配全的 gemini 模型名来构造这个情形。
        """
        shared = CLOUD_ROW["gemini_model"]
        la.save_model_entry({"id": "local-compat", "provider_name": "openai", "model": shared, "api_key": "COMPAT"})
        cc.pull_cloud_config("u1", "tok")
        rows = [c for c in la.load_model_configs() if c["model"] == shared]
        assert len(rows) == 2, "同名不同 provider 被误当成重复"
        assert {c["provider_name"] for c in rows} == {"openai", "gemini"}

    def test_local_default_is_not_hijacked(self, signed_in, cloud):
        """本地已有模型时不改默认 —— 那是用户自己选的。

        只有本地一个模型都没有时才用云端的 chat_provider 设默认，
        否则界面会一直显示「未配置模型」。
        """
        la.save_model_entry({"id": "mine", "provider_name": "openai", "model": "my-pick", "api_key": "K"})
        la.set_default_model("mine")
        cc.pull_cloud_config("u1", "tok")
        config = json.loads(la.CONFIG_FILE.read_text(encoding="utf-8"))
        assert config["default"] == "mine", "云端抢走了用户选的默认模型"

    def test_missing_row_is_not_an_error(self, signed_in, monkeypatch):
        """云端没配过不是错误，只是没东西可拉。"""
        monkeypatch.setattr(cc, "_fetch_settings", lambda *_a: None)
        out = cc.pull_cloud_config("u1", "tok")
        assert out == {"models": [], "data_sources": [], "available": False}

    def test_fetch_failure_does_not_raise(self, signed_in, monkeypatch):
        """拉配置失败不能让登录失败 —— 用户已经通过认证了。"""

        def boom(*_a, **_k):
            raise RuntimeError("network down")

        monkeypatch.setattr("integrations.supabase_base.create_user_client", boom)
        assert cc.pull_cloud_config("u1", "tok")["available"] is False

    def test_refuse_merge_after_logout_during_fetch(self, signed_in, monkeypatch):
        """拉取途中登出：不得把上一账号的云端密钥写进本机。"""

        def fetch_then_logout(user_id: str, _token: str):
            assert user_id == "u1"
            la.logout()
            return dict(CLOUD_ROW)

        monkeypatch.setattr(cc, "_fetch_settings", fetch_then_logout)
        out = cc.pull_cloud_config("u1", "tok")
        assert out == {"models": [], "data_sources": [], "available": False}
        config = json.loads(la.CONFIG_FILE.read_text(encoding="utf-8")) if la.CONFIG_FILE.exists() else {}
        assert not config.get("tickflow_api_key")
        assert not la.load_model_configs()

    def test_refuse_merge_after_account_switch_during_fetch(self, signed_in, monkeypatch):
        """拉取途中换号：Alice 的密钥不得落到已是 Bob 的机器上（本地优先后更难清）。"""

        def fetch_then_switch(user_id: str, _token: str):
            assert user_id == "u1"
            la.save_session(
                {
                    "user_id": "u2",
                    "email": "bob@b.c",
                    "access_token": "bob-tok",
                    "refresh_token": "bob-r",
                }
            )
            return dict(CLOUD_ROW)

        monkeypatch.setattr(cc, "_fetch_settings", fetch_then_switch)
        out = cc.pull_cloud_config("u1", "tok")
        assert out == {"models": [], "data_sources": [], "available": False}
        config = json.loads(la.CONFIG_FILE.read_text(encoding="utf-8")) if la.CONFIG_FILE.exists() else {}
        assert not config.get("tickflow_api_key")
        assert not any(str(c.get("api_key") or "").startswith(("G-", "O-", "A-")) for c in la.load_model_configs())


class TestAccountRestoresSession:
    """account 必须走 restore，不能只读文件。"""

    def test_account_uses_restore_not_plain_load(self):
        """`load_session()` 只读文件：不续期、也不用已保存的凭据重登。

        CLI 启动走 restore，桌面端原来走 load —— 于是同一台机器上 CLI 已登录而
        桌面端显示未登录；token 过期后桌面端也会把用户踢回登录页，
        而它本来能自己续上。
        """
        source = Path("cli/ipc/methods.py").read_text(encoding="utf-8")
        body = source[source.index("def account(") :]
        body = body[: body.index("\ndef ")]
        assert "restore_session" in body, "account 应该用 restore_session"
        # 只看代码，不看注释与 docstring —— 那里正解释着「为什么不用 load_session」，
        # 一条把说明文字也当违规的断言会逼人删掉有用的注释。
        code = "\n".join(
            line for line in body.splitlines() if not line.strip().startswith("#") and "`load_session()`" not in line
        )
        assert "load_session(" not in code, "load_session 不会续期，别用它判断登录态"

    def test_restore_failure_reports_signed_out_not_an_error(self, store, monkeypatch):
        """网络不通时界面需要一个明确的「未登录」，而不是错误弹窗。"""
        import cli.ipc.methods as m

        monkeypatch.setattr(
            "integrations.local_auth.restore_session",
            lambda: (_ for _ in ()).throw(RuntimeError("network down")),
        )
        events = list(m.account({}))
        assert events[0]["signed_in"] is False


class TestAuthMethodsAreExposed:
    def test_ipc_exposes_login_and_logout(self):
        """桌面端原来没有任何登录入口 —— 这两个方法就是那条路径。"""
        from cli.ipc.methods import METHODS

        assert "auth_login" in METHODS
        assert "auth_logout" in METHODS

    def test_login_requires_both_fields(self):
        from cli.ipc.methods import MethodError, auth_login

        for params in ({"email": "a@b.c"}, {"password": "x"}, {}):
            with pytest.raises(MethodError):
                list(auth_login(params))

    def test_password_never_comes_back_in_the_response(self, store, monkeypatch):
        """密码只该单向流入。回传（或进日志）会把它扩散到渲染层和日志文件。"""
        monkeypatch.setattr(
            la,
            "login",
            lambda e, p: {"user_id": "u1", "email": e, "access_token": "t", "refresh_token": "r"},
        )
        monkeypatch.setattr(cc, "_fetch_settings", lambda *_a: None)
        from cli.ipc.methods import auth_login

        events = list(auth_login({"email": "a@b.c", "password": "secret-pw"}))
        assert "secret-pw" not in json.dumps(events)


BOB_ROW = {
    "chat_provider": "openai",
    "openai_api_key": "BOB-O-KEY",
    "openai_model": "gpt-4o",
    "tickflow_api_key": "BOB-TF-KEY",
    "tushare_token": "BOB-TS-TOKEN",
}


def _switch_session(user_id: str) -> None:
    la.save_session({"user_id": user_id, "email": f"{user_id}@b.c", "access_token": "tok", "refresh_token": "r"})


class TestCloudConfigDoesNotOutliveAccount:
    """云端拉来的密钥属于拉取时的账号：登出或换号要带走，本机手填的值要留下。"""

    def test_next_account_does_not_inherit_keys(self, signed_in, cloud):
        from agents.tool_context import _local_default_llm_config

        cc.pull_cloud_config("u1", "tok")
        assert _local_default_llm_config()[1] == "G-KEY"

        la.logout()
        _switch_session("u2")

        config = la.load_config()
        assert not config.get("tickflow_api_key")
        assert not config.get("tushare_token")
        assert la.load_model_configs() == []
        assert _local_default_llm_config() is None

    def test_next_account_pull_fills_its_own_keys(self, signed_in, cloud, monkeypatch):
        cc.pull_cloud_config("u1", "tok")
        la.logout()
        _switch_session("u2")

        monkeypatch.setattr(cc, "_fetch_settings", lambda *_a: dict(BOB_ROW))
        pulled = cc.pull_cloud_config("u2", "tok")

        assert sorted(pulled["data_sources"]) == ["tickflow_api_key", "tushare_token"]
        assert la.load_config()["tickflow_api_key"] == "BOB-TF-KEY"
        assert [c["api_key"] for c in la.load_model_configs()] == ["BOB-O-KEY"]

    def test_manual_values_survive_logout(self, signed_in, cloud):
        la.save_config_key("tickflow_api_key", "MY-TF-KEY")
        la.save_model_entry({"id": "mine", "provider_name": "openai", "model": "local", "api_key": "MY-KEY"})

        cc.pull_cloud_config("u1", "tok")
        la.logout()

        config = la.load_config()
        assert config["tickflow_api_key"] == "MY-TF-KEY"
        assert not config.get("tushare_token")
        assert [c["id"] for c in la.load_model_configs()] == ["mine"]
        assert la.load_default_model_id() == "mine"

    def test_values_edited_after_pull_survive_logout(self, signed_in, cloud):
        cc.pull_cloud_config("u1", "tok")
        la.save_config_key("tickflow_api_key", "EDITED-TF")
        edited = next(c for c in la.load_model_configs() if c["provider_name"] == "openai")
        la.save_model_entry({**edited, "api_key": "EDITED-KEY"})

        la.logout()

        assert la.load_config()["tickflow_api_key"] == "EDITED-TF"
        assert [c["api_key"] for c in la.load_model_configs()] == ["EDITED-KEY"]

    def test_default_and_fallback_do_not_dangle(self, signed_in, cloud):
        la.save_model_entry({"id": "mine", "provider_name": "openai", "model": "local", "api_key": "MY-KEY"})
        cc.pull_cloud_config("u1", "tok")
        cloud_id = next(c["id"] for c in la.load_model_configs() if c["id"].startswith("cloud:"))
        la.set_default_model(cloud_id)
        la.set_fallback_model(cloud_id)

        la.logout()

        assert la.load_default_model_id() == "mine"
        assert la.load_config().get("default") == "mine"
        assert la.load_fallback_model_id() == ""

    def test_logout_drops_seeded_env_but_not_foreign_env(self, signed_in, cloud, monkeypatch):
        monkeypatch.delenv("TICKFLOW_API_KEY", raising=False)
        monkeypatch.setenv("TUSHARE_TOKEN", "FROM-SHELL")
        cc.pull_cloud_config("u1", "tok")
        la.seed_data_source_env()
        assert os.environ["TICKFLOW_API_KEY"] == "TF-KEY"

        la.logout()

        assert "TICKFLOW_API_KEY" not in os.environ
        assert os.environ["TUSHARE_TOKEN"] == "FROM-SHELL"

    def test_login_as_another_account_purges_without_logout(self, signed_in, cloud, monkeypatch):
        cc.pull_cloud_config("u1", "tok")
        monkeypatch.setattr(
            la,
            "_sign_in_with_password",
            lambda *_a: {"user_id": "u2", "email": "b@b.c", "access_token": "t", "refresh_token": "r"},
        )

        la.login("b@b.c", "pw")

        assert not la.load_config().get("tickflow_api_key")
        assert la.load_model_configs() == []

    def test_login_as_same_account_keeps_keys(self, signed_in, cloud, monkeypatch):
        cc.pull_cloud_config("u1", "tok")
        monkeypatch.setattr(
            la,
            "_sign_in_with_password",
            lambda *_a: {"user_id": "u1", "email": "a@b.c", "access_token": "t", "refresh_token": "r"},
        )

        la.login("a@b.c", "pw")

        assert la.load_config()["tickflow_api_key"] == "TF-KEY"
        assert len(la.load_model_configs()) == 3

    def test_cloud_write_refuses_after_logout(self, signed_in):
        """Disk-identity CAS inside save_cloud_*: logout clears session before purge."""
        la.logout()
        la.save_cloud_data_source("u1", "tickflow_api_key", "LATE-TF")
        assert not la.load_config().get("tickflow_api_key")

    def test_cloud_write_refuses_after_account_switch_mid_merge(self, signed_in, cloud, monkeypatch):
        """IPC race: Alice merge stalls in save_cloud_*; Bob login must win."""
        import threading

        enter = threading.Event()
        bob_done = threading.Event()
        real_save = la.save_cloud_data_source

        def racing_save(user_id, key, value):
            enter.set()
            assert bob_done.wait(timeout=2)
            return real_save(user_id, key, value)

        monkeypatch.setattr(la, "save_cloud_data_source", racing_save)

        def bob_switch():
            assert enter.wait(timeout=2)
            # Match login()'s order: flip session first, then purge prior cloud keys.
            la.save_session({"user_id": "u2", "email": "b@b.c", "access_token": "bt", "refresh_token": "br"})
            la.clear_cloud_synced_config(keep_user_id="u2")
            bob_done.set()

        thread = threading.Thread(target=bob_switch)
        thread.start()
        cc.pull_cloud_config("u1", "tok")
        thread.join(timeout=5)

        cfg = la.load_config()
        assert la.load_session()["user_id"] == "u2"
        assert not cfg.get("tickflow_api_key"), f"Alice key leaked: {cfg.get('tickflow_api_key')}"
        assert not any(
            str(c.get("api_key") or "").startswith(("G-", "O-", "A-", "TF-")) for c in la.load_model_configs()
        )

    def test_keys_pulled_before_upgrade_are_adopted_by_owner(self, signed_in, cloud):
        """升级前拉的值没有来源记录：下次拉取值相同才认领，不同的不动。"""
        la.save_config_key("tickflow_api_key", "TF-KEY")
        la.save_config_key("tushare_token", "TYPED-BY-HAND")
        la.save_model_entry(
            {
                "id": "cloud:gemini:gemini-2.5-pro",
                "provider_name": "gemini",
                "model": "gemini-2.5-pro",
                "api_key": "G-KEY",
            }
        )

        cc.pull_cloud_config("u1", "tok")
        la.logout()

        config = la.load_config()
        assert not config.get("tickflow_api_key")
        assert config["tushare_token"] == "TYPED-BY-HAND"
        assert la.load_model_configs() == []
