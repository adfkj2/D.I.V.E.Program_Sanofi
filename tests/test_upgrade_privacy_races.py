"""Concurrent deletion regressions against real local stores; never paid calls."""

import base64
import io
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from PIL import Image, PngImagePlugin
from wot.domain import DomainError
from wot.main import create_app
from wot.model import Provider
from wot.service import WotService
from wot.upgrade import Upgrade


@pytest.fixture
def local(tmp_path):
    service = WotService(tmp_path)
    upgrade = Upgrade(service, tmp_path)
    try:
        yield service, upgrade
    finally:
        upgrade.close()
        service.close()


def test_concurrent_initial_profile_reads_share_one_stable_version(local):
    service, upgrade = local
    barrier = threading.Barrier(8)

    def read():
        barrier.wait(timeout=3)
        return upgrade.people.me()["draft"]["id"]

    with ThreadPoolExecutor(max_workers=8) as pool:
        versions = list(pool.map(lambda _: read(), range(8)))
    assert len(set(versions)) == 1
    assert len(service.store.all("user_skill")) == 1
    assert upgrade.assessments.unlock_state()["unlocked"] is False
    assert upgrade.people.me()["certified"] is None


def test_deletion_cannot_be_followed_by_late_profile_snapshot_resurrection(local, monkeypatch):
    service, upgrade = local
    canary = "我喜欢PRIVATE_SNAPSHOT_DELETE_CANARY_57282先比较风险"
    evidence = upgrade.people.statement(canary)
    snapshot_ready, release_snapshot = threading.Event(), threading.Event()
    deletion_started, deletion_done = threading.Event(), threading.Event()
    original_mirror = service.mirror
    errors = []

    def paused_mirror():
        snapshot = original_mirror()
        if threading.current_thread().name == "compile-snapshot":
            snapshot_ready.set()
            assert release_snapshot.wait(3)
        return snapshot

    def compile_snapshot():
        try:
            upgrade.people.user_skill()
        except Exception as exc:
            errors.append(exc)

    def delete():
        deletion_started.set()
        try:
            service.delete(evidence["id"])
        except Exception as exc:
            errors.append(exc)
        finally:
            deletion_done.set()

    monkeypatch.setattr(service, "mirror", paused_mirror)
    compiler = threading.Thread(target=compile_snapshot, name="compile-snapshot", daemon=True)
    compiler.start()
    assert snapshot_ready.wait(2)
    deletion = threading.Thread(target=delete, daemon=True)
    deletion.start()
    assert deletion_started.wait(2)
    # With an atomic snapshot the deletion waits for compilation, then scrubs it.
    # Without it the callback finishes too soon, before the stale version exists.
    deletion_done.wait(0.2)
    release_snapshot.set()
    compiler.join(timeout=3)
    deletion.join(timeout=3)
    assert not compiler.is_alive() and not deletion.is_alive()
    assert errors == []
    assert canary not in json.dumps(upgrade.state(), ensure_ascii=False)
    assert canary not in json.dumps(service.export(), ensure_ascii=False)
    assert all(
        v["status"] == "INVALIDATED"
        for v in service.store.all("user_skill")
        if evidence["id"] in v["evidence_ids"]
    )


def test_cached_receipt_decode_cannot_return_deleted_text(local, monkeypatch):
    service, upgrade = local
    canary = "我喜欢CACHED_RECEIPT_DELETE_CANARY_20339认真分析"
    evidence = upgrade.people.statement(canary)
    service.once("cached-receipt", {"operation": "read"}, lambda: {"receipt_marker": canary})
    decoded, release = threading.Event(), threading.Event()
    original_loads = json.loads
    values, errors = [], []

    def pause_receipt(text, *args, **kwargs):
        value = original_loads(text, *args, **kwargs)
        if isinstance(value, dict) and value.get("receipt_marker") == canary:
            decoded.set()
            assert release.wait(3)
        return value

    def read():
        try:
            values.append(service.once("cached-receipt", {"operation": "read"}, lambda: {}))
        except DomainError as exc:
            errors.append(exc)

    monkeypatch.setattr(json, "loads", pause_receipt)
    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    assert decoded.wait(2)
    try:
        service.delete(evidence["id"])
    finally:
        release.set()
    reader.join(timeout=3)
    assert not reader.is_alive()
    assert values == []
    assert len(errors) == 1 and errors[0].status == 409
    assert (
        service.store.fetchone("SELECT response FROM requests WHERE key='cached-receipt'")[0] == ""
    )


