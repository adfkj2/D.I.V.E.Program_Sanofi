"""Real loopback HTTP contract: tools, limits, errors, redirect credential boundary."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from dive_memory.agent.protocols import ModelError
from dive_memory.agent.transport import CompatibleModel


@pytest.fixture
def server():
    captured = []
    reply = {
        "status": 200,
        "body": {
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "one",
                                "type": "function",
                                "function": {
                                    "name": "respond",
                                    "arguments": '{"message":"hello","citations":[]}',
                                },
                            }
                        ],
                    },
                }
            ]
        },
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            captured.append(
                {
                    "path": self.path,
                    "key": self.headers.get("Authorization"),
                    "body": json.loads(
                        self.rfile.read(int(self.headers["Content-Length"]))
                    ),
                }
            )
            self.send_response(reply["status"])
            if reply["status"] == 302:
                self.send_header("Location", "/stolen")
            self.end_headers()
            raw = reply["body"]
            self.wfile.write(
                raw if isinstance(raw, bytes) else json.dumps(raw).encode()
            )

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=http.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{http.server_port}", reply, captured
    http.shutdown()
    http.server_close()
    thread.join()


def test_wire_protocol(server):
    endpoint, _, captured = server
    model = CompatibleModel(endpoint, "deepseek-chat", "contract-key")
    answer = model.complete([{"role": "user", "content": "hi"}], [{"type": "function"}])
    assert answer["tool_calls"][0]["function"]["name"] == "respond"
    assert captured[0]["path"] == "/chat/completions"
    assert captured[0]["key"] == "Bearer contract-key"
    assert captured[0]["body"]["tool_choice"] == "required"
    assert captured[0]["body"]["max_tokens"] == 3000
    CompatibleModel(endpoint + "/chat/completions", "x", "k").complete([], [])
    assert captured[1]["path"] == "/chat/completions"


@pytest.mark.parametrize(
    "status,part",
    [(401, "认证"), (403, "认证"), (429, "额度"), (500, "HTTP 500"), (302, "HTTP 302")],
)
def test_http_errors_do_not_leak_or_retry(server, status, part):
    endpoint, reply, captured = server
    reply.update(status=status, body=b"secret-provider-body")
    with pytest.raises(ModelError, match=part) as exc:
        CompatibleModel(endpoint, "x", "secret-key").complete([], [])
    assert "secret" not in str(exc.value)
    assert len(captured) == 1


@pytest.mark.parametrize(
    "body,part",
    [
        (b"not-json", "格式"),
        ({"choices": []}, "格式"),
        ({"choices": [{"finish_reason": "length", "message": {}}]}, "未完整"),
        ({"choices": [{"finish_reason": "stop", "message": "text"}]}, "格式"),
        pytest.param(b"x" * 1_000_001, "过大", id="oversized-response"),
    ],
)
def test_bad_responses_are_bounded(server, body, part):
    endpoint, reply, _ = server
    reply["body"] = body
    with pytest.raises(ModelError, match=part):
        CompatibleModel(endpoint, "x", "k").complete([], [])


def test_validation_and_network_failure(monkeypatch):
    with pytest.raises(ValueError, match="API Key"):
        CompatibleModel("https://api.deepseek.com", "x", "")
    model = CompatibleModel("https://api.deepseek.com", "x", "k")
    with pytest.raises(ModelError, match="上下文过长"):
        model.complete([{"role": "user", "content": "x" * 200_001}], [])

    class Timeout:
        def open(self, *args, **kwargs):
            raise TimeoutError("secret")

    monkeypatch.setattr(
        "dive_memory.agent.transport.build_opener", lambda *args: Timeout()
    )
    with pytest.raises(ModelError, match="超时"):
        model.complete([], [])
