import importlib.util
import sqlite3
from pathlib import Path

import pytest
from wot.service import WotService

spec = importlib.util.spec_from_file_location("wot_backup", Path(__file__).parents[1] / "scripts/data.py")
backup_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup_module)


def test_offline_backup_restore_preserves_deletion_tombstones(tmp_path):
    source, backup, restored = (tmp_path / name for name in ("source", "backup", "restored"))
    service = WotService(source)
    conversation = service.conversation("备份测试", None)
    ev = service.send(conversation["id"], "我喜欢咖啡", True, "general", "one")["evidence_id"]
    service.compile("PERCEIVED")
    service.delete(ev)
    service.close()
    backup_module.backup(source, backup)
    backup_module.restore(backup, restored)
    resumed = WotService(restored)
    assert resumed.active_evidence() == []
    assert resumed.store.all("version")[0]["state"] == "INVALIDATED"
    assert resumed.core.retrieve("user/local/real", "咖啡") == []
    resumed.close()
    with pytest.raises(ValueError, match="new, empty"):
        backup_module.restore(backup, restored)


def test_restore_verifies_both_databases_before_writing(tmp_path):
    source, backup, restored = (tmp_path / name for name in ("source", "backup", "restored"))
    WotService(source).close()
    backup_module.backup(source, backup)
    with sqlite3.connect(backup / "memory.db") as db:
        db.execute("CREATE TABLE tampered(value TEXT)")
    with pytest.raises(ValueError, match="checksum"):
        backup_module.restore(backup, restored)
    assert not restored.exists()
