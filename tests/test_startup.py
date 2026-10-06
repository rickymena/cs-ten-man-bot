"""Startup failures must be a clear message and exit 7, never a traceback."""

import logging
import os

import pytest

import bot
from utils import tracking
from utils.exit_codes import EXIT_CONFIG


@pytest.mark.skipif(os.getuid() == 0, reason="root can write anywhere")
def test_unwritable_logs_folder_falls_back_to_stdout(tmp_path, capsys):
    logs = tmp_path / "logs"
    logs.mkdir()
    logs.chmod(0o500)
    root = logging.getLogger()
    saved = root.handlers[:]
    root.handlers.clear()
    try:
        tracking.setup_logging("x.log", str(logs))  # must not raise
        assert "logging to stdout only" in capsys.readouterr().err
    finally:
        root.handlers[:] = saved
        logs.chmod(0o700)


@pytest.mark.skipif(os.getuid() == 0, reason="root can write anywhere")
def test_unwritable_data_folder_exits_7_with_a_reason(tmp_path, monkeypatch, caplog):
    data = tmp_path / "data"
    data.mkdir()
    data.chmod(0o500)
    monkeypatch.setenv("DISCORD_TOKEN", "a" * 24 + "." + "b" * 6 + "." + "c" * 27)
    monkeypatch.setenv("GUILD_ID", "123456789012345678")
    monkeypatch.setenv("DB_PATH", str(data / "tenman.db"))
    monkeypatch.setattr(bot, "setup_logging", lambda *a: None)
    try:
        with caplog.at_level(logging.ERROR):
            assert bot.main() == EXIT_CONFIG
        assert "cannot open the database" in caplog.text and "writable by uid" in caplog.text
    finally:
        data.chmod(0o700)


@pytest.mark.parametrize("uid, uid_map, real_root", [
    (1000, "0 1000 1", False),           # normal user
    (0, "0 1000 1", False),              # rootless Podman: root inside = your user outside
    (0, "0 0 4294967295", True),         # Docker without user:, or a rootful engine
])
def test_real_root_is_detected(monkeypatch, tmp_path, uid, uid_map, real_root):
    import builtins

    monkeypatch.setattr(bot.os, "getuid", lambda: uid)
    real_open = builtins.open
    monkeypatch.setattr(builtins, "open", lambda p, *a, **k: real_open(tmp_path / "map", *a, **k)
                        if p == "/proc/self/uid_map" else real_open(p, *a, **k))
    (tmp_path / "map").write_text(f"         {uid_map}\n")
    assert bot.running_as_real_root() is real_root


def test_bot_refuses_to_start_as_real_root(monkeypatch, caplog):
    monkeypatch.setattr(bot, "running_as_real_root", lambda: True)
    monkeypatch.setattr(bot, "setup_logging", lambda *a: None)
    monkeypatch.delenv("TENMAN_ALLOW_ROOT", raising=False)
    with caplog.at_level(logging.ERROR):
        assert bot.main() == EXIT_CONFIG
    assert "refusing to run as root" in caplog.text
