"""Persistent personal-project navigation, tasks and session checkpoints.

These are user-managed workflow data, not extracted facts or model answers.
The caller holds the application's lock; writes share the core transaction.
"""

from hashlib import sha256
from typing import Any

from ..models import utc_now
from ..store import SQLiteStore

SCHEMA = """
CREATE TABLE IF NOT EXISTS app_projects (
  namespace TEXT PRIMARY KEY, name TEXT NOT NULL, goal TEXT NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS app_tasks (
  id TEXT PRIMARY KEY, namespace TEXT NOT NULL, title TEXT NOT NULL,
  completed INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, original_title TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS app_tasks_project ON app_tasks(namespace, created_at);
CREATE TABLE IF NOT EXISTS app_checkpoints (
  id TEXT PRIMARY KEY, namespace TEXT NOT NULL, note TEXT NOT NULL,
  next_step TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS app_checkpoints_project ON app_checkpoints(namespace, created_at);
"""


def text(value: str, field: str, limit: int, *, optional: bool = False) -> str:
    if (
        not isinstance(value, str)
        or len(value) > limit
        or (not optional and not value.strip())
    ):
        raise ValueError(
            f"{field} must contain {'0' if optional else '1'} to {limit} characters"
        )
    return value.strip()


def identity(kind: str, namespace: str, key: str) -> str:
    text(key, "request id", 200)
    return kind + "_" + sha256((namespace + "\0" + key).encode()).hexdigest()[:32]


