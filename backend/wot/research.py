"""Application-owned, read-only public research; never execute persona/web tools.

Only fixed provider APIs are contacted by this process. Search phrases and full
pages are deliberately not durable records; published evidence is a bounded,
source-linked excerpt, scoped to its conversation.
"""
from __future__ import annotations

import ipaddress
import json
import re
import socket
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from urllib.parse import unquote, urlsplit, urlunsplit

import httpx

from .connections import _OSVault
from .domain import DomainError
from .research_providers import (
    build_fetch,
    build_search,
    extract_usage,
    normalise_fetch,
    normalise_search,
    provider_catalog,
    provider_info,
    validate_provider_config,
)
from .storage import digest, now, uid

_WEB_SLOTS = threading.BoundedSemaphore(2)
_DNS_SLOTS = threading.BoundedSemaphore(4)
_VAULT = "WeOnceTalked.web-research"
_CONFIG = "web_research_config"
_ACTIVE = {"NEW", "SEARCHING", "VERIFYING", "GENERATING"}
_STOPPED = {"INVALIDATED", "INTERRUPTED", "CANCELLED"}
_MAX_BODY = 3_000_000
_SECRET = re.compile(r"(?:sk-|tvly-|Bearer\s+)\S{8,}|\b[A-Za-z0-9_-]{32,}\b", re.I)
_PRIVATE = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|(?<!\d)1[3-9]\d{9}(?!\d)|(?<!\d)\d{17}[\dXx](?!\d)")


class ResearchLimit(DomainError):
    reason = "WEB_REQUEST_LIMIT"

    def __init__(self):
        super().__init__("本轮联网查询已达到限制，请缩小问题后重试", 409)


def public_url(value, resolver=None, *, timeout=3):
    """Validate a public target, including all DNS answers; no local fetch occurs.

    Tavily performs extraction remotely. We still reject private/credentialed
    targets before sending them to that service and revalidate returned URLs.
    """
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 33 for c in value):
        raise DomainError("仅支持有效的公开网页地址")
    try:
        parts = urlsplit(value)
        host = parts.hostname
        port = parts.port
    except (ValueError, UnicodeError):
        raise DomainError("网页地址格式无效") from None
    if (parts.scheme not in {"http", "https"} or not host or parts.username is not None
            or parts.password is not None or port not in {None, 80, 443} or "\\" in value):
        raise DomainError("仅支持没有登录凭据的公开 HTTP/HTTPS 网页")
    host = host.rstrip(".").lower()
    if (_PRIVATE.search(unquote(value)) or re.search(
            r"(?:^|[&;])(?:api[_-]?key|access[_-]?token|token|password|authorization|signature)=",
            parts.query, re.I)):
        raise DomainError("网页地址不能包含私人信息或凭据")
    if (host in {"localhost", "metadata.google.internal"} or "." not in host and ":" not in host
            or host.endswith((".localhost", ".local", ".internal", ".lan", ".home", ".invalid"))):
        raise DomainError("不能查询本机、内部网络或保留地址")
    try:
        literal = ipaddress.ip_address(host)
        addresses = [str(literal)]
    except ValueError:
        lookup = resolver or socket.getaddrinfo
        try:
            # getaddrinfo has no timeout. A bounded daemon worker prevents a
            # stuck system resolver from hanging the research coordinator.
            if not _DNS_SLOTS.acquire(timeout=max(0.01, timeout)):
                raise TimeoutError()
            completed, result = threading.Event(), []
            def resolve():
                try:
                    result.append(lookup(host, port or (443 if parts.scheme == "https" else 80), type=socket.SOCK_STREAM))
                except Exception:
                    result.append(None)
                finally:
                    _DNS_SLOTS.release()
                    completed.set()
            threading.Thread(target=resolve, name="wot-public-dns", daemon=True).start()
            if not completed.wait(max(0.01, timeout)) or not result[0]:
                raise TimeoutError()
            answers = result[0]
            addresses = [answer[4][0] for answer in answers]
        except Exception:
            raise DomainError("网页地址无法解析为公开地址") from None
    if not addresses:
        raise DomainError("网页地址无法解析为公开地址")
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address.split("%", 1)[0])
        except ValueError:
            raise DomainError("网页地址无法解析为公开地址") from None
        mapped = getattr(ip, "ipv4_mapped", None)
        if not ip.is_global or (mapped is not None and not mapped.is_global):
            raise DomainError("不能查询本机、内部网络或保留地址")
    # Fragments are browser-only; never store them as distinct evidence.
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, ""))


def _excerpt(text, limit=4200):
    """Keep complete lines/paragraphs; do not slice a table row or sentence."""
    if not isinstance(text, str):
        return ""
    text = text.replace("\x00", "")
    lines, size = [], 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            if lines and lines[-1] != "":
                lines.append("")
            continue
        if len(line) > limit:
            # One exceptionally long paragraph: retain only whole sentences.
            segments = re.split(r"(?<=[。！？.!?])\s*", line)
        else:
            segments = [line]
        for segment in segments:
            if not segment or size + len(segment) + 1 > limit:
                continue
            lines.append(segment)
            size += len(segment) + 1
    return "\n".join(lines).strip()


def _bounded_sources(sources):
    """Approximately 6000 evidence tokens, separate from persona/history."""
    result, estimated = [], 0
    seen = set()
    for source in sources:
        if source["url"] in seen or len(result) >= 5:
            continue
        remaining = 6000 - estimated
        if remaining <= 0:
            break
        text = _excerpt(source["excerpt"], min(4200, int(remaining / 1.5)))
        if not text:
            continue
        wide = sum(ord(c) > 127 for c in text)
        estimated += wide * 1.5 + (len(text) - wide) / 3
        result.append({**source, "excerpt": text, "content_hash": digest(text)})
        seen.add(source["url"])
    return result


