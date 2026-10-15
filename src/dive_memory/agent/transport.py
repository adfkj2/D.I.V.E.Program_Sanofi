"""Bounded OpenAI-compatible tool calling, independent of Oracle transports."""

import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .protocols import ModelError


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class CompatibleModel:
    def __init__(self, endpoint: str, model: str, key: str):
        endpoint = endpoint.strip().rstrip("/")
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or (
                parsed.scheme == "http"
                and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            )
        ):
            raise ValueError(
                "模型地址须为 HTTPS，或本机 HTTP 地址；不能包含凭据或查询参数。"
            )
        if not model.strip() or len(model) > 200 or not key.strip() or len(key) > 1000:
            raise ValueError("请填写模型名称与 API Key。")
        self.endpoint = endpoint
        self.model = model.strip()
        self._key = key.strip()

    def complete(self, messages: list[dict], tools: list[dict]) -> dict:
        body = json.dumps(
            {
                "model": self.model,
                "messages": messages,
                "tools": tools,
                "tool_choice": "required",
                "temperature": 0.2,
                "max_tokens": 3000,
            },
            ensure_ascii=False,
        ).encode()
        if len(body) > 200_000:
            raise ModelError("本次上下文过长，请缩小问题范围后重试。")
        url = (
            self.endpoint
            if self.endpoint.endswith("/chat/completions")
            else self.endpoint + "/chat/completions"
        )
        request = Request(
            url,
            data=body,
            headers={
                "Authorization": "Bearer " + self._key,
                "Content-Type": "application/json",
            },
        )
        try:
            with build_opener(NoRedirect).open(request, timeout=45) as response:
                raw = response.read(1_000_001)
            if len(raw) > 1_000_000:
                raise ModelError("模型响应过大，本次操作已停止。")
            result = json.loads(raw)
            choice = result["choices"][0]
            if choice.get("finish_reason") not in {"stop", "tool_calls"}:
                raise ModelError("模型响应未完整结束，请缩小请求后重试。")
            message = choice["message"]
            if not isinstance(message, dict):
                raise ValueError("invalid message")
            return message
        except HTTPError as exc:
            if exc.code in {401, 403}:
                raise ModelError("模型认证失败，请检查 API Key 和权限。") from None
            if exc.code == 429:
                raise ModelError("模型服务额度不足或请求过快，请稍后重试。") from None
            raise ModelError(
                f"模型服务返回 HTTP {exc.code}，请检查地址、模型及工具调用支持。"
            ) from None
        except (TimeoutError, URLError, OSError):
            raise ModelError(
                "模型连接失败或超时，请检查网络和服务地址后重试。"
            ) from None
        except (KeyError, IndexError, TypeError, ValueError):
            raise ModelError("模型返回格式无效，请使用支持工具调用的模型。") from None
