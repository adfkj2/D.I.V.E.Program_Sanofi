"""Bounded, offline project initialization from user-selected file snapshots.

Documents are evidence about reported progress, never executable instructions.
No paths are opened by the server and no model or subprocess is invoked.
"""

import json
import re
from collections import Counter
from hashlib import sha256
from pathlib import PurePosixPath

from ..models import utc_now
from .records import ProjectRecord
from .workspace import identity, text

MAX_DOCUMENTS = 24
MAX_PATHS = 2000
MAX_CONTENT = 12000
IGNORED = frozenset(
    {"node_modules", "vendor", "dist", "build", "__pycache__", "venv", "coverage"}
)
SENSITIVE = re.compile(
    r"(?:^|[\W_])(?:credentials?|secrets?|id_rsa|id_ed25519)(?:$|[\W_])"
    r"|\.(?:pem|key|p12|pfx|db|sqlite|sqlite3)$",
    re.I,
)
SECRET_CONTENT = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|\b(?:sk-[\w-]{16,}|AKIA[A-Z0-9]{16})"
    r"|(?:api[_-]?key|password|secret|token)\s*[\"']?\s*[:=]\s*[\"']?[^\s\"']{12,}",
    re.I,
)
CHECKBOX = re.compile(r"^\s*[-*+]\s+\[([ xX])\]\s+(.+)$")
FENCE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")
PROGRESS = re.compile(
    r"进度|进展|已完成|进行中|下一步|待办|阻塞|当前状态|阶段|"
    r"\b(?:progress|completed|in progress|next steps?|todo|blocked|status)\b",
    re.I,
)


