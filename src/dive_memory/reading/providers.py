"""Chat transports for the Oracle reader.

Transport only. The domain layer (``reader.py``) depends on the ``ChatClient``
protocol, so replacing a vendor never touches memory logic -- handbook
appendix B.3 requires exactly that decoupling. Nothing here imports a vendor
SDK: the OpenAI-compatible client speaks plain HTTP with ``urllib``.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .reader import ChatCompletion


class ChatTransportError(RuntimeError):
    """A transport-level failure: DNS, connection, TLS, HTTP status, envelope."""


@dataclass(slots=True)
class OpenAICompatibleChatClient:
    """Minimal ``POST /chat/completions`` client.

    ``endpoint`` is the full URL (e.g. ``https://api.openai.com/v1/chat/completions``
    or a local gateway). The API key is passed in, never read from the
    environment, so a run's credentials are the caller's decision.
    """

    endpoint: str
    api_key: str
    provider: str = "openai-compatible"
    extra_headers: Mapping[str, str] = field(default_factory=dict)
    extra_body: Mapping[str, Any] = field(default_factory=dict)

    def complete(
        self,
        *,
        model: str,
        messages: Sequence[Mapping[str, str]],
        temperature: float,
        max_tokens: int,
        timeout_seconds: float,
        response_format: Mapping[str, Any] | None = None,
    ) -> ChatCompletion:
        body: dict[str, Any] = {
            "model": model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "messages": [dict(message) for message in messages],
            **dict(self.extra_body),
        }
        if response_format is not None:
            body["response_format"] = dict(response_format)
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                **dict(self.extra_headers),
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:  # pragma: no cover - network path
            detail = ""
            try:
                detail = exc.read().decode("utf-8")[:500]
            except Exception:  # noqa: BLE001 - best-effort diagnostics
                detail = ""
            raise ChatTransportError(
                f"HTTP {exc.code} from {self.endpoint}: {detail}"
            ) from exc
        except TimeoutError as exc:
            # ``urllib`` surfaces a socket timeout as ``TimeoutError`` on 3.10+
            # and as ``socket.timeout`` (an alias) earlier; both are caught here
            # so the reader status can be TIMEOUT rather than ERROR.
            raise TimeoutError(f"reader request exceeded {timeout_seconds}s") from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, TimeoutError):
                raise TimeoutError(
                    f"reader request exceeded {timeout_seconds}s"
                ) from exc
            raise ChatTransportError(f"cannot reach {self.endpoint}: {reason}") from exc
        except OSError as exc:
            raise ChatTransportError(
                f"transport failure for {self.endpoint}: {exc}"
            ) from exc

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ChatTransportError("response envelope is not JSON") from exc
        try:
            choice = payload["choices"][0]
            content = choice["message"]["content"]
            usage = payload.get("usage")
            resolved = str(payload.get("model") or model)
        except (KeyError, IndexError, TypeError) as exc:
            raise ChatTransportError(
                "response envelope is missing choices/message/content"
            ) from exc
        finish_reason = choice.get("finish_reason")
        if not isinstance(finish_reason, str) or not finish_reason:
            finish_reason = None
        # A completed response may be truncated, filtered, or otherwise end
        # without content. Preserve its explicit termination and known usage;
        # a missing content field or null without a termination is still an error.
        if content is None and finish_reason is not None:
            content = ""
        if content is None:
            raise ChatTransportError("response message has no content")
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        usage = usage if isinstance(usage, dict) else {}
        details = usage.get("completion_tokens_details")
        reasoning_tokens = (
            details.get("reasoning_tokens") if isinstance(details, dict) else None
        )
        if type(reasoning_tokens) is not int or reasoning_tokens < 0:
            reasoning_tokens = None
        return ChatCompletion(
            text=content,
            model=resolved,
            revision=str(payload.get("system_fingerprint"))
            if payload.get("system_fingerprint")
            else None,
            input_tokens=_as_int(usage.get("prompt_tokens")),
            output_tokens=_as_int(usage.get("completion_tokens")),
            finish_reason=finish_reason,
            reasoning_tokens=reasoning_tokens,
        )


def _as_int(value: Any) -> int | None:
    # Usage is billing evidence. Coercing floats or strings could silently
    # undercount a response and bypass the caller's unknown-usage stop.
    return value if type(value) is int and value >= 0 else None


@dataclass(slots=True)
class ScriptedChatClient:
    """Deterministic in-memory transport for offline reader-wiring checks.

    Returns the queued responses in order. It exists so the grounded reader
    path -- prompt assembly, JSON parsing, citation validation, token
    accounting -- can be exercised end to end without a model credential, and
    so a smoke run can prove the wiring rather than assert it.
    """

    responses: list[str] = field(default_factory=list)
    provider: str = "scripted"
    model: str = "scripted-chat-v1"
    revision: str | None = "scripted-revision-1"
    input_tokens_per_call: int = 0
    output_tokens_per_call: int = 0
    prompts: list[str] = field(default_factory=list)

    def complete(
        self,
        *,
        model: str,
        messages: Sequence[Mapping[str, str]],
        temperature: float,
        max_tokens: int,
        timeout_seconds: float,
        response_format: Mapping[str, Any] | None = None,
    ) -> ChatCompletion:
        self.prompts.append(
            "\n".join(str(message.get("content", "")) for message in messages)
        )
        if not self.responses:
            raise ChatTransportError("scripted client has no queued response")
        text = self.responses.pop(0)
        return ChatCompletion(
            text=text,
            model=self.model,
            revision=self.revision,
            input_tokens=self.input_tokens_per_call,
            output_tokens=self.output_tokens_per_call,
        )
