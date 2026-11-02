"""The launcher must never leave a user staring at a window that says nothing."""
import logging

import pytest
from wot import desktop


@pytest.fixture
def data_dir(monkeypatch, tmp_path):
    home = tmp_path / "data"
    monkeypatch.setattr(desktop, "default_data_dir", lambda: home)
    monkeypatch.delenv("WOT_NO_BROWSER", raising=False)
    return home


def test_setup_logging_writes_beside_the_database(data_dir):
    target = desktop.setup_logging(desktop.log_dir(data_dir))
    logging.getLogger("wot.test").warning("hello")
    assert target == data_dir / "logs" / "app.log"
    assert "hello" in target.read_text(encoding="utf-8")


def test_setup_logging_replaces_its_own_handlers(data_dir):
    desktop.setup_logging(data_dir / "logs")
    desktop.setup_logging(data_dir / "logs")
    owned = [h for h in logging.getLogger().handlers if getattr(h, "_wot_owned", False)]
    assert len(owned) == 2


def test_browser_can_be_disabled_for_automation(monkeypatch):
    monkeypatch.setenv("WOT_NO_BROWSER", "1")
    assert desktop.browser_allowed() is False
    monkeypatch.setenv("WOT_NO_BROWSER", "0")
    assert desktop.browser_allowed() is True
    monkeypatch.delenv("WOT_NO_BROWSER")
    assert desktop.browser_allowed() is True


def test_health_probe_ignores_a_foreign_server(monkeypatch):
    class Response:
        status_code = 200

        def json(self):
            return {"status": "not-us"}

    monkeypatch.setattr(desktop.httpx, "get", lambda *a, **k: Response())
    assert desktop.app_answers() is False


def test_health_probe_accepts_our_server(monkeypatch):
    class Response:
        status_code = 200

        def json(self):
            return {"status": "ok"}

    monkeypatch.setattr(desktop.httpx, "get", lambda *a, **k: Response())
    assert desktop.app_answers() is True


def test_health_probe_survives_a_closed_port(monkeypatch):
    def refuse(*args, **kwargs):
        raise desktop.httpx.ConnectError("refused")

    monkeypatch.setattr(desktop.httpx, "get", refuse)
    assert desktop.app_answers() is False


def test_run_reuses_a_live_instance(data_dir, monkeypatch):
    opened = []
    monkeypatch.setattr(desktop, "app_answers", lambda timeout=0.6: True)
    monkeypatch.setattr(desktop.webbrowser, "open", opened.append)
    assert desktop.run() == 0
    assert opened == [desktop.URL]


def test_run_refuses_a_port_held_by_another_program(data_dir, monkeypatch, capsys):
    monkeypatch.setattr(desktop, "app_answers", lambda timeout=0.6: False)
    monkeypatch.setattr(desktop, "port_is_free", lambda: False)
    assert desktop.run() == 2
    assert "8765" in capsys.readouterr().out
