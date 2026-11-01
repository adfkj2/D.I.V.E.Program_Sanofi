"""Prepare newly generated dialogue once, without touching authored/stored text."""

from __future__ import annotations

import ast
import json
import re
from uuid import uuid4

from markdown_it import MarkdownIt

from .domain import DomainError

DIALOGUE_STYLE_SYSTEM = (
    "依据人物表达档案接住这一轮的话：称呼、句式、口头用词、断句、反问、接梗、情绪由该人物和当下情境决定。"
    "保留有依据的强势、讽刺、普通粗口及表达棱角，不统一改成温和顾问。"
    "人物可以讲故事、反复强调、自我打断、连续反问、只接一个梗、简短赞同或不展开。"
    "不要求每轮有建议、金句、完整结论或追问；不要套短判断加理由加建议的共同结构。"
    "对对方刚说的话保持具体的好奇：有信息缺口、想听后续或值得接着聊时，可以主动问、补充或转接话题。"
    "不要等对方逐项提问才肯展开，也不要每次以你觉得呢、还有什么可以帮你结束；对方已回答的事不要重复问。"
    "核心习惯优先，场景示范只是原创示范，不是本人原话，不逐字照搬或凑口头禅。"
    "资料里的出处、覆盖范围、依据强度和风格标签是背景说明，不在日常台词中复述。"
    "避免客服式开场、报告式总结和重复说明自己正在采用什么风格。"
    "使用自然聊天段落，不用 Markdown 标题、粗斜体、引用块、列表、表格、分隔线或代码围栏。"
    "需要讨论代码或公式时保留实际内容。不要替对方发言。"
    "日常台词不主动提 AI、模拟、扮演或非本人等身份说明。"
    "如果对方直接询问身份，简短如实说明自己是依据资料呈现的 AI 角色，随后回应问题。"
    "不冒认现实身份，不编造未提供的私人经历或当前亲身活动。"
)

DIALOGUE_BATCH_SYSTEM = (
    "本次输出用于聊天消息分条协议，只输出一个完整 JSON 对象："
    '{"messages":["第一条消息","可选的下一条消息"],"idle_followup":null,"expects_reply":false}。'
    "messages 是1至4条本人物将连续发送的文字，每条有独立的表达意图；"
    "允许只发一句或在一条里讲完整故事，不把每个句子、标点或段落机械拆开，不替对方接话。"
    "主动追问应接住刚才的具体话，不必每轮提问，也不把多条消息变成连续盘问。"
    "expects_reply 表示最后是否有明确问题等待对方回答。"
    "idle_followup 只有在没有问题等对方回答、也没有结束谈话时，才可提供一条稍后自然补充的消息，"
    "通常为null；它和messages合计不得超过4条。不得催问、虚构对方的沉默原因或现实活动。"
    "所有消息内部使用自然聊天文字，不使用报告式排版。不要在JSON外写解释或围栏。"
)


