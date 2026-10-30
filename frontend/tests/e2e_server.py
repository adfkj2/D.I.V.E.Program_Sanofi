"""Free browser-test server. Use only an isolated directory named e2e."""
import os
import time
from pathlib import Path

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
        if "[e2e_wait]" in topic:
            time.sleep(1.2)
        return super().generate(**kwargs)


if __name__ == "__main__":
    directory = Path(os.environ["WOT_DATA_DIR"])
    if "e2e" not in str(directory).lower():
        raise RuntimeError("Use an explicitly named, isolated e2e directory")
    uvicorn.run(create_app(directory, BrowserTestProvider()), host="127.0.0.1", port=8765, workers=1)
