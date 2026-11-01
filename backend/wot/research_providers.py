"""Fixed, credential-free wire contracts for public research providers.

Only fields documented as original page content become ``raw_content``. Search
summaries remain snippets and cannot pass the application's read-source gate.
These adapters never contact a network or select/fetch a credential.
"""
from __future__ import annotations

from copy import deepcopy
from math import isfinite

from .domain import DomainError

_PROVIDERS = (
    {
        "id": "deepseek", "name": "DeepSeek 搜索", "reuse_provider": "deepseek",
        "description": "复用 DeepSeek 模型 Key，提供搜索及网页正文读取。",
        "key_hint": "DeepSeek API Key", "query_limit": 128, "has_fetch": True,
        "requires_model": False, "required_fields": [],
        "docs_url": "https://api-docs.deepseek.com/guides/search/",
    },
    {
        "id": "kimi", "name": "Kimi 搜索", "reuse_provider": "kimi",
        "description": "复用 Kimi 模型 Key，使用境内搜索和网页读取接口。",
        "key_hint": "Kimi / Moonshot API Key", "query_limit": 300, "has_fetch": True,
        "requires_model": False, "required_fields": [],
        "docs_url": "https://platform.kimi.com/docs/guide/best-practices-for-web-search",
    },
    {
        "id": "glm", "name": "智谱搜索", "reuse_provider": "glm",
        "description": "复用智谱模型 Key，搜索后通过网页阅读接口核对正文。",
        "key_hint": "智谱 BigModel API Key", "query_limit": 70, "has_fetch": True,
        "requires_model": False, "required_fields": [],
        "search_engines": ["search_std", "search_pro", "search_pro_sogou", "search_pro_quark"],
        "docs_url": "https://docs.bigmodel.cn/api-reference/工具-api/网络搜索",
    },
    {
        "id": "bocha", "name": "博查搜索", "reuse_provider": None,
        "description": "境内独立搜索服务；返回搜索摘要，不将摘要冒充网页正文。",
        "key_hint": "博查 API Key", "query_limit": 300, "has_fetch": False,
        "requires_model": False, "required_fields": [],
        "docs_url": "https://github.com/Bocha-Labs/bocha-skills/blob/main/bocha-web-search/SKILL.md",
    },
    {
        "id": "tavily", "name": "Tavily", "reuse_provider": None,
        "description": "独立搜索与网页读取服务，保留原有配置兼容。",
        "key_hint": "Tavily API Key", "query_limit": 300, "has_fetch": True,
        "requires_model": False, "required_fields": [],
        "docs_url": "https://docs.tavily.com/documentation/api-reference/endpoint/search",
    },
)
_BY_ID = {value["id"]: value for value in _PROVIDERS}


def provider_catalog():
    """Safe display metadata, ordered for the mainland-first experience."""
    return deepcopy(list(_PROVIDERS))


def provider_info(provider):
    if not isinstance(provider, str) or provider not in _BY_ID:
        raise DomainError("请选择支持的联网服务")
    return deepcopy(_BY_ID[provider])


def validate_provider_config(provider, config=None):
    """Only app-defined options are used; endpoints/headers are never input."""
    info = provider_info(provider)
    config = {} if config is None else config
    if not isinstance(config, dict):
        raise DomainError("联网服务参数格式无效")
    result = {}
    if provider == "glm":
        engine = config.get("search_engine", "search_std")
        if engine not in info["search_engines"]:
            raise DomainError("智谱搜索引擎配置无效")
        result["search_engine"] = engine
    return result


def _query(provider, query):
    info = provider_info(provider)
    if (not isinstance(query, str) or not query.strip()
            or len(query.strip()) > info["query_limit"]
            or any(ord(c) < 32 for c in query)):
        raise DomainError(f"{info['name']}查询词需为1至{info['query_limit']}字，请缩短查询词后重试")
    return query.strip()


def build_search(provider, query, config=None):
    """Return (fixed URL, JSON body, non-secret additional headers)."""
    query = _query(provider, query)
    options = validate_provider_config(provider, config)
    if provider == "deepseek":
        return "https://api.deepseek.com/v1/tools/search", {
            "query": query, "count": 5, "need_content": True,
        }, {}
    if provider == "kimi":
        return "https://api.moonshot.cn/v1/tools/search", {
            "text_query": query, "limit": 5, "include_content": True, "timeout_seconds": 20,
        }, {}
    if provider == "glm":
        body = {"search_query": query, "search_engine": options["search_engine"],
                "search_intent": False, "content_size": "high", "search_recency_filter": "noLimit"}
        if options["search_engine"] != "search_pro_quark":
            body["count"] = 5
        return "https://open.bigmodel.cn/api/paas/v4/web_search", body, {}
    if provider == "bocha":
        return "https://api.bocha.cn/v1/web-search", {
            "query": query, "freshness": "noLimit", "summary": True, "count": 5,
        }, {}
    return "https://api.tavily.com/search", {
        "query": query, "search_depth": "basic", "max_results": 5,
        "include_answer": False, "include_raw_content": False,
        "include_published_date": True, "include_usage": True, "auto_parameters": False,
    }, {}


