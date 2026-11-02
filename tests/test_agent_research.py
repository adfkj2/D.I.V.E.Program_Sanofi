"""Research actions are real coordinator paths with an in-process web double."""
import json

import httpx
import pytest
from wot.agents import AgentIncomplete, AgentLimit, admission_context, parse_agent_action
from wot.domain import DomainError
from wot.research import ResearchManager
from wot.service import WotService

KEY = "tvly-isolated-research-key-123456"
URL = "https://example.edu.cn/admissions/2026"
QUESTION = "2026年山东物理本科580分，位次50000，能报考哪里？"
EXCERPT = "2026年 山东 物理 本科\n山东示例大学 专业组101 投档最低分590 最低位次30000。"


class Vault:
    def __init__(self):
        self.values = {}

    def get_password(self, namespace, ref):
        return self.values.get((namespace, ref))

    def set_password(self, namespace, ref, secret):
        self.values[namespace, ref] = secret

    def delete_password(self, namespace, ref):
        self.values.pop((namespace, ref), None)


class Model:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []
        self.after = None

    def complete(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        value = self.results.pop(0)
        if self.after:
            self.after(len(self.calls))
        if isinstance(value, Exception):
            raise value
        return value


def response(value, tokens=100, finish_reason="stop"):
    return {"text": json.dumps(value, ensure_ascii=False) if isinstance(value, dict) else value,
            "tokens": tokens, "finish_reason": finish_reason}


def search(*queries):
    return response({"action": "SEARCH", "queries": list(queries or ["2026 山东 物理 本科 投档位次"])})


def reply(text, action="REPLY"):
    return response({"action": action, "reply": {"messages": [text], "idle_followup": None}})


@pytest.fixture
def rig(tmp_path):
    service = WotService(tmp_path)
    web = []
    handler = [None]

    def dispatch(request):
        web.append((request.url.path, json.loads(request.content)))
        if handler[0]:
            return handler[0](request)
        if request.url.path == "/search":
            return httpx.Response(200, json={"results": [{"title": "2026本科投档公告", "url": URL}],
                                            "usage": {"credits": 1}})
        return httpx.Response(200, json={"results": [{"url": URL, "raw_content": EXCERPT}],
                                        "usage": {"credits": 2}})

    client = httpx.Client(transport=httpx.MockTransport(dispatch))
    research = ResearchManager(service, tmp_path, http_client=client, credential_backend=Vault(),
                               resolver=lambda host, port, **kwargs: [(None, None, None, None, ("9.9.9.9", port))])
    service.research = research
    assert research.validate({"api_key": KEY, "remember_key": False})["saved"]
    web.clear()
    yield service, research, web, handler
    research.close()
    client.close()
    service.close()


def run(rig, model, question=QUESTION, **kwargs):
    service = rig[0]
    options = dict(scope_kind="CHAT", scope_id="isolated-room", participant_id="zhangxuefeng",
                   version_id="voice-v1", personality={"name": "张雪峰", "untrusted_role_data": "保留具体的接话方式。"},
                   transcript=[{"id": "human-1", "speaker": "USER", "text": question}],
                   topic=question, transport=model, allow_followup=False)
    return service.agents.respond(**(options | kwargs))


def test_search_then_persona_reply_uses_same_actor_and_charges_every_model_call(rig):
    model = Model(search(), reply("资料里山东示例大学2026年物理本科投档最低分590，最低位次30000。你这个位次，先别把它当稳妥选择。"))
    charged = []
    debits = []
    result = run(rig, model, on_usage=lambda value, cid: charged.append((value["tokens"], cid)), on_web_request=debits.append)
    assert len(model.calls) == 2 and len(rig[2]) == 2 and len(debits) == 2
    assert result["tokens"] == 200 and len(charged) == 2
    assert len({cid for _, cid in charged}) == 2
    assert len(rig[0].store.all("agent_instance")) == 1
    assert result["research_id"] and result["source_refs"]
    assert "application_web_evidence" not in str(model.calls[0]["messages"])
    final_context = json.dumps(model.calls[1]["messages"], ensure_ascii=False)
    evidence_payload = json.loads(model.calls[1]["messages"][-1]["content"])
    assert evidence_payload["application_web_evidence"]["sources"][0]["excerpt"] == EXCERPT
    assert "保留具体的接话方式" in final_context
    assert KEY not in final_context
    with pytest.raises(DomainError, match="已发布"):
        rig[1].sources(result["research_id"])


def test_casual_chat_uses_one_model_call_without_search(rig):
    model = Model(reply("今天这杯茶倒不错。"))
    result = run(rig, model, question="刚泡了一杯茶。")
    assert result["messages"] == ["今天这杯茶倒不错。"]
    assert len(model.calls) == 1 and rig[2] == [] and result["research_id"] is None


def test_score_only_question_clarifies_in_persona_without_sending_search(rig):
    model = Model(search(), reply("你哪个省的？先把年份、选科、位次和批次说一下。", "CLARIFY"))
    result = run(rig, model, question="高考580分能报考哪所大学？")
    assert rig[2] == []
    assert result["messages"] == ["你哪个省的？先把年份、选科、位次和批次说一下。"]
    assert "招生省份" in model.calls[0]["messages"][0]["content"]


def test_admission_information_can_accumulate_in_published_user_turns(rig):
    model = Model(search(), reply("2026年山东示例大学物理本科投档最低分590，最低位次30000。"))
    result = run(rig, model, question="本科批，位次50000。", transcript=[
        {"id": "u1", "speaker": "USER", "text": "2026年山东物理580分，志愿怎么填？"},
        {"id": "u2", "speaker": "AGENT", "text": "批次和位次多少？"},
        {"id": "u3", "speaker": "USER", "text": "本科批，位次50000。"},
    ])
    assert result["source_refs"] and len(rig[2]) == 2


def test_no_key_keeps_a_notice_and_never_pretends_to_search(rig):
    rig[1].remove()
    model = Model(reply("我这回还没核对到资料，先别拿一个分数猜学校。", "CLARIFY"))
    result = run(rig, model)
    context = rig[1].context(result["research_id"])
    assert context["status"] == "NOT_CONFIGURED"
    assert "尚未配置" in context["notice"] and rig[2] == []
    assert "不能声称查过" in model.calls[0]["messages"][0]["content"]


@pytest.mark.parametrize("bad", [
    '{"action":"SEARCH","queries":["山东',
    '我先查一下。{"action":"SEARCH","queries":["山东"]}',
    '{"action":"SEARCH","queries":[],"headers":{"secret":"x"}}',
    '{"action":"EXECUTE","command":"whoami"}',
])
def test_broken_or_unauthorized_actions_never_enter_bubbles(rig, bad):
    model = Model(response(bad))
    with pytest.raises(DomainError):
        run(rig, model, question="随便聊聊")
    assert rig[0].store.all("agent_instance")[0]["pending_messages"] == []
    assert len(rig[0].store.all("agent_call")) == 1


@pytest.mark.parametrize("unverified", [
    "山东示例大学2026年投档最低分680。",
    "山东示例大学2026年最低位次20000。",
    "山东示例大学2026年投档最低分580。",  # user's score is not a university cutoff
])
def test_admissions_numbers_without_source_support_are_not_published(rig, unverified):
    model = Model(search(), reply(unverified))
    with pytest.raises(DomainError, match="报考数字"):
        run(rig, model)
    assert len(rig[0].store.all("agent_call")) == 2
    assert rig[0].store.all("agent_instance")[0]["pending_messages"] == []


@pytest.mark.parametrize("other", [
    "2025年 山东 物理 本科\n山东示例大学 投档最低分590。",
    "2026年 河南 物理 本科\n河南示例大学 投档最低分590。",
    "2026年 山东 历史类 本科\n山东示例大学 投档最低分590。",
])
def test_admission_year_province_and_subject_cannot_be_silently_mixed(rig, other):
    def handler(request):
        if request.url.path == "/search":
            return httpx.Response(200, json={"results": [{"url": URL, "title": "录取信息"}]})
        return httpx.Response(200, json={"results": [{"url": URL, "raw_content": other}]})
    rig[3][0] = handler
    with pytest.raises(DomainError, match="报考数字"):
        run(rig, Model(search(), reply("山东示例大学投档最低分590。")))


def test_historical_source_is_usable_when_actual_year_is_explicit(rig):
    def handler(request):
        if request.url.path == "/search":
            return httpx.Response(200, json={"results": [{"url": URL, "title": "往年公告"}]})
        return httpx.Response(200, json={"results": [{"url": URL,
            "raw_content": "2025年 山东 物理 本科\n山东示例大学投档最低分590。"}]})
    rig[3][0] = handler
    result = run(rig, Model(search(), reply("山东示例大学2025年的投档最低分590，只能拿往年作参考。")))
    assert result["messages"] and result["source_refs"]


def test_search_failure_can_end_in_natural_uncertainty_without_fabricating(rig):
    rig[3][0] = lambda request: httpx.Response(429, json={"message": KEY})
    result = run(rig, Model(search(), reply("这回没查到可核对的资料，先别按猜出来的线填。", "CLARIFY")))
    assert "没查到" in result["messages"][0]
    assert rig[1].context(result["research_id"])["status"] == "FAILED"
    assert KEY not in json.dumps(rig[0].store.all("agent_instance"), ensure_ascii=False)


def test_interruption_after_search_prevents_final_model_call(rig):
    cancelled = [False]
    def debit(_):
        cancelled[0] = True
    model = Model(search(), reply("不应生成。"))
    with pytest.raises(DomainError, match="取消"):
        run(rig, model, cancelled=lambda: cancelled[0], on_web_request=debit)
    assert len(model.calls) == 1
    assert rig[0].store.all("agent_instance")[0]["pending_messages"] == []


def test_removing_config_during_final_call_charges_usage_and_discards_reply(rig):
    model = Model(search(), reply("山东示例大学2026年最低分590。"))
    model.after = lambda count: rig[1].remove() if count == 2 else None
    charges = []
    with pytest.raises(DomainError, match="取消"):
        run(rig, model, on_usage=lambda result, cid: charges.append(result["tokens"]))
    assert charges == [100, 100]
    assert rig[0].store.all("agent_instance")[0]["pending_messages"] == []
    assert rig[0].store.all("agent_call")[-1]["discarded"]


def test_web_limit_pauses_original_agent_before_search_dispatch(rig):
    def exhausted(_):
        raise AgentLimit("WEB_REQUEST_LIMIT")
    with pytest.raises(AgentLimit) as error:
        run(rig, Model(search()), on_web_request=exhausted)
    assert error.value.reason == "WEB_REQUEST_LIMIT"
    assert rig[2] == []
    assert rig[0].store.all("agent_instance")[0]["state"] == "PAUSED_LIMIT"


def test_truncated_research_action_is_regenerated_and_all_attempts_metered(rig):
    model = Model(response('{"action":"SEARCH","queries":["未完成', tokens=50, finish_reason="length"),
                  search(), reply("山东示例大学2026年最低分590。"))
    result = run(rig, model)
    assert [c["max_tokens"] for c in model.calls] == [2048, 4096, 2048]
    assert result["tokens"] == 250
    assert "未完成" not in json.dumps(model.calls[-1]["messages"], ensure_ascii=False)


def test_interrupted_search_control_does_not_persist_raw_queries_in_checkpoints(rig):
    raw = '{"action":"SEARCH","queries":["TRANSIENT_QUERY_CANARY'
    cut = response(raw, finish_reason="length")
    with pytest.raises(AgentIncomplete):
        run(rig, Model(cut, cut, cut))
    assert "TRANSIENT_QUERY_CANARY" not in "\n".join(rig[0].store.db.iterdump())
    checkpoint = rig[0].store.all("agent_instance")[0]["checkpoint"]
    assert checkpoint["kind"] == "OUTPUT_TRUNCATED" and "draft" not in checkpoint


def test_no_more_than_two_research_rounds_even_if_model_demands_more(rig):
    model = Model(search("第一次公开查询"), search("第二次公开查询"), search("第三次公开查询"))
    with pytest.raises(DomainError, match="上限"):
        run(rig, model, question="查一下当前公开消息")
    assert len(model.calls) == 3
    assert sum(path == "/search" for path, _ in rig[2]) == 2


def test_current_fact_direct_guess_is_asked_to_search_before_publication(rig):
    model = Model(reply("今天已经公布新政策。"), search("当前公开政策公告"),
                  reply("目前读取的资料不足以确认你说的这项政策。", "CLARIFY"))
    result = run(rig, model, question="查一下今年最新政策")
    assert result["messages"] == ["目前读取的资料不足以确认你说的这项政策。"]
    assert len(model.calls) == 3


@pytest.mark.parametrize("action", ["REPLY", "CLARIFY"])
def test_tag_question_and_clarify_label_cannot_hide_an_unverified_current_assertion(rig, action):
    model = Model(reply("今天最新价格已经达到999元，你买吗？", action), search("当前公开价格"),
                  reply("没有读取到这件商品的价格依据，先告诉我具体型号。", "CLARIFY"))
    result = run(rig, model, question="查一下这个最新价格")
    assert "999" not in result["messages"][0]
    assert len(model.calls) == 3


def test_latest_human_corrections_override_old_province_year_and_subject(rig):
    correction = "刚才说错了，不是山东，是河南；不是2026年，是2027年；不是物理，是历史。"
    model = Model(reply("还没有核实到这个条件下的录取资料，先别急着定。", "CLARIFY"))
    run(rig, model, question=correction, transcript=[
        {"id": "u1", "speaker": "USER", "text": QUESTION},
        {"id": "a1", "speaker": "AGENT", "text": "假设上海2025年历史类，不代表你的真实条件。"},
        {"id": "u2", "speaker": "USER", "text": correction},
    ])
    prompt = model.calls[0]["messages"][0]["content"]
    assert '"province": "河南"' in prompt
    assert '"year": "2027"' in prompt
    assert '"subjects": "历史"' in prompt


def test_new_candidate_resets_prior_admissions_conditions(rig):
    question = "换个情况，另一个考生600分能报考哪里？"
    model = Model(reply("这位同学是哪个省、哪年参加的高考？", "CLARIFY"))
    run(rig, model, question=question, transcript=[
        {"id": "u1", "speaker": "USER", "text": QUESTION},
        {"id": "u2", "speaker": "USER", "text": question},
    ])
    prompt = model.calls[0]["messages"][0]["content"]
    assert '"province": null' in prompt and '"year": null' in prompt
    assert '"score": "600"' in prompt
    assert "招生省份" in prompt and rig[2] == []


def test_target_city_or_school_does_not_replace_home_province():
    conditions = admission_context("2026年山东物理本科580分位次50000，想去北京，能报考北京大学吗？")
    assert conditions["province"] == "山东"
    conditions = admission_context("2026年山东物理本科580分位次50000，省份不是山东了，报考条件要重说。")
    assert conditions["province"] is None and "招生省份" in conditions["missing"]


@pytest.mark.parametrize("row,statement", [
    ("历史类 投档最低分600。", "山东示例大学2026年物理本科投档最低分600。"),
    ("河南 物理类 投档最低分600。", "山东示例大学2026年物理本科投档最低分600。"),
    ("物理类 投档最低分600。", "山东示例大学2026年专业录取线600分。"),
])
def test_number_in_another_table_row_or_line_type_does_not_support_the_claim(rig, row, statement):
    def handler(request):
        if request.url.path == "/search":
            return httpx.Response(200, json={"results": [{"url": URL, "title": "招生表格"}]})
        return httpx.Response(200, json={"results": [{"url": URL,
            "raw_content": "2026年 山东 物理 本科\n山东示例大学 物理类 投档最低分590。\n" + row}]})
    rig[3][0] = handler
    with pytest.raises(DomainError, match="报考数字"):
        run(rig, Model(search(), reply(statement)))


def test_published_same_scope_evidence_can_be_discussed_without_another_web_call(rig):
    service, research, web, _ = rig
    first = run(rig, Model(search(), reply("山东示例大学2026年投档最低分590。")))
    with service.store.transaction():
        service.store.add("conversation", {"title": "isolated", "skill_id": "skill_socratic"}, rid="isolated-room")
        mid = service.store.add_message("isolated-room", "AGENT", first["messages"][0], request_key="published-research")
        research.publish(first["research_id"], mid, 0, first["messages"][0])
    before = len(web)
    model = Model(reply("2026年的590只是已公布的投档最低分，专业选择还得另看。"))
    result = run(rig, model, question="这所学校的专业呢？", transcript=[
        {"id": "u1", "speaker": "USER", "text": QUESTION},
        {"id": mid, "speaker": "AGENT", "text": first["messages"][0],
         "research_id": first["research_id"], "source_refs": first["source_refs"]},
        {"id": "u2", "speaker": "USER", "text": "这所学校的专业呢？"},
    ])
    assert len(web) == before and len(model.calls) == 1
    assert result["research_id"] != first["research_id"] and result["source_refs"]
    assert "application_web_evidence" in model.calls[0]["messages"][-1]["content"]


def test_search_does_not_spend_web_quota_when_final_reply_cannot_fit_remaining_tokens(rig):
    remaining = [20000]
    model = Model(search())
    with pytest.raises(AgentLimit) as error:
        run(rig, model, remaining_tokens=lambda: remaining[0], on_usage=lambda result, cid: remaining.__setitem__(0, 1000))
    assert error.value.reason == "BUDGET_PRECHECK" and rig[2] == []
    assert len(model.calls) == 1


def test_admission_detector_does_not_turn_an_emotional_memory_into_a_form():
    assert admission_context("高考之后很难过，想找你聊聊。") is None
    missing = admission_context("580分能报考哪里？")["missing"]
    assert "分数" not in missing and "招生省份" in missing and "位次" in missing


def test_control_parser_keeps_ordinary_json_and_rejects_unknown_capabilities():
    content = '{"formula":"a*b"}'
    assert parse_agent_action(content) == ("REPLY", content)
    with pytest.raises(DomainError):
        parse_agent_action('{"action":"FETCH","url":"file:///private"}')


@pytest.mark.parametrize("unsupported", [
    "清华大学2026年物理本科投档最低分590，最低位次30000。",
    "山东示例大学投档最低分590，清华大学最低位次30000。",
    "清华大学580分就能录取，你想去吗？",
    "山东示例大学580分就能录取，你想去吗？",
    "山东示例大学投档最低分580，对吧？",
    "山东示例大学最低位次50000，你觉得呢？",
])
def test_unseen_school_and_tag_question_cannot_reuse_another_school_cutoff(rig, unsupported):
    with pytest.raises(DomainError, match="报考数字"):
        run(rig, Model(search(), reply(unsupported)))
    actor = rig[0].store.all("agent_instance")[-1]
    assert actor["pending_messages"] == []


def test_clarifying_users_score_is_still_allowed_without_inventing_a_cutoff(rig):
    text = "你问的是清华大学580分能录取吗？省份、选科和位次先告诉我。"
    model = Model(reply(text, "CLARIFY"))
    result = run(rig, model, question="580分能报考清华大学吗？")
    assert result["messages"] == [text]
    assert rig[2] == []


@pytest.mark.parametrize("swapped", [
    "山东示例大学2026年投档最低分30000分，最低位次590。",
    "山东示例大学2026年的专业组590，投档最低分101。",
    "山东示例大学招生计划30000人。",
])
def test_admission_values_cannot_swap_score_rank_group_or_plan_columns(rig, swapped):
    with pytest.raises(DomainError, match="报考数字"):
        run(rig, Model(search(), reply(swapped)))


def test_admission_table_headers_keep_column_meaning_for_a_valid_reply(rig):
    def table(request):
        if request.url.path == "/search":
            return httpx.Response(200, json={"results": [{"title": "2026本科投档公告", "url": URL}]})
        return httpx.Response(200, json={"results": [{"url": URL, "raw_content":
            "2026年 山东 物理 本科\n| 院校 | 专业组 | 最低分 | 最低位次 |\n"
            "| --- | --- | --- | --- |\n| 山东示例大学 | 101 | 590 | 30000 |"}]})
    rig[3][0] = table
    text = "山东示例大学2026年专业组101投档最低分590，最低位次30000。"
    result = run(rig, Model(search(), reply(text)))
    assert result["messages"] == [text]