class DelayedEchoProvider(Provider):
    def __init__(self):
        super().__init__()
        self.entered, self.release = threading.Event(), threading.Event()

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        self.entered.set()
        assert self.release.wait(3)
        return {
            "text": "Late model response echoes " + kwargs["topic"],
            "tokens": 20,
            "provider": "mock",
        }


def test_late_real_chat_reply_after_deletion_never_reenters_history_or_receipt(tmp_path):
    provider = DelayedEchoProvider()
    app = create_app(tmp_path, provider)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        client.headers["X-WOT-Token"] = client.get("/api/v1/session").json()["token"]
        conversation = client.post(
            "/api/v1/real-conversations", json={"skill_id": "skill_munger"}
        ).json()
        canary = "我喜欢LATE_CHAT_DELETE_CANARY_39480先考虑风险"
        path = f"/api/v1/real-conversations/{conversation['id']}/messages"
        responses = []

        def send():
            responses.append(
                client.post(
                    path,
                    json={"text": canary, "remember": True},
                    headers={"Idempotency-Key": "late-chat-request"},
                )
            )

        sender = threading.Thread(target=send, daemon=True)
        sender.start()
        assert provider.entered.wait(2)
        evidence = app.state.service.active_evidence()[0]
        try:
            assert client.delete(f"/api/v1/data/evidence/{evidence['id']}").status_code == 200
        finally:
            provider.release.set()
        sender.join(timeout=3)
        assert not sender.is_alive()
        assert len(responses) == 1 and responses[0].status_code == 409
        assert canary not in client.get(path).text
        assert canary not in client.get("/api/v1/state").text
        assert canary not in client.post("/api/v1/data/export", json={}).text
        assert provider.calls == []
        assert (
            app.state.service.store.fetchone(
                "SELECT response FROM requests WHERE key='late-chat-request'"
            )[0]
            == ""
        )


def test_self_report_correction_redacts_all_versions_and_ordinary_views(local):
    service, upgrade = local
    service.people = upgrade.people
    old = "我喜欢SELF_REPORT_OLD_VIEW_CANARY_99583迅速决定"
    evidence = upgrade.people.statement(old)
    first = upgrade.people.user_skill()
    newer = service.once(
        "correct-self-report",
        {"id": evidence["id"]},
        lambda: service.correct(evidence["id"], "我喜欢先写下依据再决定。", "general"),
    )
    assert newer["origin"] == "USER_CORRECTION"
    assert service.evidence(evidence["id"])["status"] == "DELETED"
    assert upgrade.people.user_skill()["id"] != first["id"]
    for view in (service.state(), service.export(), upgrade.state(), upgrade.people.me()):
        assert old not in json.dumps(view, ensure_ascii=False)
    assert service.store.get(first["id"], "user_skill")["status"] == "INVALIDATED"


