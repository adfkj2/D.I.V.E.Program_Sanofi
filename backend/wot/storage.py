"""One local owner, one process. The separate core DB is coordinated by durable outbox."""
import json
import shutil
import sqlite3
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

operation_context: ContextVar[dict | None] = ContextVar("wot_operation", default=None)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def uid(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value) -> str:
    return sha256(canonical(value).encode()).hexdigest()


def backup_before_upgrade(directory: Path) -> Path | None:
    """Called before either application DB is opened; keep the complete old state."""
    app_path = directory / "wot.db"
    if not app_path.is_file():
        return None
    with sqlite3.connect(str(app_path)) as source:
        exists = source.execute("SELECT 1 FROM sqlite_master WHERE name='schema_version'").fetchone()
        version = source.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] if exists else 0
    if version and version >= 3:
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = directory / "backups" / ("before-v3-" + stamp)
    target.mkdir(parents=True)
    manifest = {"format": "wot-local-backup-v2", "files": {}}
    for name in ("wot.db", "memory.db", "assessment-private.db"):
        source_path = directory / name
        if source_path.is_file():
            with sqlite3.connect(str(source_path)) as src, sqlite3.connect(str(target / name)) as dst:
                src.backup(dst)
            manifest["files"][name] = sha256((target / name).read_bytes()).hexdigest()
    for folder in ("avatars",):
        if (directory / folder).is_dir():
            for source_path in (directory / folder).glob("*.png"):
                relative = folder + "/" + source_path.name
                (target / folder).mkdir(exist_ok=True)
                shutil.copyfile(source_path, target / relative)
                manifest["files"][relative] = sha256((target / relative).read_bytes()).hexdigest()
    (target / "manifest.json").write_text(canonical(manifest), encoding="utf-8")
    return target


SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS schema_version(version INTEGER PRIMARY KEY);
INSERT OR IGNORE INTO schema_version VALUES(1);
CREATE TABLE IF NOT EXISTS resources(
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, owner_id TEXT NOT NULL,
 data TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS resources_owner_kind ON resources(owner_id,kind,created_at);
CREATE TABLE IF NOT EXISTS messages(
 id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES resources(id),
 owner_id TEXT NOT NULL, speaker TEXT NOT NULL CHECK(speaker IN ('USER','AGENT')),
 text TEXT NOT NULL, created_at TEXT NOT NULL, evidence_id TEXT, request_key TEXT,
 UNIQUE(owner_id,conversation_id,speaker,request_key));
CREATE TABLE IF NOT EXISTS evidence(
 id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, subject_id TEXT NOT NULL,
 message_id TEXT UNIQUE REFERENCES messages(id), origin TEXT NOT NULL,
 claim_kind TEXT NOT NULL, domain TEXT NOT NULL, text TEXT NOT NULL,
 namespace TEXT NOT NULL, stable_key TEXT NOT NULL UNIQUE, core_event_id TEXT,
 memory_ids TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL,
 created_at TEXT NOT NULL, occurred_at TEXT NOT NULL, consent_scope TEXT NOT NULL,
 visibility_scope TEXT NOT NULL, content_hash TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS evidence_owner_status ON evidence(owner_id,status,domain);
CREATE TABLE IF NOT EXISTS dependencies(
 parent_id TEXT NOT NULL, child_id TEXT NOT NULL REFERENCES resources(id),
 PRIMARY KEY(parent_id,child_id));
CREATE TABLE IF NOT EXISTS outbox(
 id TEXT PRIMARY KEY, evidence_id TEXT NOT NULL REFERENCES evidence(id),
 operation TEXT NOT NULL CHECK(operation IN ('INGEST','DELETE')),
 state TEXT NOT NULL CHECK(state IN ('PENDING','APPLIED','FAILED')),
 attempts INTEGER NOT NULL DEFAULT 0, error_code TEXT,
 UNIQUE(evidence_id,operation));
CREATE TABLE IF NOT EXISTS audit(
 id TEXT PRIMARY KEY, actor TEXT NOT NULL, operation TEXT NOT NULL,
 resource_id TEXT NOT NULL, at TEXT NOT NULL, detail_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS requests(
 key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, response TEXT NOT NULL);
"""


class AppStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        existed = path.exists() and path.stat().st_size > 0
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.migration_backup_path: Path | None = None
        schema = self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'"
        ).fetchone()
        version = self.db.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] if schema else 0
        if existed and (version or 0) < 3:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            self.migration_backup_path = path.with_name(f"{path.stem}.pre-v3.{stamp}.db")
            backup = sqlite3.connect(str(self.migration_backup_path))
            try:
                self.db.backup(backup)
            finally:
                backup.close()
        self.db.executescript(SCHEMA)
        self.db.execute("PRAGMA secure_delete=ON")
        self.lock = threading.RLock()
        self._migrate_v2()
        with self.transaction():
            self.db.execute("INSERT OR IGNORE INTO schema_version(version) VALUES(3)")
        with self.transaction():
            # An interrupted real reply is visible and manually retryable. It
            # must never trigger a surprise provider request on application start.
            self.db.execute(
                "UPDATE messages SET reply_state=CASE WHEN EXISTS(SELECT 1 FROM messages AS agent "
                "WHERE agent.owner_id=messages.owner_id AND agent.conversation_id=messages.conversation_id "
                "AND agent.speaker='AGENT' AND agent.request_key=messages.request_key) "
                "THEN 'REPLIED' ELSE 'FAILED' END WHERE speaker='USER' AND reply_state='WAITING'")

    def _migrate_v2(self) -> None:
        version = self.db.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        if version >= 2:
            return
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(messages)")}
        additions = {
            "sequence": "INTEGER NOT NULL DEFAULT 0",
            "client_request_id": "TEXT",
            "deleted": "INTEGER NOT NULL DEFAULT 0 CHECK(deleted IN (0,1))",
            "reply_state": "TEXT NOT NULL DEFAULT 'REPLIED' CHECK(reply_state IN ('WAITING','REPLIED','FAILED'))",
            "request_domain": "TEXT",
            "remember_requested": "INTEGER CHECK(remember_requested IN (0,1))",
        }
        with self.transaction():
            for name, declaration in additions.items():
                if name not in columns:
                    self.db.execute(f"ALTER TABLE messages ADD COLUMN {name} {declaration}")
            counters: dict[tuple[str, str], int] = {}
            for message in self.db.execute("SELECT rowid,* FROM messages ORDER BY rowid").fetchall():
                pair = (message["owner_id"], message["conversation_id"])
                counters[pair] = counters.get(pair, 0) + 1
                self.db.execute("UPDATE messages SET sequence=?,client_request_id=request_key,"
                                "deleted=? WHERE id=?",
                                (counters[pair], int(message["text"] in {
                                    "[已删除]", "[关联来源已删除]"}), message["id"]))
                if message["speaker"] == "USER":
                    replied = self.db.execute(
                        "SELECT 1 FROM messages WHERE owner_id=? AND conversation_id=? "
                        "AND speaker='AGENT' AND request_key=?",
                        (message["owner_id"], message["conversation_id"], message["request_key"]),
                    ).fetchone()
                    # v1 never stored the original send parameters. Current
                    # evidence may have been admitted later with another scope,
                    # so it cannot reconstruct remember/domain for the old body.
                    self.db.execute("UPDATE messages SET reply_state=?,request_domain=?,"
                                    "remember_requested=? WHERE id=?",
                                    ("REPLIED" if replied else "FAILED",
                                     None, None, message["id"]))
            self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS messages_conversation_sequence "
                            "ON messages(owner_id,conversation_id,sequence)")
            self.db.execute("INSERT OR IGNORE INTO schema_version(version) VALUES(2)")

    def add_message(self, conversation_id: str, speaker: str, text: str, *,
                    request_key: str, request_domain: str | None = None,
                    remember_requested: bool | None = None, reply_state: str = "REPLIED",
                    message_id: str | None = None) -> str:
        """Caller holds transaction(); sequence is stable even when clocks tie."""
        message_id = message_id or uid("msg")
        sequence = self.db.execute(
            "SELECT COALESCE(MAX(sequence),0)+1 FROM messages "
            "WHERE owner_id='local' AND conversation_id=?", (conversation_id,)
        ).fetchone()[0]
        self.db.execute(
            "INSERT INTO messages(id,conversation_id,owner_id,speaker,text,created_at,evidence_id,"
            "request_key,sequence,client_request_id,deleted,reply_state,request_domain,remember_requested) "
            "VALUES(?,?,'local',?,?,?,NULL,?,?,?,0,?,?,?)",
            (message_id, conversation_id, speaker, text, now(), request_key, sequence,
             request_key, reply_state, request_domain,
             None if remember_requested is None else int(remember_requested)),
        )
        return message_id

    @contextmanager
    def transaction(self):
        with self.lock, self.db:
            yield

    def fetchone(self, sql: str, parameters=()):
        """Serialize cursor execution and consumption on the shared connection."""
        with self.lock:
            return self.db.execute(sql, parameters).fetchone()

    def fetchall(self, sql: str, parameters=()):
        with self.lock:
            return self.db.execute(sql, parameters).fetchall()

    def add(self, kind: str, data: dict, owner: str = "local", rid: str | None = None) -> dict:
        operation = operation_context.get()
        if rid is None and operation is not None:
            index = operation["index"]
            operation["index"] += 1
            rid = kind + "_" + digest([operation["key"], kind, index])[:32]
            existing = self.get(rid, kind, owner)
            if existing is not None:
                return existing
        data = {"id": rid or uid(kind), "created_at": now(), **data}
        self.db.execute("INSERT INTO resources VALUES(?,?,?,?,?)",
                        (data["id"], kind, owner, canonical(data), data["created_at"]))
        return data

    def get(self, rid: str, kind: str | None = None, owner: str = "local") -> dict | None:
        row = self.fetchone("SELECT * FROM resources WHERE id=? AND owner_id=?", (rid, owner))
        if row is None or (kind is not None and row["kind"] != kind):
            return None
        return json.loads(row["data"])

    def update(self, value: dict) -> None:
        self.db.execute("UPDATE resources SET data=? WHERE id=? AND owner_id='local'",
                        (canonical(value), value["id"]))

    def all(self, kind: str, owner: str = "local") -> list[dict]:
        return [json.loads(r[0]) for r in self.fetchall(
            "SELECT data FROM resources WHERE kind=? AND owner_id=? ORDER BY created_at,id",
            (kind, owner))]

    def audit(self, operation: str, rid: str, detail=None, actor: str = "HUMAN") -> None:
        self.db.execute("INSERT INTO audit VALUES(?,?,?,?,?,?)",
                        (uid("audit"), actor, operation, rid, now(), digest(detail)))

    def link(self, parent: str, child: str) -> None:
        self.db.execute("INSERT OR IGNORE INTO dependencies VALUES(?,?)", (parent, child))
