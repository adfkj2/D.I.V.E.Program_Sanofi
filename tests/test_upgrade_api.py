"""Owner-route regressions for the upgrade. No real supplier calls or grants."""
import base64
import io
import json
import sqlite3

from fastapi.testclient import TestClient
from PIL import Image
from wot.main import create_app
from wot.service import WotService


def open_client(app):
    client = TestClient(app, base_url="http://127.0.0.1:8765")
    client.headers["X-WOT-Token"] = client.get("/api/v1/session").json()["token"]
    return client


def avatar_data():
    image = Image.new("RGB", (32, 64), "blue")
    out = io.BytesIO()
    exif = image.getexif()
    exif[270] = "private-camera-description"
    image.save(out, format="JPEG", exif=exif)
    return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode()


def test_first_run_browsing_and_all_simulation_routes_are_closed(tmp_path):
    app = create_app(tmp_path)
    with open_client(app) as client:
        state = client.get("/api/v1/state").json()
        assert len([s for s in state["skills"] if s.get("builtin")]) == 15
        upgrade = state["upgrade"]
        assert upgrade["connections"] == []
        assert upgrade["unlock"]["unlocked"] is False
        assert upgrade["preferences"]["setup_dismissed"] is False
        assert len(client.get("/api/v1/model-providers").json()) == 9
        assert client.post("/api/v1/preferences",json={"setup_dismissed":True}).status_code == 200
        assert client.post("/api/v1/assessments",json={"kind":"PERSONAL"}).status_code == 409
        assert client.post("/api/v1/virtual-sessions",json={"topic":"bypass"}).status_code == 403
        assert client.post("/api/v1/simulations",json={"topic":"bypass","persona_version_id":"old","skill_id":"skill_socratic"}).status_code == 403
        assert client.post("/api/v1/simulations/old/next",json={},headers={"Idempotency-Key":"old-next"}).status_code == 403
        assert client.post("/api/v1/simulations/old/resume",json={}).status_code == 403
        assert client.post("/api/v1/assessments",json={"kind":"PERSONAL","score":100,"passed":True}).status_code == 400


def test_secrets_never_enter_state_export_requests_or_database(tmp_path):
    secret = "private-test-only-not-a-real-provider-key-0123456789"
    app = create_app(tmp_path)
    with open_client(app) as client:
        result = client.post("/api/v1/model-connections",json={"provider":"deepseek","model":"manual-model",
            "api_key":secret,"remember":False})
        assert result.status_code == 200
        assert secret not in result.text
        assert secret not in client.get("/api/v1/state").text
        assert secret not in client.post("/api/v1/data/export",json={}).text
        assert app.state.upgrade.connections.external_calls == 0
        for table in ("resources","requests","audit"):
            rows = app.state.service.store.fetchall("SELECT * FROM " + table)
            assert secret not in repr([tuple(r) for r in rows])
    assert secret.encode() not in (tmp_path/"wot.db").read_bytes()
    second = create_app(tmp_path)
    with open_client(second) as client:
        connection = client.get("/api/v1/model-connections").json()[0]
        assert connection["key_status"] == "MISSING"


def test_import_is_draft_even_through_legacy_route_and_never_executes(tmp_path):
    app = create_app(tmp_path)
    with open_client(app) as client:
        data = {"text":"---\nname: 待评人物\n---\n这是人物的思考方式。", "source":"本人文本",
                "permission":"本人允许", "category":"CURATED"}
        draft = client.post("/api/v1/skills/import",json=data).json()
        assert draft["status"] == "DRAFT"
        assert client.post(f"/api/v1/skill-imports/{draft['id']}/add",json={}).status_code == 409
        assert not any(s["id"]==draft["id"] for s in client.get("/api/v1/state").json()["skills"])
        changed=client.post(f"/api/v1/skill-imports/{draft['id']}",json={"source":"修订来源"}).json()
        assert changed["version"] == draft["version"]+1
        assert changed["content_hash"] != draft["content_hash"]