def parse_dialogue_batch(
    text: str, *, max_messages: int = 4, allow_followup: bool = True,
) -> dict:
    """Validate a new transport envelope, preparing only generated speech.

    Plain legacy replies remain one semantic message. We never split paragraphs
    or ask another model to repair an envelope. Broken control JSON is an error,
    rather than something a character would say to a human.
    """
    if not isinstance(max_messages, int) or isinstance(max_messages, bool) or not 1 <= max_messages <= 4:
        raise DomainError("聊天分条数量无效")
    if not isinstance(text, str) or not text.strip() or len(text) > 64_000:
        raise DomainError("模型没有返回有效的聊天消息", 502)
    candidate = text.strip().lstrip("\ufeff").strip()
    fenced = re.fullmatch(r"```(?:json)?\s*\n?([\s\S]*?)\n?```", candidate, re.I)
    if fenced and (fenced[1].lstrip().startswith(("{", "["))):
        candidate = fenced[1].strip()
    structured = candidate.startswith(("{", "["))
    if not structured:
        # A prefixed protocol is also invalid: don't leak control fields into a
        # chat bubble merely because a provider added a sentence of commentary.
        if re.search(r'\{\s*"(?:messages|idle_followup|expects_reply)"\s*:', candidate):
            raise DomainError("模型聊天分条格式未完成，请重试回复", 502)
        return {"messages": [prepare_dialogue_reply(text)], "idle_followup": None}
    try:
        value = json.loads(candidate)
    except (ValueError, RecursionError):
        raise DomainError("模型聊天分条格式未完成，请重试回复", 502) from None
    if isinstance(value, dict) and not set(value).intersection({"messages", "idle_followup", "expects_reply"}):
        # Literal JSON/code from a legacy provider is content, not our envelope.
        return {"messages": [prepare_dialogue_reply(text)], "idle_followup": None}
    if not isinstance(value, dict) or set(value) - {"messages", "idle_followup", "expects_reply"}:
        raise DomainError("模型聊天分条格式无效，请重试回复", 502)
    parts = value.get("messages")
    followup = value.get("idle_followup")
    expects_reply = value.get("expects_reply", False)
    if (
        not isinstance(parts, list) or not 1 <= len(parts) <= max_messages
        or any(not isinstance(part, str) or not part.strip() or len(part) > 12_000 for part in parts)
        or (followup is not None and (not isinstance(followup, str) or not followup.strip() or len(followup) > 12_000))
        or type(expects_reply) is not bool
        or len(parts) + int(followup is not None) > max_messages
    ):
        raise DomainError("模型聊天分条内容无效，请重试回复", 502)
    messages = [prepare_dialogue_reply(part) for part in parts]
    # Even if a provider forgets expects_reply, a clear question at the end
    # should wait for the human instead of following them up during silence.
    question = bool(re.search(r"[？?][\s\"'”’）)]*$", messages[-1]))
    return {
        "messages": messages,
        "idle_followup": prepare_dialogue_reply(followup)
        if followup is not None and allow_followup and not expects_reply and not question else None,
    }

def _operator(state, silent):
    if state.src[state.pos:state.pos + 1] != "*" or not state.pos:
        return False
    start = state.pos
    end = start
    while end < state.posMax and state.src[end] == "*":
        end += 1
    before, after = state.src[start - 1], state.src[end:end + 1]
    if not (before.isascii() and (before.isalnum() or before in "_)") and after and after.isascii() and (after.isalnum() or after in "_(")):
        return False
    if not silent:
        state.push("text", "", 0).content = state.src[start:end]
    state.pos = end
    return True


_MARKDOWN = MarkdownIt("commonmark", {"html": False, "maxNesting": 100}).enable(["table", "strikethrough"])
_MARKDOWN.inline.ruler.before("emphasis", "literal_operator", _operator)


def _literal_block(state, start, end, silent):
    """Recognize display TeX and obvious code before Markdown block parsing."""
    line = state.src[state.bMarks[start] + state.tShift[start]:state.eMarks[start]]
    delimiter = "$$" if line.startswith("$$") else "\\]" if line.startswith("\\[") else None
    if delimiter:
        stop = start + 1
        if delimiter not in line[2:]:
            while stop < end:
                candidate = state.src[state.bMarks[stop] + state.tShift[stop]:state.eMarks[stop]]
                stop += 1
                if delimiter in candidate:
                    break
            else:
                return False
        if not silent:
            token = state.push("literal_block", "", 0)
            token.content = state.getLines(start, stop, state.blkIndent, False)
            token.map = [start, stop]
        state.line = stop
        return True
    try:
        tree = ast.parse(line)
    except (SyntaxError, ValueError, RecursionError):
        return False
    if len(tree.body) != 1:
        return False
    if any(isinstance(item, ast.Name) and not item.id.isascii() for item in ast.walk(tree)):
        return False
    node = tree.body[0]
    if not (isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)) or
            isinstance(node, ast.Expr) and isinstance(node.value, (ast.Call, ast.BinOp, ast.Compare, ast.Dict, ast.List, ast.Set))):
        return False
    if not silent:
        token = state.push("literal_block", "", 0)
        token.content, token.map = line, [start, start + 1]
    state.line = start + 1
    return True


_MARKDOWN.block.ruler.before("fence", "literal_block", _literal_block)
_BLOCKS = MarkdownIt("commonmark", {"html": False, "maxNesting": 100}).enable("table").disable("inline")
_BLOCKS.block.ruler.before("fence", "literal_block", _literal_block)
_DUNDER = re.compile(r"(?<!\w)__(?:init|new|name|main|file|all|version|dict|class|repr|str|enter|exit|call|iter|next|len|getattr|setattr|getitem|setitem|eq|hash|doc|slots)__(?!\w)")