class ProjectWorkspace:
    def __init__(self, store: SQLiteStore):
        self.store = store
        store.db.executescript(SCHEMA)

    def projects(self) -> list[dict[str, Any]]:
        # Existing workspaces need no manual migration or renaming.
        rows = self.store.db.execute("""
            WITH spaces AS (
              SELECT namespace FROM app_projects UNION SELECT namespace FROM events
              UNION SELECT namespace FROM memories WHERE status != 'DELETED'
            )
            SELECT s.namespace, COALESCE(p.name, s.namespace) AS name,
              COALESCE(p.goal, '') AS goal,
              COALESCE(p.updated_at, (SELECT MAX(created_at) FROM events e
                WHERE e.namespace=s.namespace), '') AS updated_at,
              (SELECT COUNT(*) FROM memories m WHERE m.namespace=s.namespace
                AND m.status IN ('ACTIVE','REINFORCED')) AS record_count,
              (SELECT COUNT(*) FROM app_tasks t WHERE t.namespace=s.namespace
                AND t.completed=0) AS open_tasks
            FROM spaces s LEFT JOIN app_projects p ON p.namespace=s.namespace
            ORDER BY updated_at DESC, s.namespace
        """).fetchall()
        return [dict(row) for row in rows]

    def details(self, namespace: str) -> dict[str, Any]:
        row = self.store.db.execute(
            "SELECT * FROM app_projects WHERE namespace=?", (namespace,)
        ).fetchone()
        checkpoints = self.store.db.execute(
            "SELECT * FROM app_checkpoints WHERE namespace=? ORDER BY created_at DESC, id DESC LIMIT 20",
            (namespace,),
        ).fetchall()
        tasks = self.store.db.execute(
            "SELECT id,namespace,title,completed,revision,created_at,updated_at FROM app_tasks WHERE namespace=? ORDER BY completed,created_at,id",
            (namespace,),
        ).fetchall()
        return {
            "namespace": namespace,
            "name": row["name"] if row else namespace,
            "goal": row["goal"] if row else "",
            "tasks": [dict(task) for task in tasks],
            "checkpoints": [dict(checkpoint) for checkpoint in checkpoints],
        }

    def save_project(self, namespace: str, name: str, goal: str) -> dict:
        name = text(name, "project name", 80)
        goal = text(goal, "project goal", 1000, optional=True)
        if "\n" in name or "\r" in name:
            raise ValueError("project name must be a single line")
        now = utc_now()
        with self.store.transaction():
            self.store.db.execute(
                """
                INSERT INTO app_projects VALUES (?,?,?,?,?)
                ON CONFLICT(namespace) DO UPDATE SET name=excluded.name,goal=excluded.goal,updated_at=excluded.updated_at
            """,
                (namespace, name, goal, now, now),
            )
        return self.details(namespace)

    def create_project(self, name: str, goal: str, request_id: str) -> dict:
        name = text(name, "project name", 80)
        goal = text(goal, "project goal", 1000, optional=True)
        namespace = identity("project", "", request_id)
        existing = self.store.db.execute(
            "SELECT * FROM app_projects WHERE namespace=?", (namespace,)
        ).fetchone()
        if existing:
            if (existing["name"], existing["goal"]) != (name.strip(), goal.strip()):
                raise ValueError("request id already used for a different project")
            return self.details(namespace)
        return self.save_project(namespace, name, goal)

    def _touch(self, namespace: str) -> None:
        now = utc_now()
        self.store.db.execute(
            """
            INSERT INTO app_projects VALUES (?,?,'',?,?)
            ON CONFLICT(namespace) DO UPDATE SET updated_at=excluded.updated_at
        """,
            (namespace, namespace, now, now),
        )

    def add_task(self, namespace: str, title: str, request_id: str) -> dict:
        title = text(title, "task", 500)
        identifier = identity("task", namespace, request_id)
        existing = self.store.db.execute(
            "SELECT * FROM app_tasks WHERE id=?", (identifier,)
        ).fetchone()
        if existing:
            if existing["original_title"] != title:
                raise ValueError("request id already used for a different task")
            return self.details(namespace)
        now = utc_now()
        with self.store.transaction():
            self._touch(namespace)
            self.store.db.execute(
                "INSERT INTO app_tasks VALUES (?,?,?,0,1,?,?,?)",
                (identifier, namespace, title, now, now, title),
            )
        return self.details(namespace)

    def change_task(
        self,
        namespace: str,
        identifier: str,
        title: str,
        completed: bool,
        revision: int,
    ) -> dict:
        title = text(title, "task", 500)
        if not isinstance(completed, bool) or type(revision) is not int or revision < 1:
            raise ValueError("invalid task state or revision")
        with self.store.transaction():
            existing = self.store.db.execute(
                "SELECT * FROM app_tasks WHERE id=? AND namespace=?",
                (identifier, namespace),
            ).fetchone()
            if existing is None:
                raise KeyError("task not found in this project")
            if existing["revision"] != revision:
                # A response can be lost after commit; the identical retry is safe.
                if (existing["title"], bool(existing["completed"])) == (
                    title,
                    completed,
                ):
                    return self.details(namespace)
                raise ValueError("这条待办已在其他页面更新，请刷新后重试。")
            self.store.db.execute(
                "UPDATE app_tasks SET title=?,completed=?,revision=revision+1,updated_at=? WHERE id=?",
                (title, int(completed), utc_now(), identifier),
            )
            self._touch(namespace)
        return self.details(namespace)

    def checkpoint(
        self, namespace: str, note: str, next_step: str, request_id: str
    ) -> dict:
        note = text(note, "session note", 4000)
        next_step = text(next_step, "next step", 500, optional=True)
        identifier = identity("checkpoint", namespace, request_id)
        existing = self.store.db.execute(
            "SELECT * FROM app_checkpoints WHERE id=?", (identifier,)
        ).fetchone()
        if existing:
            if (existing["note"], existing["next_step"]) != (note, next_step):
                raise ValueError("request id already used for a different checkpoint")
            return self.details(namespace)
        with self.store.transaction():
            self._touch(namespace)
            self.store.db.execute(
                "INSERT INTO app_checkpoints VALUES (?,?,?,?,?)",
                (identifier, namespace, note, next_step, utc_now()),
            )
            if next_step:
                self.add_task(namespace, next_step, "checkpoint:" + identifier)
        return self.details(namespace)
