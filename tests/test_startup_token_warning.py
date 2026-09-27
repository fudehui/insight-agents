"""
启动警告测试：未设置 APP_ACCESS_TOKEN 时输出"无鉴权模式"警告

服务在 lifespan 启动钩子里调用 _warn_if_access_token_missing 检查令牌配置。
本文件直接调用该函数并显式控制环境变量：.env 真实存在时，import 链中的
load_dotenv 会把 APP_ACCESS_TOKEN 注入进程环境，因此用 monkeypatch 覆盖
（测试结束自动恢复），保证断言不受本机 .env 内容影响。
"""

import logging

import app.api.server as server


class TestStartupTokenWarning:
    def test_warns_when_token_missing(self, monkeypatch, caplog):
        # 未设置令牌（显式删除，屏蔽 .env 注入）：应输出无鉴权模式警告
        monkeypatch.delenv("APP_ACCESS_TOKEN", raising=False)
        with caplog.at_level(logging.WARNING, logger="app.api.server"):
            server._warn_if_access_token_missing()
        assert "无鉴权模式" in caplog.text

    def test_warns_when_token_blank(self, monkeypatch, caplog):
        # 纯空白令牌视同未设置（与 _access_token 的 strip 口径一致）：应输出警告
        monkeypatch.setenv("APP_ACCESS_TOKEN", "   ")
        with caplog.at_level(logging.WARNING, logger="app.api.server"):
            server._warn_if_access_token_missing()
        assert "无鉴权模式" in caplog.text

    def test_no_warning_when_token_set(self, monkeypatch, caplog):
        # 已配置令牌：鉴权生效，不应输出警告
        monkeypatch.setenv("APP_ACCESS_TOKEN", "ci-dummy-token")
        with caplog.at_level(logging.WARNING, logger="app.api.server"):
            server._warn_if_access_token_missing()
        assert "无鉴权模式" not in caplog.text
