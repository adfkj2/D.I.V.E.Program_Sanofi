"""Actor isolation, completion recovery and usage are tested without live calls."""
import json

import pytest
from wot.agents import AgentIncomplete, AgentLimit
from wot.domain import DomainError
from wot.service import WotService


def done(text="这件事我说完了。", **extra):
    return {"text": json.dumps({"messages": [text], "idle_followup": None}, ensure_ascii=False),
            "tokens": 100, "finish_reason": "stop", **extra}


class Transport:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def complete(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def service(tmp_path):
    svc = WotService(tmp_path / "agent-tests")
    yield svc
    svc.close()


def run(service, transport, **kwargs):
    base = dict(scope_kind="VIRTUAL", scope_id="room", participant_id="munger", version_id="v1",
                personality="仅使用此人物资料。", transcript=[], topic="聊聊今天", transport=transport,
                allow_followup=False)
    return service.agents.respond(**(base | kwargs))


def test_length_and_empty_reasoning_retry_full_group_and_charge_each_call(service):
    transport = Transport([
        {"text": '{"messages":["未完成', "tokens": 201, "finish_reason": "length"},
        {"text": "", "tokens": 401, "finish_reason": "MAX_TOKENS"}, done(),
    ])
    charged = []
    result = run(service, transport, on_usage=lambda r, cid: charged.append((r["tokens"], cid)))
    assert [c["max_tokens"] for c in transport.calls] == [2048, 4096, 8192]
    assert result["messages"] == ["这件事我说完了。"]
    assert result["tokens"] == 702
    assert [t for t, _ in charged] == [201, 401, 100]
    assert len(service.store.all("agent_call")) == 3
    assert "未完成" not in json.dumps(transport.calls[2]["messages"], ensure_ascii=False)
    assert service.agents.public(result["agent_id"])["state"] == "READY"


@pytest.mark.parametrize("reason", ["length", "max_tokens", "max_output_tokens", "MAX_TOKENS"])
def test_unfinished_draft_remains_private_and_manual_retry_keeps_actor(service, reason):
    cut = {"text": '{"messages":["PRIVATE_INCOMPLETE_CANARY', "tokens": 10, "finish_reason": reason}
    transport = Transport([cut, cut, cut, done()])
    with pytest.raises(AgentIncomplete):
        run(service, transport)
    actor = service.store.all("agent_instance")[0]
    assert actor["checkpoint"]["kind"] == "OUTPUT_TRUNCATED"
    assert "PRIVATE_INCOMPLETE_CANARY" not in json.dumps(service.agents.public(actor["id"]))
    assert run(service, transport)["agent_id"] == actor["id"]
    assert len(service.store.all("agent_instance")) == 1


@pytest.mark.parametrize("result", [done(finish_reason="SAFETY"), done(finish_reason="refusal"),
                                     DomainError("鉴权失败", 502), done(text="", finish_reason="tool_calls")])
def test_other_endings_or_errors_do_not_automatically_retry(service, result):
    transport = Transport([result, done()])
    with pytest.raises(DomainError):
        run(service, transport)
    assert len(transport.calls) == 1


def test_budget_precheck_pauses_without_invoking_provider(service):
    transport = Transport([done()])
    with pytest.raises(AgentLimit) as error:
        run(service, transport, remaining_tokens=lambda: 5)
    assert error.value.reason == "BUDGET_PRECHECK"
    assert transport.calls == []
    assert service.store.all("agent_instance")[0]["state"] == "PAUSED_LIMIT"


def test_roles_context_sources_and_rooms_are_isolated(service):
    transcript = [{"id": f"m{i}", "speaker": "另一个人", "speaker_id": "buffett", "text": f"已经发布{i}。"}
                  for i in range(17)]
    transcript[-1] = {"id": "own", "speaker": "芒格", "speaker_id": "munger", "text": "我上次这样说。"}
    transport = Transport([done(), done(), done()])
    first = run(service, transport, transcript=transcript)
    second = run(service, transport, participant_id="buffett", transcript=transcript)
    third = run(service, transport, scope_id="another-room", transcript=[])
    assert len({r["agent_id"] for r in (first, second, third)}) == 3
    assert "我上次这样说。" in [m["content"] for m in transport.calls[0]["messages"] if m["role"] == "assistant"]
    assert not any(m["role"] == "assistant" and m["content"] == "我上次这样说。"
                   for m in transport.calls[1]["messages"])
    assert "已经发布" not in json.dumps(transport.calls[2]["messages"], ensure_ascii=False)
    actor = service.require(first["agent_id"], "agent_instance")
    assert actor["summary"][0]["source_id"] == "m0"
    assert actor["context_cursor"] == "own"
    assert len(service.store.all("agent_call")) == 3  # extractive summary is local


def test_published_ack_and_withdrawal_clear_private_actor_state(service):
    result = run(service, Transport([done()]))
    with service.store.transaction():
        service.agents.acknowledge(result["agent_id"], 0, result["generation_id"])
    assert service.require(result["agent_id"], "agent_instance")["state"] == "IDLE"
    run(service, Transport([done("待发隐私。")]))
    service.agents.invalidate_scope("room", "来源撤回")
    actor = service.require(result["agent_id"], "agent_instance")
    assert actor["pending_messages"] == [] and actor["summary"] == [] and actor["checkpoint"] is None


def test_late_result_is_charged_without_publication(service):
    cancelled = [False]

    class Late(Transport):
        def complete(self, messages, **kwargs):
            cancelled[0] = True
            return super().complete(messages, **kwargs)

    charged = []
    with pytest.raises(DomainError):
        run(service, Late([done()]), cancelled=lambda: cancelled[0], on_usage=lambda r, _: charged.append(r["tokens"]))
    assert charged == [100]
    assert service.store.all("agent_call")[0]["discarded"] is True
    assert service.store.all("agent_instance")[0]["pending_messages"] == []


def test_deleted_history_cannot_be_reintroduced_before_actor_dispatch(service):
    transcript = [{"id": f"m{i}", "speaker": "other", "text": "DELETED_HISTORY_CANARY。"}
                  for i in range(20)]
    transport = Transport([done()])
    with pytest.raises(DomainError, match="取消"):
        run(service, transport, transcript=transcript, cancelled=lambda: True)
    assert transport.calls == []
    assert "DELETED_HISTORY_CANARY" not in json.dumps(service.store.all("agent_instance"))