def build_fetch(provider, url, config=None):
    """URL public-address checks belong to the coordinator before this mapping."""
    provider_info(provider)
    validate_provider_config(provider, config)
    if not isinstance(url, str) or not url or len(url) > 8192:
        raise DomainError("网页地址无效")
    if provider == "deepseek":
        return "https://api.deepseek.com/v1/tools/fetch", {"url": url}, {}
    if provider == "kimi":
        return "https://api.moonshot.cn/v1/tools/fetch", {"url": url}, {}
    if provider == "glm":
        return "https://open.bigmodel.cn/api/paas/v4/reader", {
            "url": url, "timeout": 20, "return_format": "text", "retain_images": False,
            "with_images_summary": False, "with_links_summary": False,
        }, {}
    if provider == "tavily":
        return "https://api.tavily.com/extract", {
            "urls": [url], "extract_depth": "advanced", "format": "text",
            "include_images": False, "include_usage": True,
        }, {}
    raise DomainError("该联网服务只返回搜索摘要，未提供网页正文读取接口")


def _text(value):
    return value if isinstance(value, str) else ""


def _candidate(*, url, title, published_at=None, raw_content="", snippet="", publisher=""):
    raw_content, snippet = _text(raw_content), _text(snippet)
    return {"url": _text(url), "title": _text(title),
            "published_at": _text(published_at) or None,
            "raw_content": raw_content, "snippet": snippet, "publisher": _text(publisher),
            "evidence_kind": "PAGE_CONTENT" if raw_content.strip() else "SEARCH_SNIPPET"}


def normalise_search(provider, payload):
    provider_info(provider)
    if not isinstance(payload, dict) or payload.get("error"):
        raise DomainError("联网服务未返回有效的搜索结果", 502)
    if provider == "bocha":
        if payload.get("code") not in (200, "200"):
            raise DomainError("博查搜索未成功，请检查服务权限或账户额度", 502)
        data = payload.get("data")
        pages = data.get("webPages") if isinstance(data, dict) else None
        items = pages.get("value") if isinstance(pages, dict) else None
    else:
        field = "search_results" if provider == "kimi" else "search_result" if provider == "glm" else "results"
        items = payload.get(field)
    if not isinstance(items, list):
        raise DomainError("联网服务返回的搜索结果格式无效", 502)
    result = []
    for item in items[:50]:
        if not isinstance(item, dict):
            continue
        if provider == "deepseek":
            candidate = _candidate(url=item.get("url"), title=item.get("title"),
                published_at=item.get("date"), raw_content=item.get("content"),
                snippet=item.get("summary") or item.get("snippet"), publisher=item.get("site_name"))
        elif provider == "kimi":
            candidate = _candidate(url=item.get("url"), title=item.get("title"),
                published_at=item.get("date"), raw_content=item.get("text"),
                snippet=item.get("snippet"), publisher=item.get("site_name"))
        elif provider == "glm":
            candidate = _candidate(url=item.get("link"), title=item.get("title"),
                published_at=item.get("publish_date"), snippet=item.get("content"), publisher=item.get("media"))
        elif provider == "bocha":
            candidate = _candidate(url=item.get("url"), title=item.get("name"),
                published_at=item.get("datePublished"), snippet=item.get("summary") or item.get("snippet"),
                publisher=item.get("siteName"))
        else:
            candidate = _candidate(url=item.get("url"), title=item.get("title"),
                published_at=item.get("published_date"), raw_content=item.get("raw_content"),
                snippet=item.get("content"))
        if candidate["url"]:
            result.append(candidate)
    return result


def normalise_fetch(provider, payload, url):
    provider_info(provider)
    if not isinstance(payload, dict) or payload.get("error"):
        raise DomainError("联网服务未返回有效的网页正文", 502)
    if provider == "deepseek":
        return _candidate(url=url, title=payload.get("title"), raw_content=payload.get("content"))
    if provider == "kimi":
        return _candidate(url=payload.get("url") or url, title=payload.get("title"), raw_content=payload.get("markdown"))
    if provider == "glm":
        value = payload.get("reader_result")
        if not isinstance(value, dict):
            raise DomainError("智谱未返回有效的网页阅读结果", 502)
        return _candidate(url=value.get("url") or url, title=value.get("title"), raw_content=value.get("content"))
    if provider == "tavily":
        items = payload.get("results")
        if not isinstance(items, list):
            raise DomainError("联网服务返回的网页正文格式无效", 502)
        for item in items:
            if isinstance(item, dict) and item.get("url") == url:
                return _candidate(url=url, title=item.get("title"), raw_content=item.get("raw_content"))
        return _candidate(url=url, title="")
    raise DomainError("该联网服务未提供网页正文读取接口")


def extract_usage(provider, payload):
    """Direct search/fetch calls do not manufacture LLM token consumption."""
    provider_info(provider)
    result = {"tokens": None, "input_tokens": None, "output_tokens": None, "credits": None}
    usage = payload.get("usage") if isinstance(payload, dict) else None
    if provider == "tavily" and isinstance(usage, dict):
        credits = usage.get("credits")
        if (isinstance(credits, (int, float)) and not isinstance(credits, bool)
                and isfinite(credits) and credits >= 0):
            result["credits"] = credits
    return result
