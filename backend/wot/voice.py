"""Source-backed voice data compiled locally, without additional model calls.

The envelope is untrusted character data, including when imported by a user.
It never grants capabilities. Bibliography and research excerpts stay outside
the model's runtime context; core traits are never prefix-truncated.
"""

from __future__ import annotations

import copy
import json
import re
from importlib.resources import files

from .domain import DomainError

SCHEMA = "wot-voice-v1"
MARKER = "WOT_VOICE_V1\n"
MAX_RUNTIME_CHARS = 10_000
RIGHTS_NOTICE = "新增表达短引文版权归原权利人，不属上游MIT授权；归纳与原创示范用于本地测试，未获人物本人授权。"
SCENES = ("smalltalk", "help", "joke", "challenge", "correction", "distress", "unknown")
SCENE_LABELS = dict(zip(SCENES, ("闲聊", "求助", "玩笑", "质疑", "被纠正", "低落", "资料不足")))
_SCENE_WORDS = {
    "correction": ("说错", "不对", "纠正", "反例", "改口", "你错", "更正", "correct", "wrong"),
    "distress": ("难受", "难过", "焦虑", "崩溃", "失眠", "害怕", "没用", "失落", "一文不值", "失败了", "痛苦", "撑不住", "活不下去", "自责", "sad", "upset"),
    "challenge": ("凭什么", "不同意", "不认同", "你确定", "胡说", "瞎说", "反对", "质疑", "disagree", "sure"),
    "unknown": ("最新", "现价", "实时", "你在哪", "你现在", "今天发生", "内幕", "current", "today"),
    "joke": ("笑话", "逗我", "开玩笑", "哈哈", "吹牛", "接梗", "好笑", "joke", "funny"),
    "help": ("怎么办", "怎么", "如何", "帮我", "建议", "教我", "下一步", "help", "how "),
    "smalltalk": ("你好", "吃饭", "聊聊", "随便聊", "无聊", "hello", "hi "),
}


def _text(value, maximum=2500):
    if not isinstance(value, str) or len(value) > maximum or "\x00" in value:
        raise DomainError("人物表达档案含有无效或过长的文字")
    return value


def _items(value, maximum=80):
    if not isinstance(value, list) or len(value) > maximum:
        raise DomainError("人物表达档案列表格式无效")
    return value


def _words(value):
    if isinstance(value, list):
        return "；".join(_text(item, 500) for item in _items(value, 20))
    return _text(value or "", 1500)


