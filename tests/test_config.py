import pytest

from dive_memory.api import create_app
from dive_memory.auth import StaticTokenAuthorizer
from dive_memory.config import RuntimeConfig


class SemanticProvider:
    model = "semantic-test"
    revision = "r1"
    dimensions = 2
    semantic_similarity = True

    def embed(self, text):
        return [1.0, 0.0]


def test_runtime_config_is_versioned_hashable_and_rejects_unknown_mode():
    config = RuntimeConfig.from_mapping({"mode": "development", "trace_enabled": "true"})
    assert config.version == "runtime-config-v1"
    assert len(config.sha256()) == 64
    with pytest.raises(ValueError, match="mode"):
        RuntimeConfig.from_mapping({"mode": "demo-ish"})


def test_production_api_requires_auth_and_semantic_embedding():
    production = RuntimeConfig(mode="production")
    with pytest.raises(RuntimeError, match="authorizer"):
        create_app(":memory:", runtime_config=production)

    auth = StaticTokenAuthorizer({"token": {"u1"}})
    with pytest.raises(RuntimeError, match="semantic"):
        create_app(":memory:", authorizer=auth, runtime_config=production)

    app = create_app(":memory:", authorizer=auth, embedder=SemanticProvider(), runtime_config=production)
    assert app.state.runtime_config.mode == "production"


def test_sensitive_trace_payloads_are_disabled_by_default_even_in_development():
    assert not RuntimeConfig().allow_sensitive_trace_payloads
