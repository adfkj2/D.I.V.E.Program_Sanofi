"""Official request/response contracts for domestic public-research adapters."""
import pytest
from wot.domain import DomainError
from wot.research_providers import (
    build_fetch,
    build_search,
    extract_usage,
    normalise_fetch,
    normalise_search,
    provider_catalog,
    provider_info,
    validate_provider_config,
)

URL = "https://www.example.edu.cn/admissions/2026"
TEXT = "2026年本科招生。物理类专业组101最低位次30000。"


def test_catalog_prioritises_mainland_services_and_can_reuse_matching_model_keys():
    values = provider_catalog()
    assert [v["id"] for v in values] == ["deepseek", "kimi", "glm", "bocha", "tavily"]
    assert [v["reuse_provider"] for v in values[:3]] == ["deepseek", "kimi", "glm"]
    assert all(not v["requires_model"] for v in values)
    values[2]["search_engines"].append("arbitrary_tool")
    assert "arbitrary_tool" not in provider_info("glm")["search_engines"]


@pytest.mark.parametrize("provider,url,body", [
    ("deepseek", "https://api.deepseek.com/v1/tools/search",
     {"query": "招生计划", "count": 5, "need_content": True}),
    ("kimi", "https://api.moonshot.cn/v1/tools/search",
     {"text_query": "招生计划", "limit": 5, "include_content": True, "timeout_seconds": 20}),
    ("glm", "https://open.bigmodel.cn/api/paas/v4/web_search",
     {"search_query": "招生计划", "search_engine": "search_std", "search_intent": False,
      "content_size": "high", "search_recency_filter": "noLimit", "count": 5}),
    ("bocha", "https://api.bocha.cn/v1/web-search",
     {"query": "招生计划", "freshness": "noLimit", "summary": True, "count": 5}),
])
def test_domestic_wire_contracts_never_send_user_location_or_original_private_question(provider, url, body):
    actual_url, actual_body, headers = build_search(provider, "招生计划")
    assert actual_url == url and actual_body == body and headers == {}
    assert "ori_question" not in actual_body and "localization" not in actual_body
    assert "api_key" not in actual_body and "Authorization" not in headers


def test_qwen_key_is_not_claimed_to_authorise_a_different_search_provider():
    assert all(v["reuse_provider"] != "qwen" for v in provider_catalog())
    with pytest.raises(DomainError, match="支持"):
        build_search("qwen", "招生计划")


def test_glm_quark_omits_the_unsupported_count_parameter_and_engine_is_bounded():
    _, body, _ = build_search("glm", "招生", {"search_engine": "search_pro_quark"})
    assert body["search_engine"] == "search_pro_quark" and "count" not in body
    with pytest.raises(DomainError, match="引擎"):
        build_search("glm", "招生", {"search_engine": "run-script"})


@pytest.mark.parametrize("provider,limit", [("deepseek", 128), ("kimi", 300), ("glm", 70), ("bocha", 300), ("tavily", 300)])
def test_queries_follow_provider_unicode_limits_without_silent_truncation(provider, limit):
    _, body, _ = build_search(provider, "中" * limit)
    assert "中" * limit in body.values()
    with pytest.raises(DomainError, match="缩短"):
        build_search(provider, "中" * (limit + 1))


@pytest.mark.parametrize("query", [None, [], {}, True, "", "  ", "abc\ndef", "abc\x00def"])
def test_invalid_query_types_and_control_characters_never_become_requests(query):
    with pytest.raises(DomainError):
        build_search("deepseek", query)


def test_untrusted_config_cannot_change_request_host_or_headers():
    config = {"base_url": "http://127.0.0.1:1", "headers": {"Authorization": "secret"}, "model": "arbitrary"}
    assert build_search("deepseek", "招生", config)[0] == "https://api.deepseek.com/v1/tools/search"
    assert build_search("deepseek", "招生", config)[2] == {}
    assert validate_provider_config("deepseek", config) == {}


def test_deepseek_only_original_content_is_treated_as_read_page():
    result = normalise_search("deepseek", {"results": [{
        "url": URL, "title": "招生计划", "date": "2026-07-01T00:00:00Z",
        "site_name": "招生办", "snippet": "短摘要", "summary": "模型摘要不是原文", "content": TEXT,
    }]})[0]
    assert result["raw_content"] == TEXT and result["evidence_kind"] == "PAGE_CONTENT"
    assert result["published_at"] == "2026-07-01T00:00:00Z" and result["publisher"] == "招生办"
    missing = normalise_search("deepseek", {"results": [{
        "url": URL, "title": "摘要", "summary": "590分稳上大学", "snippet": "最低590", "content": "",
    }]})[0]
    assert missing["raw_content"] == "" and missing["evidence_kind"] == "SEARCH_SNIPPET"
    assert missing["snippet"] == "590分稳上大学"