def test_connection_configs_and_private_answers_are_absent_from_export(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        client.headers["X-WOT-Token"] = client.get("/api/v1/session").json()["token"]
        key = "ONLY_SESSION_SECRET_CANARY_69445"
        connection = client.post(
            "/api/v1/model-connections",
            json={
                "provider": "custom",
                "model": "private-model-marker-36585",
                "api_key": key,
                "base_url": "http://127.0.0.1:9977/v1",
                "label": "private-label-marker-95347",
            },
        ).json()
        assert connection["key_status"] == "CONFIGURED"
        # Private answer storage has no generic state/export adapter.
        app.state.upgrade.assessments._save_private(
            "private-test-only",
            {"answers": {"q01": {"choice": "A", "reason": "HELDOUT_PRIVATE_CANARY_19373"}}},
        )
        state = client.get("/api/v1/state").text
        export = client.post("/api/v1/data/export", json={}).text
        for canary in (key, "HELDOUT_PRIVATE_CANARY_19373"):
            assert canary not in state and canary not in export
        assert "private-label-marker-95347" not in export
        assert "credential_ref" not in export
        assert "http://127.0.0.1:9977/v1" not in export
        assert app.state.upgrade.connections.external_calls == 0


def test_legacy_preview_routes_stay_readonly_after_unlock_even_with_cached_receipt(
    tmp_path, monkeypatch
):
    app = create_app(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        client.headers["X-WOT-Token"] = client.get("/api/v1/session").json()["token"]
        # Test only the legacy-route policy after gate acceptance; no fake grant
        # is issued or stored, and the strict engine is tested independently.
        monkeypatch.setattr(app.state.upgrade.assessments, "assert_unlocked", lambda *_: {})
        app.state.service.once(
            "old-next-receipt",
            {"path": "/api/v1/simulations/old/next", "disclosure_approved": False},
            lambda: {"old_preview_text": "must never bypass readonly history"},
        )
        responses = [
            client.post(
                "/api/v1/simulations",
                json={"topic": "legacy", "persona_version_id": "old", "skill_id": "skill_munger"},
            ),
            client.post(
                "/api/v1/simulations/old/next",
                json={},
                headers={"Idempotency-Key": "old-next-receipt"},
            ),
            client.post("/api/v1/simulations/old/resume", json={}),
        ]
        assert all(response.status_code == 409 for response in responses)
        assert all("旧" in response.json()["detail"] for response in responses)
        assert app.state.upgrade.connections.external_calls == 0


def test_uploaded_avatar_discards_icc_exif_and_text_metadata(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        client.headers["X-WOT-Token"] = client.get("/api/v1/session").json()["token"]
        source = Image.new("RGB", (48, 48), "blue")
        pnginfo = PngImagePlugin.PngInfo()
        pnginfo.add_text("private_note", "PRIVATE_AVATAR_TEXT_METADATA_CANARY_5727")
        exif = source.getexif()
        exif[270] = "PRIVATE_AVATAR_EXIF_METADATA_CANARY_9511"
        buffer = io.BytesIO()
        source.save(
            buffer,
            format="PNG",
            pnginfo=pnginfo,
            exif=exif,
            icc_profile=b"PRIVATE_AVATAR_ICC_METADATA_CANARY_5838",
        )
        data_url = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()
        avatar = client.post("/api/v1/me/avatar", json={"data_url": data_url}).json()["avatar_url"]
        result = client.get(avatar)
        assert result.status_code == 200
        with Image.open(io.BytesIO(result.content)) as decoded:
            assert not decoded.getexif()
            assert "icc_profile" not in decoded.info
            assert "private_note" not in decoded.info


def test_intervention_trace_events_export_and_discarded_draft_redact_together(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        client.headers["X-WOT-Token"] = client.get("/api/v1/session").json()["token"]
        upgrade, service = app.state.upgrade, app.state.service
        canary = "我喜欢INTERVENTION_TRACE_DELETE_CANARY_79251先比较依据"
        evidence = upgrade.people.statement(canary)
        # A historical fixture contains no unlock permit and starts paused. It
        # exercises read APIs without enabling a synthetic assessment outcome.
        with service.store.transaction():
            participants = [
                {
                    "id": rid,
                    "kind": "CHARACTER",
                    "version_id": "historical-" + rid,
                    "display_name": rid,
                    "avatar_url": "",
                }
                for rid in ("skill_munger", "skill_buffett")
            ]
            session = service.store.add(
                "virtual_session",
                {
                    "participants": participants,
                    "state": "PAUSED",
                    "epoch": 1,
                    "revision": 1,
                    "connection_id": None,
                    "draft": {"id": "discard-me", "text": canary},
                    "typing": False,
                    "context_corrections": [{"evidence_id": evidence["id"], "text": canary}],
                    "last_dispatched": "",
                    "intervention_pending": "internal-token-not-for-view",
                },
            )
            intervention = service.store.add(
                "intervention",
                {
                    "session_id": session["id"],
                    "draft_id": "historical-turn",
                    "scope": "REAL_VIEW",
                    "text": canary,
                    "source_id": evidence["id"],
                    "participant_version": participants[0]["version_id"],
                    "human_confirmed": True,
                },
            )
            service.store.add(
                "virtual_turn",
                {
                    "session_id": session["id"],
                    "speaker_id": participants[0]["id"],
                    "text": canary,
                    "turn_index": 0,
                    "version_id": participants[0]["version_id"],
                    "intervention_id": intervention["id"],
                    "disclosure": "AI 模拟",
                },
                rid="historical-turn",
            )
        path = f"/api/v1/virtual-sessions/{session['id']}/events"
        before = client.get(path).json()["session"]
        assert before["interventions"][0]["source_id"] == evidence["id"]
        assert before["interventions"][0]["text"] == canary
        assert before["turns"][0]["intervention_id"] == before["interventions"][0]["id"]
        assert "intervention_pending" not in before
        assert client.delete(f"/api/v1/data/evidence/{evidence['id']}").status_code == 200
        after = client.get(path).json()["session"]
        assert after["state"] == "INVALIDATED" and after["draft"] is None
        assert after["interventions"][0]["source_id"] == evidence["id"]
        for response in (
            client.get(path),
            client.get("/api/v1/state"),
            client.post("/api/v1/data/export", json={}),
        ):
            assert canary not in response.text
        assert service.store.get(session["id"], "virtual_session")["context_corrections"] == []
        assert upgrade.connections.external_calls == 0