def _protect_literals(text):
    """Protect TeX and technical identifiers before the Markdown parser runs.

    A random, source-absent alphabetic marker cannot turn literal contents into
    markup, even when a formula contains line breaks or table pipes. Restoration
    happens after the sole parse, including inside code tokens.
    """
    nonce = "WOTLITERAL" + uuid4().hex
    while nonce in text:
        nonce += "X"
    literals = {}

    def keep(value):
        marker = nonce + str(len(literals)) + "END"
        literals[marker] = value
        return marker

    # Inline blocks never contain fenced code. Protect complete/unclosed code
    # spans before looking for math, so $HOME cannot pair with a later price.
    parts, pos = [], 0
    for opening in re.finditer(r"`+", text):
        if opening.start() < pos:
            continue
        parts.append(text[pos:opening.start()])
        marker = opening[0]
        close = re.search(r"(?<!`)" + re.escape(marker) + r"(?!`)", text[opening.end():])
        stop = opening.end() + close.start() if close else len(text)
        parts.append(keep(text[opening.end():stop]))
        pos = stop + len(marker) if close else len(text)
    parts.append(text[pos:])
    text = "".join(parts)

    def url(match):
        value = match[0]
        if match.start() and match.string[match.start() - 1] == "<":
            return value
        suffix = re.search(r"[*_]+$", value)
        if suffix:
            return keep(value[:suffix.start()]) + suffix[0]
        return keep(value)

    pattern = re.compile(r"(?<!\\)(\$\$[\s\S]*?(?<!\\)\$\$|\\\[[\s\S]*?\\\]|\\\([\s\S]*?\\\)|\$(?!\s)[\s\S]*?(?<![\\\s])\$)")
    text = pattern.sub(lambda m: m[0] if nonce in m[0] else keep(m[0]), text)
    text = _DUNDER.sub(lambda m: keep(m[0]), text)
    text = re.sub(r"https?://[^\s<>\[\]()]+", url, text)
    return text, literals


def _prepare_inline(content, env):
    protected, literals = _protect_literals(content)
    value = _inline(_MARKDOWN.parseInline(protected, env)[0].children)
    value = re.sub(r"(?<![A-Za-z0-9*])\*{2,3}(?=[\u3400-\u9fff])", "", value)
    value = re.sub(r"^\*{1,3}(?=[\u3400-\u9fff])", "", value)
    for marker, literal in reversed(list(literals.items())):
        value = value.replace(marker, literal)
    return value


def _inline(tokens):
    """Read semantic tokens; never parse code/math a second time."""
    output, links = [], []
    for token in tokens or []:
        kind = token.type
        if kind in {"text", "code_inline", "html_inline"}:
            content = token.content
            if kind == "text":
                # CommonMark intentionally leaves intraword CJK underscores
                # literal. Models still use them as emphasis in Chinese prose.
                content = re.sub(r"(?<![A-Za-z0-9_])(_{1,2})([\u3400-\u9fff][^_\n]*?)\1(?![A-Za-z0-9_])", r"\2", content)
                content = re.sub(r"(?<![A-Za-z0-9*])\*{2,3}(?=[\u3400-\u9fff])", "", content)
            output.append(content)
        elif kind in {"softbreak", "hardbreak"}:
            output.append("\n")
        elif kind == "link_open":
            links.append((len(output), token.attrGet("href") or ""))
        elif kind == "link_close" and links:
            start, target = links.pop()
            if "".join(output[start:]) != target:
                output.append(f"（{target}）")
        elif kind == "image":
            label = _inline(token.children) or token.content
            target = token.attrGet("src") or ""
            output.append(f"{label}（{target}）")
        elif kind.endswith("_open") or kind.endswith("_close"):
            continue
        else:
            output.append(token.content)
    return "".join(output)


def _raw_cells(line):
    line = re.sub(r"^\s*(?:>\s*)+", "", line).strip()
    cells, current, i = [], [], 0
    while i < len(line):
        if line[i] == "\\" and i + 1 < len(line):
            current.append(line[i:i + 2])
            i += 2
            continue
        if line[i] == "|":
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(line[i])
        i += 1
    cells.append("".join(current).strip())
    if line.startswith("|"):
        cells.pop(0)
    if line.endswith("|"):
        cells.pop()
    return cells