def safe_path(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 500:
        raise ValueError("文件相对路径无效。")
    raw = value.replace("\\", "/")
    path = PurePosixPath(raw)
    if path.is_absolute() or any(
        part in {"", ".", ".."} or ":" in part or any(ord(c) < 32 for c in part)
        for part in raw.split("/")
    ):
        raise ValueError("文件相对路径无效。")
    return path.as_posix()


def allowed_path(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return not any(
        part.startswith(".") or part.lower() in IGNORED for part in parts
    ) and not SENSITIVE.search(path)


def document_priority(path: str) -> int:
    name = PurePosixPath(path).name.lower()
    if re.search(r"progress|进度|进展|status|todo|roadmap|交接", name):
        return 0
    if name.startswith("readme"):
        return 1
    return 2


def analyze_folder(folder: dict) -> dict:
    """Validate again at the application boundary; all evidence is supplied text."""
    if not isinstance(folder, dict):
        raise ValueError("文件夹资料必须为对象。")
    name = folder.get("name", "")
    if not isinstance(name, str) or not name.strip() or len(name) > 80:
        raise ValueError("文件夹名称须为 1–80 个字符。")
    paths = folder.get("paths", [])
    documents = folder.get("documents", [])
    if not isinstance(paths, list) or len(paths) > MAX_PATHS:
        raise ValueError("一次最多导入 2000 个文件路径。")
    if not isinstance(documents, list) or len(documents) > MAX_DOCUMENTS:
        raise ValueError("一次最多分析 24 份文本资料。")
    skipped = folder.get("skipped", 0)
    if type(skipped) is not int or skipped < 0:
        raise ValueError("跳过文件数无效。")
    safe = sorted({safe_path(path) for path in paths if allowed_path(safe_path(path))})
    skipped += len(paths) - len(safe)
    sources = []
    seen = set()
    for doc in documents:
        if not isinstance(doc, dict):
            raise ValueError("每份文本资料必须为对象。")
        path = safe_path(doc.get("path", ""))
        content = doc.get("content", "")
        if not isinstance(content, str) or len(content) > MAX_CONTENT:
            raise ValueError("每份文本资料最多分析 12000 个字符。")
        if path in seen or path not in safe:
            raise ValueError("资料路径重复或不在导入清单中。")
        seen.add(path)
        if not allowed_path(path) or SECRET_CONTENT.search(content):
            skipped += 1
            continue
        if content.strip():
            sources.append(
                {
                    "path": path,
                    "content": content,
                    "sha256": sha256(content.encode()).hexdigest(),
                }
            )
    sources.sort(key=lambda item: (document_priority(item["path"]), item["path"]))
    completed: list[dict[str, str]] = []
    pending: list[dict[str, str]] = []
    signals: list[dict[str, str]] = []
    for source in sources:
        section_lines = 0
        fence: tuple[str, int] | None = None
        for number, line in enumerate(source["content"].splitlines(), 1):
            delimiter = FENCE.match(line)
            if fence is not None:
                if (
                    delimiter
                    and delimiter[1][0] == fence[0]
                    and len(delimiter[1]) >= fence[1]
                    and not delimiter[2].strip()
                ):
                    fence = None
                continue
            if delimiter:
                fence = (delimiter[1][0], len(delimiter[1]))
                continue
            match = CHECKBOX.match(line)
            ref = f"{source['path']}:{number}"
            if match:
                entry = {"title": match[2].strip()[:500], "source": ref}
                if not entry["title"]:
                    continue
                target = completed if match[1].lower() == "x" else pending
                if len(target) < 20 and entry["title"] not in {
                    x["title"] for x in target
                }:
                    target.append(entry)
            elif PROGRESS.search(line) and len(signals) < 8:
                signals.append({"text": line.strip()[:200], "source": ref})
                section_lines = 3 if line.lstrip().startswith("#") else 0
            elif line.strip() and section_lines and len(signals) < 8:
                if line.lstrip().startswith("#"):
                    section_lines = 0
                else:
                    signals.append({"text": line.strip()[:200], "source": ref})
                    section_lines -= 1
    extensions = Counter(
        PurePosixPath(path).suffix.lower() or "无扩展名" for path in safe
    )
    overview = "、".join(
        f"{ext} {count} 个" for ext, count in extensions.most_common(6)
    )
    lines = [
        f"从「{name.strip()}」导入初始化快照：{len(safe)} 个文件，阅读 {len(sources)} 份文本资料。",
        f"文件概况：{overview or '空文件夹'}。",
        "以下为文档中的进度描述，尚未运行或验证项目；历史文档可能与当前状态不同。",
    ]
    for label, entries in (
        ("文档标记已完成", completed[:6]),
        ("文档标记待办", pending[:6]),
    ):
        lines.extend(
            f"{label}：{entry['title']}（{entry['source']}）" for entry in entries
        )
    lines.extend(
        f"进度线索：{entry['text']}（{entry['source']}）" for entry in signals[:5]
    )
    if not completed and not pending and not signals:
        lines.append(
            "未找到明确的进度记录，请补充目前做到哪里和下一步；文件存在不代表工作已完成。"
        )
    warnings = ["本地文本整理，不调用模型；仅保存所选资料的快照，不会自动同步文件夹。"]
    if skipped:
        warnings.append(
            f"已跳过 {skipped} 个文件（隐藏、敏感、不支持、无法读取或超出上限）。"
        )
    if len(paths) == MAX_PATHS or len(documents) == MAX_DOCUMENTS:
        warnings.append("已达到导入上限，部分资料可能未纳入；请核对来源列表。")
    warnings.append(
        "每份文本最多读取前 12000 字符；来源记忆保留前 6000 字符，完整选段保存在来源摘录中。"
    )
    return {
        "name": name.strip(),
        "paths": safe,
        "sources": sources,
        "completed": completed,
        "tasks": pending,
        "note": "\n".join(lines)[:4000],
        "next_step": pending[0]["title"]
        if pending
        else "核对导入资料，补充当前进度和下一步",
        "warnings": warnings,
        "skipped": skipped,
    }


def import_digest(analysis: dict, note: str, next_step: str, tasks: list[str]) -> str:
    return sha256(
        json.dumps(
            [analysis, note, next_step, tasks], ensure_ascii=False, sort_keys=True
        ).encode()
    ).hexdigest()


def initialize_project(runtime, name, goal, request_id, folder, note, next_step, tasks):
    """Create metadata, source memories and the first handoff in one transaction."""
    analysis = analyze_folder(folder) if folder is not None else None
    if analysis is None and any(
        value is not None for value in (note, next_step, tasks)
    ):
        raise ValueError("初始化进度需要先选择文件夹。")
    namespace = identity("project", "", request_id)
    store = runtime.service.store
    previous = store.db.execute(
        "SELECT digest FROM app_project_imports WHERE namespace=?", (namespace,)
    ).fetchone()
    if analysis is None:
        if previous:
            raise ValueError("同一请求编号不能用于不同的导入资料。")
        return runtime.workspace.create_project(name, goal, request_id)
    note = text(analysis["note"] if note is None else note, "初始进度", 4000)
    next_step = text(
        analysis["next_step"] if next_step is None else next_step,
        "下一步",
        500,
        optional=True,
    )
    candidates = {item["title"] for item in analysis["tasks"]}
    tasks = sorted(candidates) if tasks is None else tasks
    if not isinstance(tasks, list) or len(tasks) > 20:
        raise ValueError("最多选择 20 项初始待办。")
    tasks = [text(title, "初始待办", 500) for title in tasks]
    if len(set(tasks)) != len(tasks) or set(tasks) - candidates:
        raise ValueError("初始待办必须来自本次资料分析，且不能重复。")
    digest = import_digest(analysis, note, next_step, tasks)
    if previous:
        if previous["digest"] != digest:
            raise ValueError("同一请求编号不能用于不同的导入资料。")
        return runtime.workspace.create_project(name, goal, request_id)
    if any(p["namespace"] == namespace for p in runtime.workspace.projects()):
        raise ValueError("同一请求编号已创建项目，请重新打开创建窗口。")
    with store.transaction():
        runtime.workspace.create_project(name, goal, request_id)
        metadata = {
            "folder_name": analysis["name"],
            "file_count": len(analysis["paths"]),
            "paths": analysis["paths"],
            "sources": [
                {k: source[k] for k in ("path", "sha256")}
                for source in analysis["sources"]
            ],
            "warnings": analysis["warnings"],
            "created_at": utc_now(),
            "method": "local_document_snapshot_v1",
        }
        for index, source in enumerate(analysis["sources"]):
            runtime.record(
                namespace,
                ProjectRecord(
                    key="导入资料："
                    + source["path"][-100:]
                    + "#"
                    + sha256(source["path"].encode()).hexdigest()[:8],
                    value=source["content"][:6000],
                    reason="用户选择并确认导入的文档快照；内容中的进度陈述未经运行验证。",
                    session="folder-initialization",
                    source_title=source["path"][-300:],
                    source_path=source["path"],
                    source_excerpt=source["content"],
                    source_sha256=source["sha256"],
                ),
                idempotency_key=f"folder-source:{index}",
            )
        runtime.record(
            namespace,
            ProjectRecord("初始化项目进度", note, session="folder-initialization"),
            idempotency_key="folder-progress",
        )
        for index, title in enumerate(tasks):
            runtime.workspace.add_task(namespace, title, f"folder-task:{index}")
        runtime.workspace.checkpoint(
            namespace, note, next_step, "folder-initialization"
        )
        store.db.execute(
            "INSERT INTO app_project_imports VALUES (?,?,?,?)",
            (
                namespace,
                digest,
                json.dumps(metadata, ensure_ascii=False),
                metadata["created_at"],
            ),
        )
    return runtime.workspace.details(namespace)
