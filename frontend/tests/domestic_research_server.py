"""Isolated browser fixtures use real routes and mocked domestic providers only."""
import os
from pathlib import Path

import httpx
import uvicorn
from e2e_server import browser_app
from fastapi import Body

MODEL_KEY = "sk-domestic-model-fixture-not-a-real-key"


def domestic_app(directory):
    app = browser_app(directory)
    upgrade = app.state.upgrade
    upgrade.people.preferences({"setup_dismissed": True})
    upgrade.connections.save({"provider": "deepseek", "model": "deepseek-fixture-only",
                              "api_key": MODEL_KEY, "remember": False,
                              "label": "DeepSeek 本机测试连接"})
    manager = upgrade.research
    manager.client.close()
    app.state.domestic_mode = "AUTH_FAILED"
    app.state.domestic_calls = []

    def respond(request):
        app.state.domestic_calls.append({"host": request.url.host, "path": request.url.path})
        if app.state.domestic_mode == "AUTH_FAILED":
            return httpx.Response(401, json={"error": request.headers.get("authorization")})
        if request.url.host == "api.deepseek.com":
            assert request.headers["authorization"] == "Bearer " + MODEL_KEY
            return httpx.Response(200, json={"results": [{"title": "界面测试公开资料", "url":
                "https://example.edu.cn/plan", "content": "2026年公开资料示例，不是真实招生依据。"}]})
        if request.url.host == "api.tavily.com":
            return httpx.Response(200, json={"results": [], "usage": {"credits": 1}})
        raise AssertionError("Unexpected fixture provider")

    manager.client = httpx.Client(transport=httpx.MockTransport(respond))
    manager._owns_client = True

    @app.post("/api/v1/e2e/domestic-mode")
    def mode(data: dict = Body(...)):
        assert data.get("mode") in {"AUTH_FAILED", "SUCCESS"}
        app.state.domestic_mode = data["mode"]
        return {"test_server": True}

    @app.post("/api/v1/e2e/domestic-count")
    def count():
        return {"mock_search_calls": len(app.state.domestic_calls),
                "providers_called": sorted({item["host"] for item in app.state.domestic_calls}),
                "real_search_calls": 0, "real_model_calls": 0}

    return app


if __name__ == "__main__":
    directory = Path(os.environ["WOT_DATA_DIR"])
    if "e2e" not in str(directory).lower():
        raise RuntimeError("Use an explicitly isolated e2e directory")
    uvicorn.run(domestic_app(directory), host="127.0.0.1",
                port=int(os.getenv("WOT_TEST_PORT", "8769")), workers=1)