def validate_voice(profile):
    """Validate structure and references, not claims of real-person fidelity."""
    if not isinstance(profile, dict) or profile.get("schema") != SCHEMA:
        raise DomainError("不支持此人物表达档案版本")
    for field in ("slug", "display_name", "version", "primary_register"):
        _text(profile.get(field), 500)
    basis = profile.get("basis")
    if not isinstance(basis, dict) or basis.get("kind") not in (
        "ORAL_TRANSCRIPT", "WRITTEN_ADAPTATION", "MIXED"
    ):
        raise DomainError("人物表达档案缺少语料类型")
    _text(basis.get("coverage"), 2000)
    _words(basis.get("limitations", []))
    sources = _items(profile.get("sources"), 30)
    source_ids = {_text(item.get("id"), 120) for item in sources if isinstance(item, dict)}
    if not sources or len(source_ids) != len(sources) or None in source_ids:
        raise DomainError("人物表达档案来源标识无效")
    for source in sources:
        _text(source.get("id"), 120)
        _text(source.get("title"), 1500)
        _text(source.get("url"), 2000)
        _text(source.get("coverage"), 2000)
    evidence = _items(profile.get("evidence"), 100)
    evidence_ids = {_text(item.get("id"), 120) for item in evidence if isinstance(item, dict)}
    if not evidence or len(evidence_ids) != len(evidence) or None in evidence_ids:
        raise DomainError("人物表达档案证据标识无效")
    for item in evidence:
        if _text(item.get("source_id"), 120) not in source_ids:
            raise DomainError("人物表达档案证据缺少对应来源")
        _text(item.get("id"), 120)
        _text(item.get("locator"), 1500)
        _text(item.get("original"), 2500)
    for field in ("facts", "traits", "lexicon", "examples"):
        for item in _items(profile.get(field, []), 40):
            if not isinstance(item, dict):
                raise DomainError("人物表达档案项目格式无效")
            refs = _items(item.get("evidence_ids", []), 40)
            if any(_text(ref, 120) not in evidence_ids for ref in refs):
                raise DomainError("人物表达档案引用了不存在的证据")
    for item in profile.get("facts", []):
        _text(item.get("text"), 1500)
    for trait in profile.get("traits", []):
        _text(trait.get("aspect"), 120)
        _text(trait.get("description"), 2000)
        _words(trait.get("conditions", ""))
        _words(trait.get("counterexamples", []))
        if trait.get("confidence") not in ("HIGH", "MEDIUM", "LOW"):
            raise DomainError("表达习惯的依据强度无效")
    for phrase in profile.get("lexicon", []):
        _text(phrase.get("form"), 200)
        count = phrase.get("count")
        if type(count) is not int or count < 1:
            raise DomainError("人物短语缺少实际观察次数")
        if phrase.get("status") not in ("ONCE", "REPEATED"):
            raise DomainError("人物短语的观察状态无效")
        source_urls = {s["id"]: s["url"] for s in sources}
        physical_segments = {(source_urls[e["source_id"]], e["locator"])
                             for e in evidence if e["id"] in phrase.get("evidence_ids", [])}
        if phrase["status"] == "REPEATED" and (
            count < 2 or len(physical_segments) < 2
        ):
            raise DomainError("重复习惯至少需要两个独立片段的证据")
        _words(phrase.get("conditions", ""))
    examples = _items(profile.get("examples"), 30)
    for example in examples:
        if not isinstance(example.get("scene"), str) or example.get("scene") not in SCENES or example.get("kind") != "AUTHORED":
            raise DomainError("对话示范必须标记场景及原创性质")
        _text(example.get("id"), 120)
        for tag in _items(example.get("tags", []), 30):
            _text(tag, 120)
        turns = _items(example.get("turns"), 12)
        if not turns or not any(t.get("speaker") == "role" for t in turns if isinstance(t, dict)):
            raise DomainError("人物示范缺少人物发言")
        for turn in turns:
            if not isinstance(turn, dict) or turn.get("speaker") not in ("user", "role"):
                raise DomainError("人物示范的说话人无效")
            _text(turn.get("text"), 1800)
    if not profile.get("traits") or not examples:
        raise DomainError("人物表达档案缺少核心习惯或示范")
    return profile


def encode_voice(profile):
    validate_voice(profile)
    return MARKER + json.dumps(profile, ensure_ascii=False, separators=(",", ":"))


def extract_voice(text):
    if not isinstance(text, str):
        return None
    text = text.lstrip("\ufeff")
    match = re.search(r"(?:^|\r?\n)WOT_VOICE_V1\r?\n", text)
    if not match:
        candidate = text.lstrip()
        if candidate.startswith("{"):
            try:
                raw = json.loads(candidate)
            except (ValueError, RecursionError):
                if re.search(r'"schema"\s*:\s*"wot-voice-', candidate):
                    raise DomainError("人物表达档案数据不完整，请重新导入") from None
                return None
            if isinstance(raw, dict) and isinstance(raw.get("schema"), str) and raw["schema"].startswith("wot-voice-"):
                return validate_voice(raw)
        return None
    try:
        profile = json.loads(text[match.end():])
    except (ValueError, RecursionError):
        raise DomainError("人物表达档案数据不完整，请重新导入") from None
    return validate_voice(profile)