def _table(tokens, start, env, source_lines):
    names, rows, cells = [], [], []
    header = False
    i = start + 1
    while i < len(tokens) and tokens[i].type != "table_close":
        token = tokens[i]
        if token.type == "thead_open":
            header = True
        elif token.type == "thead_close":
            header = False
        elif token.type == "tr_open":
            cells = []
            row_map = token.map
        elif token.type == "inline":
            cells.append(_prepare_inline(token.content, env))
        elif token.type == "tr_close":
            if row_map:
                # Markdown renderers discard excess cells in malformed rows.
                # Conversation preparation keeps every supplied value instead.
                cells = [_prepare_inline(cell, env) for cell in _raw_cells(source_lines[row_map[0]])]
            if header:
                names = cells
            else:
                rows.append("；".join(
                    (f"{names[n]}：" if n < len(names) and names[n] else "") + value
                    for n, value in enumerate(cells)
                ))
        i += 1
    return rows or ["；".join(names)], i


def prepare_dialogue_reply(text: str) -> str:
    """Normalize only a new model utterance, before persistence/draft exposure.

    User messages, interventions, assessments and historical reads never call
    this function. The resulting plain text is persisted and reused verbatim.
    """
    if not isinstance(text, str):
        raise DomainError("模型没有返回可显示的聊天内容", 502)
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    # Table splitting precedes inline parsing. Only hide literal pipes here;
    # retain every other byte so code/math cannot consume block boundaries.
    pipe_marker = "WOTPIPE" + uuid4().hex
    normalized = re.sub(r"(?<!`)(`+)([^`]*?)\1(?!`)", lambda m: m[0].replace("|", pipe_marker), normalized)
    normalized = re.sub(r"\$[^$\n]+\$|\\\([^\n]*?\\\)", lambda m: m[0].replace("|", pipe_marker), normalized)
    env = {}
    tokens = _BLOCKS.parse(normalized, env)
    source_lines = normalized.split("\n")
    result, lists, i, last_end, heading_map = [], [], 0, 0, None

    def append(value, mapping):
        nonlocal last_end
        if mapping:
            if result and mapping[0] > last_end:
                result.append("")
            last_end = mapping[1]
        result.append(value.rstrip("\n"))

    while i < len(tokens):
        token = tokens[i]
        kind = token.type
        if kind == "heading_open":
            heading_map = token.map
        elif kind == "hr":
            last_end = max(last_end, token.map[1])
        elif kind == "table_open":
            rows, i = _table(tokens, i, env, source_lines)
            append("\n".join(rows), token.map)
        elif kind in {"bullet_list_open", "ordered_list_open"}:
            lists.append({"ordered": kind == "ordered_list_open", "next": int(token.attrGet("start") or 1), "pending": False})
        elif kind in {"bullet_list_close", "ordered_list_close"}:
            lists.pop()
        elif kind == "list_item_open" and lists:
            lists[-1]["pending"] = True
            if lists[-1]["ordered"] and token.map:
                number = re.match(r"^\s*(?:>\s*)*(?:[-+*]\s+)?(\d{1,9})[.)]\s", source_lines[token.map[0]])
                if number:
                    lists[-1]["next"] = int(number[1])
        elif kind in {"inline", "fence", "code_block", "html_block", "literal_block"}:
            value = _prepare_inline(token.content, env) if kind == "inline" else token.content
            if kind == "inline":
                value = re.sub(r"^\[([ xX])\]\s+", lambda m: "已完成：" if m[1].lower() == "x" else "未完成：", value) if lists else value
            if lists and lists[-1]["pending"]:
                item = lists[-1]
                if item["ordered"]:
                    value = f"{item['next']}. {value}"
                    item["next"] += 1
                item["pending"] = False
            append(value, heading_map or token.map)
            heading_map = None
        i += 1
    prepared = "\n".join(result).strip("\n")
    prepared = prepared.replace(pipe_marker, "|")
    if not prepared.strip():
        raise DomainError("模型回复只有排版标记，请重试回复", 502)
    return prepared
