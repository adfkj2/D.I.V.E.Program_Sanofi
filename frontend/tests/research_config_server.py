"""Real local config routes with an in-process Tavily transport, never a real Key."""
import os
import time
from pathlib import Path

import httpx
import uvicorn
from e2e_server import browser_app
from fastapi import Body


class UnavailableVault:
    backend = None

    def get_password(self, service, key):
        return None

    def set_password(self, service, key, value):
        raise RuntimeError("Isolated fixture vault unavailable")

    def delete_password(self, service, key):
        pass


def config_app(directory):
    app = browser_app(directory)
    manager = app.state.upgrade.research
    manager.client.close()
    manager.vault = UnavailableVault()
    app.state.fixture_mode = "AUTH_FAILED"
    app.state.fixture_search_calls = 0
    app.state.upgrade.people.preferences({"setup_dismissed": True})

    def respond(request):
        app.state.fixture_search_calls += 1
        time.sleep(0.3)
        if app.state.fixture_mode == "AUTH_FAILED":
            # Provider echo must never reach durable data or visible errors.
            return httpx.Response(401, json={"detail": request.headers.get("authorization")})
        return httpx.Response(200, json={"results": [], "usage": {"credits": 1}})

    manager.client = httpx.Client(transport=httpx.MockTransport(respond))
    manager._owns_client = True

    @app.post("/api/v1/e2e/research-mode")
    def mode(data: dict = Body(...)):
        if data.get("mode") not in {"AUTH_FAILED", "SUCCESS"}:
            raise ValueError("Fixture mode invalid")
        app.state.fixture_mode = data["mode"]
        return {"test_server": True, "mode": app.state.fixture_mode}

    @app.post("/api/v1/e2e/research-count")
    def count():
        return {"mock_search_calls": app.state.fixture_search_calls,
                "real_search_calls": 0, "real_model_calls": 0}

    return app


if __name__ == "__main__":
    folder = Path(os.environ["WOT_DATA_DIR"])
    if "e2e" not in str(folder).lower():
        raise RuntimeError("Use an explicitly isolated e2e directory")
    uvicorn.run(config_app(folder), host="127.0.0.1",
                port=int(os.getenv("WOT_TEST_PORT", "8767")), workers=1)