def test_avatar_reencode_reset_and_tombstone_survive_restart(tmp_path):
    app = create_app(tmp_path)
    with open_client(app) as client:
        role=next(s for s in client.get("/api/v1/state").json()["skills"] if s.get("builtin"))
        rid=role["id"]
        result=client.post(f"/api/v1/skills/{rid}/avatar",json={"data_url":avatar_data()})
        assert result.status_code==200,result.text
        avatar=result.json()["avatar_url"]
        raw=client.get(avatar)
        assert raw.status_code==200
        assert b"private-camera-description" not in raw.content
        with Image.open(io.BytesIO(raw.content)) as img:
            assert img.size==(512,512) and not img.getexif()
        assert client.post(f"/api/v1/skills/{rid}/avatar",json={"data_url":"data:image/svg+xml;base64,AAAA"}).status_code==400
        assert client.post(f"/api/v1/skills/{rid}/avatar/reset",json={}).json()["avatar_url"]==role["avatar_url"]
        assert client.delete(f"/api/v1/skills/{rid}").json()["status"]=="DELETED"
        assert client.post("/api/v1/real-conversations",json={"skill_id":rid}).status_code==409
    app=create_app(tmp_path)
    with open_client(app) as client:
        assert next(s for s in client.get("/api/v1/state").json()["skills"] if s["id"]==rid)["status"]=="DELETED"
        assert client.post(f"/api/v1/skills/{rid}/restore",json={}).json()["status"]=="ACTIVE"


def test_confirmed_self_report_versioning_and_deletion_propagate(tmp_path):
    app=create_app(tmp_path)
    with open_client(app) as client:
        assert client.post("/api/v1/me",json={"self_statement":"我喜欢独立思考"}).status_code==400
        statement="我喜欢在重大决定前写下反对理由和失败条件"
        response=client.post("/api/v1/me",json={"nickname":"小林","bio":"一名学习者",
            "self_statement":statement,"statement_confirmed":True})
        assert response.status_code==200,response.text
        me=response.json()
        assert len(me["mirror"]["PERCEIVED"]) == 1
        assert me["mirror"]["OBSERVED"] == []
        eid=me["self_statements"][0]["id"]
        version_id=me["draft"]["id"]
        assert statement in me["draft"]["text"]
        client.post("/api/v1/me",json={"nickname":"新昵称"})
        assert client.get("/api/v1/me").json()["draft"]["id"]==version_id
        assert client.post("/api/v1/me/avatar",json={"data_url":avatar_data()}).status_code==200
        assert client.get("/api/v1/me").json()["draft"]["id"]==version_id
        assert client.delete(f"/api/v1/data/evidence/{eid}").status_code==200
        state=client.get("/api/v1/state").json()
        assert statement not in json.dumps(state,ensure_ascii=False)
        old=next(v for v in state["upgrade"]["me"]["versions"] if v["id"]==version_id)
        assert old["status"]=="INVALIDATED"
        assert statement not in client.post("/api/v1/data/export",json={}).text


def test_v2_whole_data_backup_and_custom_role_migration(tmp_path):
    directory=tmp_path/"old"
    old=WotService(directory)
    role=old.import_skill("---\nname: 旧人物\n---\n角色正文。","CURATED","旧授权","旧来源")
    with old.store.transaction():
        old.store.db.execute("DELETE FROM schema_version WHERE version=3")
    old.close()
    app=create_app(directory)
    with open_client(app) as client:
        state=client.get("/api/v1/state").json()
        assert next(s for s in state["skills"] if s["id"]==role["id"])["status"]=="PENDING_REVIEW"
        assert any(d["existing_skill_id"]==role["id"] for d in state["upgrade"]["skill_imports"])
        backup=app.state.service.upgrade_backup
        assert backup and (backup/"wot.db").is_file() and (backup/"memory.db").is_file()
        with sqlite3.connect(backup/"wot.db") as db:
            assert db.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]==2