def test_kimi_basic_page_content_does_not_fall_back_to_snippets_or_search_pro_chunks():
    result = normalise_search("kimi", {"search_results": [{
        "url": URL, "title": "院校", "text": TEXT, "snippet": "仅摘要", "date": "2026-07-01",
    }]})[0]
    assert result["raw_content"] == TEXT and result["evidence_kind"] == "PAGE_CONTENT"
    pro = normalise_search("kimi", {"search_results": [{
        "url": URL, "title": "院校", "snippet": TEXT, "chunks": [{"text": TEXT, "score": 0.9}],
    }]})[0]
    assert not pro["raw_content"]  # Basic and Pro schemas cannot be silently conflated.


def test_glm_search_content_is_a_snippet_and_reader_content_is_original_text():
    result = normalise_search("glm", {"search_result": [{
        "link": URL, "title": "计划", "content": TEXT, "publish_date": "2026-07-01", "media": "招生办",
    }]})[0]
    assert result["snippet"] == TEXT and result["raw_content"] == ""
    fetched = normalise_fetch("glm", {"reader_result": {"url": URL, "title": "计划", "content": TEXT}}, URL)
    assert fetched["raw_content"] == TEXT and fetched["evidence_kind"] == "PAGE_CONTENT"


def test_bocha_success_envelope_and_summary_are_not_mislabeled_as_original_page():
    value = {"code": 200, "data": {"webPages": {"value": [{
        "url": URL, "name": "计划", "summary": TEXT, "snippet": "短摘", "siteName": "招生办",
        "datePublished": "2026-07-01",
    }]}}}
    source = normalise_search("bocha", value)[0]
    assert source["title"] == "计划" and source["publisher"] == "招生办"
    assert not source["raw_content"] and source["snippet"] == TEXT
    with pytest.raises(DomainError, match="未成功"):
        normalise_search("bocha", {"code": 401, "message": "api_key=never-display"})


@pytest.mark.parametrize("provider,payload", [
    ("deepseek", {"results": []}), ("kimi", {"search_results": []}),
    ("glm", {"search_result": []}), ("bocha", {"code": 200, "data": {"webPages": {"value": []}}}),
    ("tavily", {"results": []}),
])
def test_no_results_is_valid_and_does_not_turn_into_invented_sources(provider, payload):
    assert normalise_search(provider, payload) == []
    usage = extract_usage(provider, payload)
    assert usage["tokens"] is None and usage["credits"] is None


@pytest.mark.parametrize("payload", [[], None, "html", {"error": {"message": "Bearer DO_NOT_LOG"}}, {"results": "not-list"}])
def test_malformed_search_result_raises_only_safe_errors(payload):
    with pytest.raises(DomainError) as caught:
        normalise_search("deepseek", payload)
    assert "DO_NOT_LOG" not in str(caught.value)


@pytest.mark.parametrize("provider,path,body", [
    ("deepseek", "https://api.deepseek.com/v1/tools/fetch", {"url": URL}),
    ("kimi", "https://api.moonshot.cn/v1/tools/fetch", {"url": URL}),
    ("glm", "https://open.bigmodel.cn/api/paas/v4/reader", {
        "url": URL, "timeout": 20, "return_format": "text", "retain_images": False,
        "with_images_summary": False, "with_links_summary": False,
    }),
])
def test_fetch_uses_fixed_official_endpoints_without_model_generation(provider, path, body):
    assert build_fetch(provider, URL) == (path, body, {})


def test_fetch_response_keeps_returned_url_for_coordinator_revalidation():
    other = "http://127.0.0.1/private"
    result = normalise_fetch("kimi", {"url": other, "title": "不可信返回", "markdown": TEXT}, URL)
    assert result["url"] == other
    # Mapping does not claim URL validity. Coordinator must reject this result.
    assert normalise_fetch("deepseek", {"title": "正文不可用", "content": ""}, URL)["raw_content"] == ""


def test_fetch_unavailable_and_malformed_reader_never_use_search_summary_as_body():
    with pytest.raises(DomainError, match="摘要"):
        build_fetch("bocha", URL)
    with pytest.raises(DomainError, match="阅读"):
        normalise_fetch("glm", {"content": TEXT}, URL)


def test_tavily_compatibility_and_credit_accounting_remain_separate_from_model_tokens():
    _, body, _ = build_search("tavily", "招生")
    assert body["include_usage"] and not body["include_answer"] and not body["include_raw_content"]
    result = normalise_fetch("tavily", {"results": [{"url": URL, "raw_content": TEXT}]}, URL)
    assert result["raw_content"] == TEXT
    assert extract_usage("tavily", {"usage": {"credits": 2}}) == {
        "tokens": None, "input_tokens": None, "output_tokens": None, "credits": 2,
    }
    assert extract_usage("deepseek", {"usage": {"total_tokens": 999}})["tokens"] is None
    assert extract_usage("tavily", {"usage": {"credits": True}})["credits"] is None
    assert extract_usage("tavily", {"usage": {"credits": float("inf")}})["credits"] is None


@pytest.mark.parametrize("provider", ["unknown", "qwen", None, True])
def test_unknown_provider_cannot_select_an_arbitrary_endpoint(provider):
    with pytest.raises(DomainError):
        build_search(provider, "招生")
