import json

from wot.service import WotService


def test_restart_redacts_title_and_receipts_from_previously_deleted_sources(tmp_path):
    directory = tmp_path / "data"
    service = WotService(directory)
    text = "LEGACY_DELETED_TITLE_CANARY_724971"
    conversation = service.conversation(text, "skill_buffett")
    reply = service.send(conversation["id"], text, True, "general", "legacy")
    service.delete(reply["evidence_id"])
    # Reproduce a pre-upgrade deletion: message is redacted, title/receipt stale.
    with service.store.transaction():
        stored = service.require(conversation["id"], "conversation")
        stored["title"] = text
        service.store.update(stored)
        service.store.db.execute("INSERT INTO requests VALUES(?,?,?)",
                                 ("old-receipt", "fingerprint", json.dumps({"title": text})))
    service.close()
    upgraded = WotService(directory)
    try:
        name = upgraded.require("skill_buffett", "skill")["display_name"]
        assert upgraded.require(conversation["id"], "conversation")["title"] == f"与{name}的聊天"
        assert text not in json.dumps(upgraded.export(), ensure_ascii=False)
        assert upgraded.store.db.execute(
            "SELECT response FROM requests WHERE key='old-receipt'"
        ).fetchone()[0] == ""
        assert upgraded.active_evidence() == []
    finally:
        upgraded.close()
