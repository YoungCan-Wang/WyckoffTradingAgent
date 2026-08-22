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

    def test_logout_keeps_unrelated_config(self, store):
        """清凭据不该顺手清掉主题、模型这些无关配置。"""
        la.save_config_key("theme", "dark")
        la.save_config_key("password", "secret")
        la.logout()
        assert json.loads(la.CONFIG_FILE.read_text(encoding="utf-8")).get("theme") == "dark"


class TestCloudConfigPull:
    def test_blank_machine_gets_everything(self, store, cloud):
        out = cc.pull_cloud_config("u1", "tok")
        assert out["available"] is True
        assert sorted(out["models"]) == ["claude/claude-4", "gemini/gemini-2.5-pro", "openai/gpt-4o"]
        assert sorted(out["data_sources"]) == ["tickflow_api_key", "tushare_token"]

    def test_provider_without_key_or_model_is_skipped(self, store, cloud):
        """云端只配了一半（有 key 没 model）不该产出半个条目。"""
        assert not any(m.startswith("deepseek/") for m in cc.pull_cloud_config("u1", "tok")["models"])

    def test_default_model_follows_cloud_chat_provider(self, store, cloud):
        """否则界面仍显示「未配置模型」，用户得自己再点一次默认。"""
        cc.pull_cloud_config("u1", "tok")
        config = json.loads(la.CONFIG_FILE.read_text(encoding="utf-8"))
        assert config["default"] == "cloud:gemini:gemini-2.5-pro"

    def test_second_login_is_idempotent(self, store, cloud):
        """重复登录不该把同一批模型再加一遍。"""
        cc.pull_cloud_config("u1", "tok")
        again = cc.pull_cloud_config("u1", "tok")
        assert again["models"] == []
        assert len(la.load_model_configs()) == 3

    def test_local_value_wins(self, store, cloud):
        """云端不覆盖本地 —— 否则「我明明改了，重启又变回去」，而且无声。"""
        la.save_config_key("tickflow_api_key", "LOCAL-TF")
        out = cc.pull_cloud_config("u1", "tok")
        config = json.loads(la.CONFIG_FILE.read_text(encoding="utf-8"))
        assert config["tickflow_api_key"] == "LOCAL-TF"
        assert "tickflow_api_key" not in out["data_sources"]

    def test_local_model_is_not_replaced(self, store, cloud):
        """本地已有同 provider+model 时跳过；不同 model 仍要补上。"""
        la.save_model_entry({"id": "local-1", "provider_name": "gemini", "model": "gemini-2.5-pro", "api_key": "LOCAL"})
        cc.pull_cloud_config("u1", "tok")
        entries = [c for c in la.load_model_configs() if c.get("model") == "gemini-2.5-pro"]
        assert len(entries) == 1, "同 provider+model 被加了两遍"
        assert entries[0]["api_key"] == "LOCAL", "本地的 key 被云端覆盖了"

    def test_merge_is_a_union_not_a_replace(self, store, cloud):
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

    def test_same_model_under_different_provider_is_kept(self, store, cloud):
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

    def test_local_default_is_not_hijacked(self, store, cloud):
        """本地已有模型时不改默认 —— 那是用户自己选的。

        只有本地一个模型都没有时才用云端的 chat_provider 设默认，
        否则界面会一直显示「未配置模型」。
        """
        la.save_model_entry({"id": "mine", "provider_name": "openai", "model": "my-pick", "api_key": "K"})
        la.set_default_model("mine")
        cc.pull_cloud_config("u1", "tok")
        config = json.loads(la.CONFIG_FILE.read_text(encoding="utf-8"))
        assert config["default"] == "mine", "云端抢走了用户选的默认模型"

    def test_missing_row_is_not_an_error(self, store, monkeypatch):
        """云端没配过不是错误，只是没东西可拉。"""
        monkeypatch.setattr(cc, "_fetch_settings", lambda *_a: None)
        out = cc.pull_cloud_config("u1", "tok")
        assert out == {"models": [], "data_sources": [], "available": False}

    def test_fetch_failure_does_not_raise(self, store, monkeypatch):
        """拉配置失败不能让登录失败 —— 用户已经通过认证了。"""

        def boom(*_a, **_k):
            raise RuntimeError("network down")

        monkeypatch.setattr("integrations.supabase_base.create_user_client", boom)
        assert cc.pull_cloud_config("u1", "tok")["available"] is False


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
