"""Free browser-test server. Use only an isolated directory named e2e."""
import json
import os
import time
from pathlib import Path

import httpx
import uvicorn
from wot.domain import DomainError
from wot.main import create_app
from wot.model import Provider


class BrowserTestProvider(Provider):
    def __init__(self):
        super().__init__()
        self.test_server = True
        if self.name != "mock":
            raise RuntimeError("Browser tests require WOT_PROVIDER=mock")
        self.failed_topics = set()

    def generate(self, **kwargs):
        topic = kwargs.get("topic", "")
        if "[e2e_fail_once]" in topic and topic not in self.failed_topics:
            self.failed_topics.add(topic)
            raise DomainError("离线测试：用户消息已保存，本次回复暂时失败，请手动重试。", 502)
        if "[e2e_revised]" in topic:
            text = json.dumps({"messages": ["好，那就先说你补充的这一点。", "这件事最让你在意的是什么？"], "idle_followup": None}, ensure_ascii=False)
            return {"text": text, "tokens": 90, "cost": 0, "provider": "mock"}
        if "[e2e_interrupt]" in topic:
            text = json.dumps({"messages": ["这句已经发出了。", "这句应该被插话取消。", "这句也不应出现。"], "idle_followup": None}, ensure_ascii=False)
            return {"text": text, "tokens": 90, "cost": 0, "provider": "mock"}
        if "[e2e_burst]" in topic:
            time.sleep(2)
            text = json.dumps({"messages": ["先说一句，今天这事还挺有意思。", "刚才提到的那个细节，我倒是想多听听。", "你当时为什么会这么想？"], "idle_followup": None}, ensure_ascii=False)
            return {"text": text, "tokens": 110, "cost": 0, "provider": "mock"}
        if "[e2e_markdown]" in topic:
            text = (
                "## 先把问题说清楚\n\n**别急着下结论。**\n"
                "- 看清代价\n- 保留退路\n\n> 先验证，再决定。\n\n"
                "| 项目 | 数值 |\n| --- | --- |\n| 试验次数 | 3 次 |\n\n"
                "[原始资料](https://example.com/source)\n\n"
                "```python\n# keep this comment\nvalue_name = 2 * 3 ** 2\n```\n\n"
                "公式：$E = m * c^2$"
            )
            return {"text": text, "tokens": len(text), "cost": 0, "provider": "mock"}
        if "[e2e_wait]" in topic:
            time.sleep(1.2)
        return super().generate(**kwargs)


def browser_app(directory):
    """Only this isolated test entrypoint exposes a synthetic LIVE transport.

    No network or real secret is involved, and no personal admission is granted.
    Production create_app never registers this fixture endpoint.
    """
    provider = BrowserTestProvider()
    app = create_app(directory, provider)
    app.state.fixture_transport_calls = 0

    @app.post("/api/v1/e2e/virtual-transport")
    def configure_virtual_fixture():
        manager = app.state.upgrade.connections

        def respond(request):
            app.state.fixture_transport_calls += 1
            number = app.state.fixture_transport_calls
            text = json.dumps({"messages": [
                f"隔离测试第{number}轮：这是完整的一条回应。",
                "刚才那个具体的细节，我愿意继续听。",
            ], "idle_followup": None}, ensure_ascii=False)
            return httpx.Response(200, json={
                "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 80, "completion_tokens": 60, "total_tokens": 140},
            })

        manager.client = httpx.Client(transport=httpx.MockTransport(respond))
        manager._owns_client = True
        connection = manager.save({"provider": "custom", "protocol": "chat",
                                   "model": "in-process-e2e-model", "api_key": "isolated-fixture-key",
                                   "base_url": "https://e2e.invalid/v1", "remember": False})
        virtual = app.state.upgrade.virtual
        virtual.interval = (0.15, 0.15)
        virtual._part_delay = lambda text: 0.08
        return {"connection": connection, "test_server": True}

    @app.post("/api/v1/e2e/transport-count")
    def fixture_transport_count():
        return {"mock_transport_calls": app.state.fixture_transport_calls,
                "real_model_calls": 0}

    return app


if __name__ == "__main__":
    directory = Path(os.environ["WOT_DATA_DIR"])
    if "e2e" not in str(directory).lower():
        raise RuntimeError("Use an explicitly named, isolated e2e directory")
    uvicorn.run(browser_app(directory), host="127.0.0.1", port=int(os.getenv("WOT_TEST_PORT", "8765")), workers=1)