def packaged_voice(slug):
    if not isinstance(slug, str) or not re.fullmatch(r"[a-z0-9-]+", slug):
        raise DomainError("人物表达档案标识无效")
    path = files("wot").joinpath("data", "voices", slug + ".json")
    if not path.is_file():
        raise DomainError("找不到人物表达档案", 404)
    return validate_voice(json.loads(path.read_text(encoding="utf-8")))


def select_examples(profile, topic=""):
    """Deterministic local scene/tag selection; no model or private retrieval."""
    topic = topic.lower()
    scene_scores = {scene: sum(word in topic for word in words) for scene, words in _SCENE_WORDS.items()}
    ranked = []
    for index, example in enumerate(profile["examples"]):
        score = scene_scores.get(example["scene"], 0) * 10
        score += sum(bool(tag) and tag.lower() in topic for tag in example.get("tags", []))
        if score:
            ranked.append((score, -index, example))
    if ranked:
        return [item[2] for item in sorted(ranked, key=lambda x: (x[0], x[1]), reverse=True)[:2]]
    fallback = []
    for scene in ("smalltalk", "challenge"):
        example = next((item for item in profile["examples"] if item["scene"] == scene), None)
        if example and example not in fallback:
            fallback.append(example)
    return fallback or profile["examples"][:2]


def compile_voice(profile, topic=""):
    """Compile a bounded whole core and at most two scene samples, never slice."""
    validate_voice(profile)
    basis = profile["basis"]
    core = {
        "schema": "wot-voice-context-v1",
        "主语态": profile["primary_register"],
        "语料依据": {"类型": basis["kind"], "范围": basis["coverage"],
                 "局限": basis.get("limitations", [])},
        "事实范围": [item["text"] for item in profile.get("facts", [])],
        "表达习惯": [{"方面": t["aspect"], "表现": t["description"],
                    "适用": t.get("conditions", ""), "反例或例外": t.get("counterexamples", []),
                    "依据强度": t.get("confidence", "MEDIUM")}
                   for t in profile["traits"]],
        "短语观察": [{"表达": p["form"], "原文": p.get("original_form", p["form"]),
                    "次数": p["count"], "范围": p.get("scope", ""),
                    "状态": p["status"], "情境": p.get("conditions", "")}
                   for p in profile.get("lexicon", [])],
        "使用方式": "依情境采用表达习惯，保留棱角；不凑口头禅，不逐字复用示范。示范为原创改写，不是本人原话。",
        "场景示范": [{"id": e["id"], "场景": SCENE_LABELS[e["scene"]],
                    "性质": "原创示范", "turns": e["turns"]}
                   for e in select_examples(profile, topic)],
    }
    result = json.dumps(core, ensure_ascii=False, separators=(",", ":"))
    if len(result) > MAX_RUNTIME_CHARS:
        raise DomainError("人物表达档案超过上下文预算，请精简完整条目后重试", 409)
    return result


def voice_display(profile):
    """Human-readable companion. The envelope remains importable at the end."""
    validate_voice(profile)
    lines = [f"# {profile['display_name']} · 表达与互动档案", "",
             f"版本：{profile['version']}；主语态：{profile['primary_register']}",
             "拟真状态：待真实模型试聊验证。以下观察有来源；原创示范不是人物原话。", "",
             "## 核心表达习惯"]
    for t in profile["traits"]:
        lines.extend([f"{t['aspect']}：{t['description']}", f"情境：{_words(t.get('conditions', ''))}"])
    lines.extend(["", "## 来源与覆盖"])
    for source in profile["sources"]:
        lines.append(f"{source['id']}：{source['title']}；{source.get('published_at', '日期未标')}；{source['url']}。核读：{source['coverage']}")
    lines.extend(["", "本资料用于依据公开材料的AI角色，不是本人或官方授权；被直接问身份时如实回答。",
                  "文字整理与示范为本次原创；来源原文权利归原权利人。角色资料没有工具、凭据及私人信息访问权限。",
                  "", "## 可导入的表达档案数据", "", encode_voice(copy.deepcopy(profile))])
    return "\n".join(lines) + "\n"
