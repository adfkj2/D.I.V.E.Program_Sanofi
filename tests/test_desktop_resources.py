"""A frozen build must find its bundle and keep user data outside it."""
import sys
from pathlib import Path

import pytest
from wot import resources


@pytest.fixture
def frozen(monkeypatch, tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)
    return bundle


def test_source_checkout_uses_the_repository_root(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    assert resources.is_frozen() is False
    assert (resources.resource_root() / "backend" / "wot" / "main.py").is_file()


def test_frozen_process_reads_the_unpacked_bundle(frozen):
    assert resources.is_frozen() is True
    assert resources.resource_root() == frozen


def test_frontend_dist_prefers_the_bundle_layout(monkeypatch, frozen):
    bundled = frozen / "frontend_dist"
    (bundled / "assets").mkdir(parents=True)
    (bundled / "index.html").write_text("<html></html>", encoding="utf-8")
    nested = frozen / "frontend" / "dist"
    nested.mkdir(parents=True)
    (nested / "index.html").write_text("<html></html>", encoding="utf-8")
    assert resources.frontend_dist() == bundled


def test_frontend_dist_falls_back_and_can_be_absent(monkeypatch, frozen):
    assert resources.frontend_dist() is None
    nested = frozen / "frontend" / "dist"
    nested.mkdir(parents=True)
    (nested / "index.html").write_text("<html></html>", encoding="utf-8")
    assert resources.frontend_dist() == nested
    (nested / "index.html").unlink()
    (frozen / "dist").mkdir()
    (frozen / "dist" / "index.html").write_text("<html></html>", encoding="utf-8")
    assert resources.frontend_dist() == frozen / "dist"


def test_data_dir_override_wins_everywhere(monkeypatch, frozen, tmp_path):
    chosen = tmp_path / "elsewhere"
    monkeypatch.setenv("WOT_DATA_DIR", str(chosen))
    assert resources.default_data_dir() == chosen
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert resources.default_data_dir() == chosen


def test_installed_app_keeps_data_under_localappdata(monkeypatch, frozen, tmp_path):
    monkeypatch.delenv("WOT_DATA_DIR", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "LocalAppData"))
    assert resources.default_data_dir() == tmp_path / "LocalAppData" / "WeOnceTalked" / "data"


def test_installed_app_without_localappdata_stays_in_the_user_profile(monkeypatch, frozen):
    monkeypatch.delenv("WOT_DATA_DIR", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setattr(resources.Path, "home", classmethod(lambda cls: Path("/home/tester")))
    assert resources.default_data_dir() == Path("/home/tester/.weoncetalked/data")


def test_source_checkout_keeps_the_local_directory(monkeypatch, tmp_path):
    monkeypatch.delenv("WOT_DATA_DIR", raising=False)
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.chdir(tmp_path)
    assert resources.default_data_dir() == tmp_path / ".local"


def test_logs_live_next_to_the_database(monkeypatch, tmp_path):
    monkeypatch.setenv("WOT_DATA_DIR", str(tmp_path / "data"))
    assert resources.log_dir() == tmp_path / "data" / "logs"
