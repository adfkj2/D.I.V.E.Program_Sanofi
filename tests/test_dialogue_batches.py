"""Transport envelopes must never become visible character dialogue."""

import json

import pytest
from wot.dialogue import DIALOGUE_BATCH_SYSTEM, parse_dialogue_batch
from wot.domain import DomainError
from wot.model import Provider


def envelope(parts, followup=None, **kwargs):
    return json.dumps({"messages": parts, "idle_followup": followup, **kwargs}, ensure_ascii=False)


def test_parts_preserve_intent_and_prepare_each_part_once():
    raw = envelope([
        "哎，**等一下**。\n\n我刚才漏说了一点。",
        "```python\n# comment\nx_y = 2 ** 3\n```",
        "这个数是怎么算出来的？",
    ])
    result = parse_dialogue_batch(raw)
    assert result == {"messages": [
        "哎，等一下。\n\n我刚才漏说了一点。", "# comment\nx_y = 2 ** 3", "这个数是怎么算出来的？",
    ], "idle_followup": None}


def test_plain_legacy_content_does_not_split_paragraphs_sentences_or_questions():
    raw = "好。\n\n等等，我还有一句。你刚才说的是哪一件事？"
    assert parse_dialogue_batch(raw) == {"messages": [raw], "idle_followup": None}


def test_json_code_from_legacy_reply_preserves_content():
    raw = '{"a_b": "**literal**", "count": 2}'
    assert parse_dialogue_batch(raw)["messages"] == [raw]


def test_complete_json_fence_can_be_recognized_without_leaking_protocol():
    raw = "```json\n" + envelope(["听着。", "我的意思是，先别急。"], "还有一点，我想起了刚才那个例子。") + "\n```"
    result = parse_dialogue_batch(raw)
    assert result["messages"] == ["听着。", "我的意思是，先别急。"]
    assert result["idle_followup"] == "还有一点，我想起了刚才那个例子。"


@pytest.mark.parametrize("tail", ["你后来怎么选的？", "怎么回事?", "真这样？’", '你说呢？"'])
def test_unanswered_question_suppresses_idle_followup(tail):
    result = parse_dialogue_batch(envelope([tail], "怎么不说话？"))
    assert result["idle_followup"] is None


def test_explicit_wait_and_virtual_suppress_followup():
    assert parse_dialogue_batch(envelope(["我想听你说下去。"], "还有一点。", expects_reply=True))["idle_followup"] is None
    assert parse_dialogue_batch(envelope(["这事有意思。"], "再补一句。"), allow_followup=False)["idle_followup"] is None


@pytest.mark.parametrize("raw", [
    '{"messages":["未完成',
    '{"messages": [], "idle_followup": null}',
    '{"messages": "不是数组"}',
    '{"idle_followup": "缺少正文"}',
    '{"messages":[null]}',
    '{"messages":[42]}',
    '{"messages":[""]}',
    '{"messages":["---"]}',
    '{"messages":["话"], "idle_followup": true}',
    '{"messages":["话"], "expects_reply": "yes"}',
    '{"messages":["话"], "tool": "run"}',
    '["这不是协议"]',
    '好的：{"messages":["泄露协议"]}',
    '{"messages": ["一", "二", "三", "四", "五"]}',
    '{"messages": ["一", "二", "三", "四"], "idle_followup":"第五条"}',
    " ", None,
])
def test_invalid_envelopes_fail_without_exposing_raw_text(raw):
    with pytest.raises(DomainError, match="模型"):
        parse_dialogue_batch(raw)


def test_bounds_reject_content_instead_of_silently_cutting_meaning():
    with pytest.raises(DomainError):
        parse_dialogue_batch(envelope(["a" * 12_001]))
    with pytest.raises(DomainError):
        parse_dialogue_batch(envelope(["一", "二"]), max_messages=1)


def test_demo_provider_uses_same_transport_without_live_calls():
    provider = Provider()
    provider.name = "mock"
    result = provider.generate(role="EXTERNAL", topic="你好", transcript=[], private=[],
                               skill={"slug": "munger"}, goals=[], dialogue_batch=True)
    assert len(parse_dialogue_batch(result["text"])["messages"]) == 1
    assert provider.external_calls == 0


def test_cancelled_demo_does_not_generate():
    provider = Provider()
    with pytest.raises(DomainError, match="取消"):
        provider.generate(role="EXTERNAL", topic="你好", transcript=[], private=[],
                          skill={}, goals=[], dialogue_batch=True, cancelled=lambda: True)
    assert provider.calls == []


def test_transport_prompt_does_not_require_mechanical_splitting_or_questions():
    assert "不把每个句子" in DIALOGUE_BATCH_SYSTEM
    assert "不必每轮提问" in DIALOGUE_BATCH_SYSTEM
    assert "通常为null" in DIALOGUE_BATCH_SYSTEM