class ResearchManager:
    def __init__(self, service, directory=None, *, credential_backend=None,
                 http_client=None, resolver=None, clock=None, connection_manager=None):
        self.service, self.store = service, service.store
        self.vault = credential_backend if credential_backend is not None else _OSVault()
        self.client = http_client or httpx.Client(timeout=20, follow_redirects=False, trust_env=False)
        self._owns_client = http_client is None
        self.resolver, self.clock = resolver, clock or time.monotonic
        self.connections = connection_manager
        self._session_keys = {}
        self._lock = threading.RLock()
        self._deadlines = {}
        with self._atomic():
            deleted_scopes = {conversation["id"] for conversation in self.store.all("conversation")
                              if conversation.get("deleted_at")}
            cleanup_scopes = set()
            for run in self.store.all("research_run"):
                if run.get("scope_kind") == "CHAT" and run.get("scope_id") in deleted_scopes:
                    if (run.get("phase") != "INVALIDATED" or any(run.get(key) for key in
                            ("sources", "unread_sources", "publications", "query_hashes", "requested_urls"))):
                        cleanup_scopes.add(run["scope_id"])
                    continue
                if run.get("phase") in _ACTIVE:
                    run.update(phase="INTERRUPTED", notice="上次查询已中断；不会自动产生新请求")
                    if not run.get("publications"):
                        run.update(sources=[], unread_sources=[])
                    self.store.update(run)
            for scope_id in cleanup_scopes:
                self.invalidate_scope(scope_id, "会话已删除")

    @contextmanager
    def _atomic(self):
        with self.store.lock:
            if self.store.db.in_transaction:
                yield
            else:
                with self.store.transaction():
                    yield

    def _value(self):
        return self.store.get(_CONFIG, "research_config") or {}

    def _key(self, value=None):
        value = self._value() if value is None else value
        if value.get("credential_mode") == "CONNECTION":
            try:
                connection = self._connection(value)
                return self.connections._key(connection)
            except DomainError:
                return None
        ref = value.get("credential_ref")
        if not ref:
            return None
        if ref in self._session_keys:
            return self._session_keys[ref]
        if value.get("remember"):
            try:
                result = self.vault.get_password(_VAULT, ref)
                return result if isinstance(result, str) and result else None
            except Exception:
                return None
        return None

    def _connection(self, value):
        provider = value.get("provider")
        info = provider_info(provider)
        if not self.connections or not info.get("reuse_provider"):
            raise DomainError("该联网服务不能复用模型连接，请填写对应服务的 Key")
        rid = value.get("connection_id")
        if not isinstance(rid, str) or not rid or len(rid) > 200:
            raise DomainError("请选择已保存的对应模型连接")
        connection = self.connections._get(rid)
        hosts = {"deepseek": "api.deepseek.com", "kimi": "api.moonshot.cn", "glm": "open.bigmodel.cn"}
        try:
            url = urlsplit(connection.get("base_url", ""))
            port = url.port
        except (ValueError, TypeError):
            raise DomainError("所选连接的官方接口地址无效") from None
        if (connection.get("provider") != info["reuse_provider"] or url.scheme != "https"
                or url.hostname != hosts.get(provider) or port not in {None, 443}
                or url.username is not None or url.password is not None or url.query or url.fragment):
            raise DomainError("只能复用对应供应商的境内官方接口连接；第三方代理 Key 不会转发给原厂")
        if not self.connections._key(connection):
            raise DomainError("所选模型连接的 Key 不可用，请先补充模型配置")
        return connection

    def _binding(self, value):
        if value.get("credential_mode") != "CONNECTION":
            return None
        try:
            connection = self._connection(value)
            return digest({key: connection.get(key) for key in
                           ("id", "provider", "base_url", "credential_ref", "updated_at")})
        except DomainError:
            return "UNAVAILABLE"

    def providers(self):
        return [{**info, "engines": info.get("search_engines", []),
                 "default_engine": "search_std" if info["id"] == "glm" else None}
                for info in provider_catalog()]

    def config(self):
        value = self._value()
        configured = bool(self._key(value))
        available = not hasattr(self.vault, "backend") or self.vault.backend is not None
        provider = value.get("provider", "tavily" if value.get("credential_ref") else "deepseek")
        info = provider_info(provider)
        mode = value.get("credential_mode", "SEPARATE")
        connection = None
        if mode == "CONNECTION" and configured:
            try:
                connection = self._connection(value)
            except DomainError:
                configured = False
        remember = bool(connection.get("remember")) if connection else bool(value.get("remember"))
        return {"provider": provider, "provider_label": info["name"], "configured": configured,
                "credential_mode": mode, "connection_id": value.get("connection_id"),
                "connection_label": connection.get("label") or connection.get("model") if connection else None,
                "engine": value.get("search_engine", "search_std") if provider == "glm" else None,
                "query_max_chars": info["query_limit"],
                "enabled": bool(value.get("enabled", False)) and configured,
                "key_status": "CONFIGURED" if configured else "MISSING",
                "key_state": "CONFIGURED" if configured else "MISSING",
                "api_key_mask": "••••••••" if configured else "",
                "remember": remember,
                "remember_key": remember,
                "storage": "OS_CREDENTIAL_STORE" if remember else "SESSION_ONLY",
                "credential_store_available": available,
                "revision": value.get("revision", 0), "last_test": value.get("last_test")}

    def validate(self, data):
        if not isinstance(data, dict) or set(data) - {
            "api_key", "remember", "remember_key", "enabled", "provider",
            "credential_mode", "connection_id", "engine", "search_engine",
        }:
            raise DomainError("联网配置包含未知字段")
        before = self._value()
        revision = before.get("revision", 0)
        provider = data.get("provider", before.get("provider", "tavily"))
        info = provider_info(provider)
        mode = data.get("credential_mode", before.get("credential_mode", "SEPARATE"))
        if mode not in {"CONNECTION", "SEPARATE"}:
            raise DomainError("请选择复用模型连接或单独填写服务 Key")
        options = validate_provider_config(provider, {
            "search_engine": data.get("engine", data.get("search_engine", before.get("search_engine", "search_std"))),
        })
        settings = {"provider": provider, "credential_mode": mode, **options}
        if mode == "CONNECTION":
            if data.get("api_key"):
                raise DomainError("复用连接时无需再次填写 Key，请清除单独输入的 Key")
            settings["connection_id"] = data.get("connection_id", before.get("connection_id"))
            self._connection(settings)
            key = self._key(settings)
        else:
            same = (before.get("provider", "tavily") == provider
                    and before.get("credential_mode", "SEPARATE") == mode)
            key = data.get("api_key") or (self._key(before) if same else None)
        if not isinstance(key, str) or not 8 <= len(key.strip()) <= 1024 or any(ord(c) < 33 for c in key.strip()):
            raise DomainError("请填写有效的搜索 API Key")
        key = key.strip()
        if provider == "tavily" and key.startswith("sk-"):
            raise DomainError("这里需要 Tavily Key；使用 DeepSeek Key 请将联网来源选择为 DeepSeek 搜索")
        if provider != "tavily" and key.startswith("tvly-"):
            raise DomainError(f"这里需要{info['key_hint']}；Tavily Key 只能用于 Tavily")
        remember = data.get("remember_key", data.get("remember", before.get("remember", False))) if mode == "SEPARATE" else False
        enabled = data.get("enabled", True)
        if type(remember) is not bool or type(enabled) is not bool:
            raise DomainError("联网设置参数格式无效")
        started = self.clock()
        binding = self._binding(settings)
        try:
            payload = ({"query": "Tavily connection test", "max_results": 1,
                        "include_answer": False, "include_raw_content": False,
                        "include_usage": True, "auto_parameters": False} if provider == "tavily"
                       else {"query": "联网连接测试"})
            tested = self._request("search", payload, key=key, purpose="TEST",
                                   provider=provider, settings=settings)
            normalise_search(provider, tested)
        except DomainError as exc:
            return {"status": "TEST_FAILED", "success": False, "tested": False, "saved": False,
                    "message": str(exc), "elapsed_ms": round((self.clock() - started) * 1000),
                    "config": self.config()}
        elapsed = round((self.clock() - started) * 1000)
        # New reference makes credential changes atomic with config revision.
        ref = uid("web_credential") if mode == "SEPARATE" else None
        try:
            with self._lock:
                if self._value().get("revision", 0) != revision:
                    raise DomainError("联网配置已改变，本次测试结果未保存", 409)
                if binding != self._binding(settings):
                    raise DomainError("所选模型连接在测试期间已改变，请重新测试", 409)
                if remember and ref:
                    self.vault.set_password(_VAULT, ref, key)
                elif ref:
                    self._session_keys[ref] = key
                with self._atomic():
                    current = {**settings, "credential_ref": ref, "remember": remember, "enabled": enabled,
                               "revision": revision + 1, "last_test": {"success": True,
                               "elapsed_ms": elapsed, "at": now()}, "updated_at": now()}
                    if before:
                        current.update(id=_CONFIG, created_at=before["created_at"])
                        self.store.update(current)
                    else:
                        self.store.add("research_config", current, rid=_CONFIG)
                self._erase_key(before)
        except Exception:
            self._session_keys.pop(ref, None)
            if remember and ref:
                try:
                    self.vault.delete_password(_VAULT, ref)
                except Exception:
                    pass
            return {"status": "SAVE_FAILED", "success": False, "tested": True, "saved": False,
                    "message": "连接成功但保存失败；请保留表单并检查连接或安全存储后重试", "elapsed_ms": elapsed,
                    "config": self.config()}
        return {"status": "UPDATED" if before.get("credential_ref") or before.get("connection_id") else "ADDED", "success": True,
                "tested": True, "saved": True, "message": "已保存联网查询配置", "elapsed_ms": elapsed,
                "config": self.config()}

    def _erase_key(self, value):
        ref = value.get("credential_ref")
        if not ref:
            return
        self._session_keys.pop(ref, None)
        if value.get("remember"):
            try:
                self.vault.delete_password(_VAULT, ref)
            except Exception:
                pass

    def remove(self):
        with self._lock, self._atomic():
            old = self._value()
            value = {"enabled": False, "remember": False, "revision": old.get("revision", 0) + 1,
                     "updated_at": now(), "last_test": None}
            if old:
                value.update(id=_CONFIG, created_at=old["created_at"])
                self.store.update(value)
            else:
                self.store.add("research_config", value, rid=_CONFIG)
            self._erase_key(old)
            for run in self.store.all("research_run"):
                if run.get("phase") in _ACTIVE:
                    run.update(phase="CANCELLED", status="CANCELLED", sources=[], unread_sources=[],
                               notice="联网配置已移除，本轮查询已取消")
                    self.store.update(run)
        return self.config()

    def _run(self, rid):
        run = self.store.get(rid, "research_run")
        if run is None:
            raise DomainError("查询记录不存在", 404)
        return run

    def begin(self, *, scope_kind, scope_id, agent_id, generation_id, version_id,
              source_message_ids=None):
        config = self.config()
        status = "PENDING" if config["enabled"] else "NOT_CONFIGURED" if not config["configured"] else "DISABLED"
        with self._atomic():
            run = self.store.add("research_run", {"scope_kind": scope_kind, "scope_id": scope_id,
                "agent_id": agent_id, "generation_id": generation_id, "version_id": version_id,
                "source_message_ids": list(source_message_ids or []),
                "provider": config["provider"], "connection_binding": self._binding(self._value()),
                "config_revision": config["revision"],
                "privacy_revision": self.service.privacy_revision,
                "phase": "NEW", "status": status, "sources": [], "unread_sources": [],
                "publications": [], "query_hashes": [], "rounds": 0, "searches": 0,
                "pages": 0, "requested_urls": [], "retrieval_seconds": 0, "web_requests": 0,
                "notice": "" if status == "PENDING" else "尚未配置联网查询，不能声称已核实当前事实"})
            for parent in [version_id, *(source_message_ids or [])]:
                if parent:
                    self.store.link(parent, run["id"])
        return run["id"]

    def stale(self, rid):
        run = self._run(rid)
        if run.get("phase") in _STOPPED:
            return True
        if run.get("config_revision") != self._value().get("revision", 0):
            return True
        if run.get("connection_binding") != self._binding(self._value()):
            return True
        version = self.store.get(run.get("version_id", ""), "version")
        if version and version.get("state") == "INVALIDATED":
            return True
        # A privacy epoch change requires checking actual dependencies, not
        # stopping an unrelated public-character conversation.
        if run.get("privacy_revision") != self.service.privacy_revision:
            for mid in run.get("source_message_ids", []):
                msg = self.store.fetchone("SELECT deleted FROM messages WHERE id=?", (mid,))
                if msg and msg["deleted"]:
                    return True
        return False

    def _check(self, rid, cancelled=None):
        if (cancelled and cancelled()) or self.stale(rid):
            raise DomainError("本次查询已取消", 409)
        if self.clock() >= self._deadlines.get(rid, float("inf")):
            raise DomainError("本轮查资料已超时，尚未核实的内容不能作为结论", 504)

    def stage(self, rid, state):
        if state not in {*_ACTIVE, *_STOPPED, "COMPLETE", "FAILED"}:
            raise DomainError("查询阶段无效")
        with self._atomic():
            run = self._run(rid)
            if run.get("phase") in _STOPPED:
                return
            run["phase"] = state
            self.store.update(run)

    def _query(self, value):
        if not isinstance(value, str) or not 2 <= len(value.strip()) <= 300:
            raise DomainError("查询词应为2至300字的必要公开信息")
        value = value.strip()
        if (any(ord(c) < 32 for c in value) or _SECRET.search(value) or _PRIVATE.search(value)
                or re.search(r"(?:api[ _-]*key|密钥|密码|authorization)\s*[:=：]\s*\S+", value, re.I)):
            raise DomainError("查询词包含私人信息或凭据，请改用必要的公开条件")
        key = self._key()
        if key and key in value:
            raise DomainError("查询词不能包含凭据")
        # Search operators are allowed; URLs carrying credentials/private
        # addresses are not necessary query terms and are rejected outright.
        if re.search(r"(?:https?|file|ftp|data|javascript)://", value, re.I):
            raise DomainError("请使用公开检索词，不要把网址或私密地址作为查询指令")
        return value

    def _safe_text(self, value, limit):
        text = value if isinstance(value, str) else ""
        # A service can accidentally echo authentication in a successful body
        # too. Neither source excerpts nor metadata may persist that secret.
        secret = self._key()
        if secret:
            text = text.replace(secret, "[凭据已隐藏]")
        text = _SECRET.sub("[凭据已隐藏]", text)
        text = _PRIVATE.sub("[私人信息已隐藏]", text)
        return text[:limit]

    def _cache(self, run, query_hash):
        # Select a parent and attach its deletion lineage atomically, before a
        # caller may merge the returned snapshot into the current generation.
        with self._atomic():
            self._check(run["id"])
            run = self._run(run["id"])
            current = datetime.now(timezone.utc)
            for old in reversed(self.store.all("research_run")):
                if (old["id"] == run["id"] or old.get("scope_id") != run["scope_id"]
                        or old.get("provider", "tavily") != run.get("provider", "tavily")
                        or query_hash not in old.get("query_hashes", []) or old.get("phase") in _STOPPED
                        or not old.get("publications") or not self._published(old)):
                    continue
                result = []
                for source in old.get("sources", []):
                    try:
                        age = (current - datetime.fromisoformat(source["retrieved_at"])).total_seconds()
                    except (ValueError, KeyError):
                        continue
                    if 0 <= age <= 900:
                        result.append(dict(source))
                if result:
                    self.store.link(old["id"], run["id"])
                    return result
        return []

    def collect(self, rid, queries, *, cancelled=None, on_request=None):
        if self._run(rid).get("provider", "tavily") != "tavily":
            return self._collect_domestic(rid, queries, cancelled=cancelled, on_request=on_request)
        started = self.clock()
        self._deadlines[rid] = started + max(0, 45 - self._run(rid).get("retrieval_seconds", 0))
        self._check(rid, cancelled)
        run = self._run(rid)
        if run["status"] in {"NOT_CONFIGURED", "DISABLED"}:
            return self.context(rid)
        if not isinstance(queries, list) or not 1 <= len(queries) <= 3:
            raise DomainError("每轮只允许有限的公开查询词")
        queries = list(dict.fromkeys(self._query(q) for q in queries))
        if run["rounds"] >= 2 or run["searches"] + len(queries) > 3:
            raise ResearchLimit()
        with self._atomic():
            self._check(rid, cancelled)
            run = self._run(rid)
            run.update(rounds=run["rounds"] + 1, phase="SEARCHING")
            self.store.update(run)
        candidates = []
        known = {s["url"] for s in run["sources"]} | set(run.get("requested_urls", []))
        try:
            for query in queries:
                self._check(rid, cancelled)
                qhash = digest(query)
                with self._atomic():
                    self._check(rid, cancelled)
                    run = self._run(rid)
                    if qhash not in run["query_hashes"]:
                        run["query_hashes"].append(qhash)
                    run["searches"] += 1
                    self.store.update(run)
                cached = self._cache(run, qhash)
                if cached:
                    with self._atomic():
                        self._check(rid, cancelled)
                        current = self._run(rid)
                        for source in cached:
                            if source["url"] not in known:
                                current["sources"].append({**source, "cache_hit": True})
                                known.add(source["url"])
                        current["sources"] = _bounded_sources(current["sources"])
                        self.store.update(current)
                    continue
                response = self._request("search", {"query": query, "search_depth": "basic",
                    "max_results": 5, "include_answer": False, "include_raw_content": False,
                    "include_published_date": True, "include_usage": True,
                    "auto_parameters": False}, rid=rid, cancelled=cancelled, on_request=on_request)
                for item in response.get("results", [])[:10]:
                    self._check(rid, cancelled)
                    if not isinstance(item, dict):
                        continue
                    try:
                        url = public_url(item.get("url"), self.resolver,
                                         timeout=min(3, max(0.01, self._deadlines[rid] - self.clock())))
                    except DomainError:
                        continue
                    if url in known or any(c["url"] == url for c in candidates):
                        continue
                    host = urlsplit(url).hostname or ""
                    official = host.endswith((".gov.cn", ".edu.cn", ".gov", ".edu"))
                    if self._key() and self._key() in unquote(url):
                        continue
                    candidates.append({"url": url, "title": self._safe_text(item.get("title", host), 240),
                        "published_at": self._safe_text(item["published_date"], 80) if item.get("published_date") else None,
                        "institution": host, "publisher": host,
                        "authority": "OFFICIAL_DOMAIN" if official else "PUBLIC_SOURCE"})
            self._check(rid, cancelled)
            self.stage(rid, "VERIFYING")
            run = self._run(rid)
            remaining = max(0, 5 - len(set(run.get("requested_urls", [])) | {s["url"] for s in run["sources"]}))
            candidates.sort(key=lambda item: item["authority"] != "OFFICIAL_DOMAIN")
            selected = candidates[:remaining]
            if selected:
                with self._atomic():
                    self._check(rid, cancelled)
                    run = self._run(rid)
                    run.setdefault("requested_urls", []).extend(c["url"] for c in selected)
                    run["pages"] = len(set(run["requested_urls"]))
                    self.store.update(run)
                response = self._request("extract", {"urls": [c["url"] for c in selected],
                    "extract_depth": "advanced", "format": "text", "include_images": False,
                    "query": " ".join(queries)[:600], "chunks_per_source": 3,
                    "include_usage": True, "timeout": min(15, max(1, int(self._deadlines[rid] - self.clock())))},
                    rid=rid, cancelled=cancelled, on_request=on_request)
                self._check(rid, cancelled)
                by_url = {c["url"]: c for c in selected}
                read = set()
                sources = []
                for item in response.get("results", [])[:10]:
                    self._check(rid, cancelled)
                    if not isinstance(item, dict):
                        continue
                    try:
                        url = public_url(item.get("url"), self.resolver,
                                         timeout=min(3, max(0.01, self._deadlines[rid] - self.clock())))
                    except DomainError:
                        continue
                    if url not in by_url or url in read:
                        continue
                    excerpt = _excerpt(self._safe_text(item.get("raw_content"), _MAX_BODY))
                    if not excerpt:
                        continue
                    years = sorted(set(re.findall(r"(?<!\d)20\d{2}(?!\d)", excerpt)))
                    source = {**by_url[url], "id": "web_source_" + digest([rid, url, excerpt])[:24],
                              "retrieved_at": now(), "applicable_years": years,
                              "applicable_year": "、".join(years) if years else None,
                              "excerpt": excerpt, "content_hash": digest(excerpt), "cache_hit": False}
                    sources.append(source)
                    read.add(url)
                with self._atomic():
                    self._check(rid, cancelled)
                    run = self._run(rid)
                    run["sources"].extend(sources)
                    run["sources"] = _bounded_sources(run["sources"])
                    for item in selected:
                        if item["url"] not in read:
                            run["unread_sources"].append({"url": item["url"], "title": item["title"],
                                                         "reason": "资料未读取"})
                    self.store.update(run)
            with self._atomic():
                self._check(rid, cancelled)
                run = self._run(rid)
                run.update(status="READY" if run["sources"] else "NO_RESULTS",
                           notice="检索内容是网页陈述，仍需核对年份、适用范围和相互冲突之处" if run["sources"] else
                           "没有读取到能支持结论的公开正文；不能根据搜索标题补造事实")
                self.store.update(run)
        except DomainError as exc:
            # Coordinator budget errors must pause, never be disguised as a
            # successful search or converted into a generic persona answer.
            if getattr(exc, "reason", None) or exc.status == 409:
                raise
            with self._atomic():
                if self.stale(rid) or cancelled and cancelled():
                    raise DomainError("本次查询已取消", 409) from None
                run = self._run(rid)
                run.update(status="FAILED", phase="FAILED", notice=str(exc))
                self.store.update(run)
        finally:
            with self._atomic():
                run = self._run(rid)
                run["retrieval_seconds"] = run.get("retrieval_seconds", 0) + max(0, self.clock() - started)
                self.store.update(run)
        return self.context(rid)

    def _collect_domestic(self, rid, queries, *, cancelled=None, on_request=None):
        started = self.clock()
        run = self._run(rid)
        self._deadlines[rid] = started + max(0, 45 - run.get("retrieval_seconds", 0))
        self._check(rid, cancelled)
        if run["status"] in {"NOT_CONFIGURED", "DISABLED"}:
            return self.context(rid)
        if not isinstance(queries, list) or not 1 <= len(queries) <= 3:
            raise DomainError("每轮只允许有限的公开查询词")
        queries = list(dict.fromkeys(self._query(query) for query in queries))
        provider = run["provider"]
        settings = self._value()
        info = provider_info(provider)
        # Validate every query before dispatching any paid request.
        for query in queries:
            build_search(provider, query, settings)
        if run["rounds"] >= 2 or run["searches"] + len(queries) > 3:
            raise ResearchLimit()
        with self._atomic():
            self._check(rid, cancelled)
            run = self._run(rid)
            run.update(rounds=run["rounds"] + 1, phase="SEARCHING")
            self.store.update(run)
        candidates = []
        known = {source["url"] for source in run["sources"]} | set(run.get("requested_urls", []))
        try:
            for query in queries:
                self._check(rid, cancelled)
                query_hash = digest(query)
                with self._atomic():
                    self._check(rid, cancelled)
                    run = self._run(rid)
                    if query_hash not in run["query_hashes"]:
                        run["query_hashes"].append(query_hash)
                    run["searches"] += 1
                    self.store.update(run)
                cached = self._cache(run, query_hash)
                if cached:
                    with self._atomic():
                        self._check(rid, cancelled)
                        current = self._run(rid)
                        current["sources"] = _bounded_sources([*current["sources"],
                            *({**source, "cache_hit": True} for source in cached)])
                        known.update(source["url"] for source in current["sources"])
                        self.store.update(current)
                    continue
                response = self._request("search", {"query": query}, rid=rid,
                    provider=provider, settings=settings, cancelled=cancelled, on_request=on_request)
                for item in normalise_search(provider, response)[:10]:
                    self._check(rid, cancelled)
                    try:
                        url = public_url(item["url"], self.resolver,
                            timeout=min(3, max(0.01, self._deadlines[rid] - self.clock())))
                    except DomainError:
                        continue
                    if url in known or any(candidate["url"] == url for candidate in candidates):
                        continue
                    secret = self._key(settings)
                    if secret and secret in unquote(url):
                        continue
                    host = urlsplit(url).hostname or ""
                    candidates.append({**item, "url": url, "title": self._safe_text(item["title"] or host, 240),
                        "published_at": self._safe_text(item["published_at"], 80) if item["published_at"] else None,
                        "institution": host, "publisher": self._safe_text(item.get("publisher") or host, 240),
                        "authority": "OFFICIAL_DOMAIN" if host.endswith((".gov.cn", ".edu.cn", ".gov", ".edu")) else "PUBLIC_SOURCE"})
            self._check(rid, cancelled)
            self.stage(rid, "VERIFYING")
            run = self._run(rid)
            remaining = max(0, 5 - len(set(run.get("requested_urls", [])) | {s["url"] for s in run["sources"]}))
            candidates.sort(key=lambda candidate: candidate["authority"] != "OFFICIAL_DOMAIN")
            for candidate in candidates[:remaining]:
                self._check(rid, cancelled)
                with self._atomic():
                    self._check(rid, cancelled)
                    current = self._run(rid)
                    current.setdefault("requested_urls", []).append(candidate["url"])
                    current["pages"] = len(set(current["requested_urls"]))
                    self.store.update(current)
                raw = candidate.get("raw_content", "")
                kind = "PAGE_CONTENT"
                if not raw.strip() and info["has_fetch"]:
                    try:
                        response = self._request("fetch", {"url": candidate["url"]}, rid=rid,
                            provider=provider, settings=settings, cancelled=cancelled, on_request=on_request)
                        page = normalise_fetch(provider, response, candidate["url"])
                        final_url = public_url(page["url"], self.resolver,
                            timeout=min(3, max(0.01, self._deadlines[rid] - self.clock())))
                        if final_url == candidate["url"]:
                            raw = page["raw_content"]
                    except DomainError as exc:
                        if getattr(exc, "reason", None) or exc.status == 409 or self.stale(rid):
                            raise
                        # Access-denied or unreadable public pages are evidence
                        # gaps. Never replace their body with an AI summary.
                        if self._deadlines[rid] <= self.clock():
                            raise
                elif not raw.strip() and not info["has_fetch"]:
                    raw, kind = candidate.get("snippet", ""), "SEARCH_SNIPPET"
                self._check(rid, cancelled)
                excerpt = _excerpt(self._safe_text(raw, _MAX_BODY))
                with self._atomic():
                    self._check(rid, cancelled)
                    current = self._run(rid)
                    if excerpt:
                        years = sorted(set(re.findall(r"(?<!\d)20\d{2}(?!\d)", excerpt)))
                        source = {key: value for key, value in candidate.items()
                                  if key not in {"raw_content", "snippet", "evidence_kind"}}
                        source.update(id="web_source_" + digest([rid, candidate["url"], excerpt])[:24],
                            retrieved_at=now(), applicable_years=years,
                            applicable_year="、".join(years) if years else None, excerpt=excerpt,
                            content_hash=digest(excerpt), cache_hit=False, provider=provider,
                            evidence_kind=kind, verified_body=kind == "PAGE_CONTENT")
                        current["sources"] = _bounded_sources([*current["sources"], source])
                    else:
                        current["unread_sources"].append({"url": candidate["url"], "title": candidate["title"],
                                                          "reason": "资料未读取"})
                    self.store.update(current)
            with self._atomic():
                self._check(rid, cancelled)
                run = self._run(rid)
                body_read = any(source.get("verified_body", True) for source in run["sources"])
                run.update(status="READY" if body_read else "SNIPPETS_ONLY" if run["sources"] else "NO_RESULTS",
                    notice="检索内容是网页陈述，仍需核对年份、适用范围和相互冲突之处" if body_read else
                    "当前只有搜索摘要，不能据此给出录取分数等关键数字；可改用支持网页正文读取的来源" if run["sources"] else
                    "没有读取到能支持结论的公开正文；不能根据搜索标题补造事实")
                self.store.update(run)
        except DomainError as exc:
            if getattr(exc, "reason", None) or exc.status == 409:
                raise
            with self._atomic():
                if self.stale(rid) or cancelled and cancelled():
                    raise DomainError("本次查询已取消", 409) from None
                run = self._run(rid)
                run.update(status="FAILED", phase="FAILED", notice=str(exc))
                self.store.update(run)
        finally:
            with self._atomic():
                run = self._run(rid)
                run["retrieval_seconds"] = run.get("retrieval_seconds", 0) + max(0, self.clock() - started)
                self.store.update(run)
        return self.context(rid)

    def inherit_published(self, rid, research_ids):
        """Only posted, still-current evidence can cross the two actors' boundary."""
        if not isinstance(research_ids, list):
            raise DomainError("来源引用格式无效")
        # Parent validity, excerpt selection and the child publication must be
        # one deletion-serialized operation. Reusing an earlier snapshot could
        # otherwise persist text which has just been withdrawn.
        with self._atomic():
            if self.stale(rid):
                return self.context(rid)
            run = self._run(rid)
            current = datetime.now(timezone.utc)
            inherited, parents = [], []
            for old_id in list(dict.fromkeys(research_ids))[:12]:
                old = self.store.get(old_id, "research_run")
                if (not old or old["id"] == rid or old["scope_id"] != run["scope_id"]
                        or old.get("phase") == "INVALIDATED" or not self._published(old)):
                    continue
                for source in old.get("sources", []):
                    try:
                        age = (current - datetime.fromisoformat(source["retrieved_at"])).total_seconds()
                    except (ValueError, KeyError):
                        continue
                    if 0 <= age <= 900:
                        inherited.append({**source, "cache_hit": True})
                        parents.append(old["id"])
            if inherited:
                run["sources"] = _bounded_sources([*run["sources"], *inherited])
                body_read = any(source.get("verified_body", True) for source in run["sources"])
                run.update(status="READY" if body_read else "SNIPPETS_ONLY",
                           notice="仅复用本场已发布且15分钟内查询的资料；仍需核对适用范围" if body_read else
                           "仅复用本场搜索摘要，不能据此给出录取分数等关键数字")
                self.store.update(run)
                for parent in set(parents):
                    self.store.link(parent, rid)
        return self.context(rid)

    def _request(self, endpoint, payload, *, rid=None, key=None, purpose=None,
                 cancelled=None, on_request=None, provider=None, settings=None):
        if endpoint not in {"search", "extract", "fetch"}:
            raise DomainError("未授权的联网动作")
        settings = self._value() if settings is None else settings
        provider = provider or (self._run(rid).get("provider", "tavily") if rid else settings.get("provider", "tavily"))
        provider_info(provider)
        if provider == "tavily" and endpoint in {"search", "extract"}:
            url, wire_payload, extra_headers = "https://api.tavily.com/" + endpoint, payload, {}
        elif endpoint == "search":
            url, wire_payload, extra_headers = build_search(provider, payload.get("query"), settings)
        elif endpoint == "fetch":
            url, wire_payload, extra_headers = build_fetch(provider, payload.get("url"), settings)
        else:
            raise DomainError("该联网服务不支持这项读取操作")
        if rid:
            self._check(rid, cancelled)
        elif cancelled and cancelled():
            raise DomainError("本次查询已取消", 409)
        while not _WEB_SLOTS.acquire(timeout=0.1):
            if rid:
                self._check(rid, cancelled)
            elif cancelled and cancelled():
                raise DomainError("本次查询已取消", 409)
        call_id = uid("research_call")
        call = None
        reservation_settler = None
        dispatched = False
        try:
            if rid:
                self._check(rid, cancelled)
            secret = key or self._key(settings)
            if not secret:
                raise DomainError("尚未配置联网查询，请先添加搜索 Key", 409)
            with self._atomic():
                call = self.store.add("research_call", {"research_id": rid,
                    "scope_id": self._run(rid)["scope_id"] if rid else None,
                    "purpose": purpose or endpoint.upper(), "state": "RESERVED",
                    "provider": provider, "connection_id": settings.get("connection_id"),
                    "request_hash": digest([provider, wire_payload]), "credits": None, "fee": None,
                    "at": now()}, rid=call_id)
            if on_request:
                reservation_settler = on_request(call_id)
            deadline = max(0.1, self._deadlines[rid] - self.clock()) if rid else 20
            if rid:
                self._check(rid, cancelled)
            # Reservation callbacks may themselves observe a pause. Until this
            # final check passes there is no external attempt to charge. Never
            # invoke a quota callback while holding the shared store lock.
            if callable(reservation_settler):
                reservation_settler(True)
            dispatched = True
            with self._atomic():
                call.update(state="DISPATCHED")
                self.store.update(call)
                if rid:
                    run = self._run(rid)
                    run["web_requests"] += 1
                    self.store.update(run)
            with self.client.stream("POST", url,
                                    headers={"Authorization": "Bearer " + secret,
                                             "Content-Type": "application/json", **extra_headers},
                                    json=wire_payload, timeout=min(20, deadline), follow_redirects=False) as response:
                body = bytearray()
                for block in response.iter_bytes():
                    if len(body) + len(block) > _MAX_BODY:
                        raise DomainError("联网服务返回资料过大，已停止读取", 502)
                    body.extend(block)
                    if rid and self.clock() >= self._deadlines[rid]:
                        raise DomainError("本轮查资料已超时", 504)
                try:
                    result = json.loads(body)
                except (ValueError, UnicodeError):
                    raise DomainError("联网服务未返回有效资料", 502) from None
                if not isinstance(result, dict):
                    raise DomainError("联网服务返回资料格式无效", 502)
                credits = extract_usage(provider, result)["credits"]
                with self._atomic():
                    call.update(state="COMPLETED" if response.is_success else "FAILED",
                                http_status=response.status_code,
                                credits=credits if isinstance(credits, (int, float)) and not isinstance(credits, bool) and credits >= 0 else None,
                                discarded=bool(rid and (self.stale(rid) or cancelled and cancelled())))
                    self.store.update(call)
                if not response.is_success:
                    message = {401: f"{provider_info(provider)['name']} Key 鉴权失败，请核对所选供应商与 Key 是否一致",
                               402: "联网服务账户余额不足，请检查供应商账户后重试",
                               403: "联网服务拒绝请求，请检查账号权限或网页读取权限",
                               404: "该接口或网页内容不可用，请核对供应商已开通的搜索/读取能力",
                               429: "联网请求过于频繁，请稍后重试", 432: "联网服务额度不足",
                               433: "联网服务已达到付费额度限制"}.get(response.status_code,
                               "联网服务暂时不可用，尚未核实当前事实")
                    raise DomainError(message, 502)
            if rid:
                self._check(rid, cancelled)
            return result
        except Exception as exc:
            if call:
                with self._atomic():
                    latest = self.store.get(call_id, "research_call")
                    if latest and latest["state"] in {"RESERVED", "DISPATCHED"}:
                        latest.update(state="FAILED" if dispatched else "NOT_SENT",
                                      discarded=bool(rid and self.stale(rid)))
                        self.store.update(latest)
            if not dispatched and callable(reservation_settler):
                reservation_settler(False)
            if isinstance(exc, DomainError):
                raise
            if isinstance(exc, httpx.TimeoutException):
                raise DomainError("联网查询超时，尚未核实当前事实", 504) from None
            raise DomainError("联网查询失败，请检查网络或稍后重试", 502) from None
        finally:
            _WEB_SLOTS.release()

    def context(self, rid):
        run = self._run(rid)
        if self.stale(rid):
            return {"research_id": rid, "status": "CANCELLED", "phase": "CANCELLED",
                    "sources": [], "unread_sources": [], "notice": "本轮查询已取消"}
        return {key: run.get(key) for key in ("status", "phase", "sources", "unread_sources", "notice")} | {"research_id": rid}

    def _published(self, run):
        valid = []
        for publication in run.get("publications", []):
            mid = publication["message_id"]
            if run["scope_kind"] == "CHAT":
                row = self.store.fetchone("SELECT conversation_id,deleted,speaker FROM messages WHERE id=?", (mid,))
                if row and row["conversation_id"] == run["scope_id"] and row["speaker"] == "AGENT" and not row["deleted"]:
                    valid.append(publication)
            else:
                turn = self.store.get(mid, "virtual_turn")
                if turn and turn.get("session_id") == run["scope_id"] and not turn.get("deleted"):
                    valid.append(publication)
        return valid

    def publish(self, rid, message_id, part_index, text):
        if not rid:
            return {}
        with self._atomic():
            run = self._run(rid)
            if self.stale(rid):
                return {}
            if not any(item["message_id"] == message_id for item in run["publications"]):
                run["publications"].append({"message_id": message_id, "part_index": part_index})
                self.store.update(run)
            return {"research_id": rid, "source_refs": [s["id"] for s in run["sources"]]}

    def sources(self, rid):
        run = self._run(rid)
        if run.get("phase") == "INVALIDATED" or not self._published(run):
            raise DomainError("已发布的来源记录不存在或已撤回", 404)
        # Existing public evidence remains readable after rotating/removing a
        # credential. It is historical evidence, not a new network request.
        return {"research_id": rid, "sources": run.get("sources", []),
                "unread_sources": run.get("unread_sources", []), "status": run["status"],
                "notice": run.get("notice", "")}

    def progress(self, scope_id):
        runs = [r for r in self.store.all("research_run") if r["scope_id"] == scope_id
                and r.get("phase") != "INVALIDATED"]
        if not runs:
            return None
        run = runs[-1]
        return {"research_id": run["id"], "phase": run["phase"], "status": run["status"],
                "notice": run.get("notice", ""), "source_message_ids": run.get("source_message_ids", []),
                "generation_id": run.get("generation_id")}

    def invalidate_scope(self, scope_id, reason, *, pending_only=False):
        with self._atomic():
            invalidated = []
            for run in self.store.all("research_run"):
                if run["scope_id"] == scope_id:
                    if pending_only:
                        actor = self.store.get(run.get("agent_id", ""), "agent_instance")
                        pending = bool(actor and actor.get("generation_id") == run.get("generation_id")
                                       and actor.get("pending_messages"))
                        if run.get("phase") not in _ACTIVE and run.get("publications") and not pending:
                            continue
                        run.update(phase="CANCELLED", notice="本轮未发布内容已取消")
                    else:
                        run.update(phase="INVALIDATED", status="INVALIDATED", sources=[], unread_sources=[],
                                   publications=[], query_hashes=[], requested_urls=[], notice="依赖资料已撤回")
                        invalidated.append(run["id"])
                    self.store.update(run)
            if invalidated:
                self._clear_metadata(invalidated)

    def _clear_metadata(self, run_ids):
        """Remove stale source affordances as part of the deletion transaction."""
        columns = {row["name"] for row in self.store.fetchall("PRAGMA table_info(messages)")}
        if {"research_id", "source_refs"} <= columns:
            for rid in run_ids:
                self.store.db.execute("UPDATE messages SET research_id=NULL,source_refs='[]' WHERE research_id=?", (rid,))
        for turn in self.store.all("virtual_turn"):
            if turn.get("research_id") in run_ids:
                turn.update(research_id=None, source_refs=[])
                self.store.update(turn)

    def invalidate(self, parent, reason):
        row = self.store.fetchone("SELECT id,conversation_id FROM messages WHERE evidence_id=?", (parent,))
        message_id = row["id"] if row else None
        with self._atomic():
            affected = {parent}
            # Include version and inherited-research lineage, even if the
            # original human message is no longer in the latest12 context.
            pending = [parent]
            while pending:
                current = pending.pop()
                for child in self.store.fetchall("SELECT child_id FROM dependencies WHERE parent_id=?", (current,)):
                    if child["child_id"] not in affected:
                        affected.add(child["child_id"])
                        pending.append(child["child_id"])
            invalidated = []
            for run in self.store.all("research_run"):
                if (run["id"] in affected or run.get("version_id") in affected or parent == run.get("agent_id")
                        or parent in run.get("source_message_ids", [])
                        or message_id and message_id in run.get("source_message_ids", [])
                        or row and row["conversation_id"] == run["scope_id"]):
                    run.update(phase="INVALIDATED", status="INVALIDATED", sources=[], unread_sources=[],
                               publications=[], query_hashes=[], requested_urls=[], notice="依赖资料已撤回")
                    self.store.update(run)
                    invalidated.append(run["id"])
            self._clear_metadata(invalidated)

    def close(self):
        self._session_keys.clear()
        if self._owns_client:
            self.client.close()
