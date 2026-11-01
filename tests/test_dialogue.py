"""New generated speech is prepared once; private and authored text is data."""

import pytest
from wot.dialogue import DIALOGUE_STYLE_SYSTEM, prepare_dialogue_reply
from wot.domain import DomainError
from wot.model import Provider
from wot.service import WotService


@pytest.mark.parametrize(("raw", "expected"), [
    ("# 我的看法\n\n**先看风险**，再谈*收益*和_代价_。", "我的看法\n\n先看风险，再谈收益和代价。"),
    ("## 标题 ##\n---\n> 先等等。\n>> 再想想。", "标题\n先等等。\n再想想。"),
    ("标题\n===\n- 看条件\n* 看证据\n+ 看反例\n1. 保留第一步\n2. 再决定", "标题\n看条件\n看证据\n看反例\n1. 保留第一步\n2. 再决定"),
    ("- [x] 观察\n- [ ] 实验", "已完成：观察\n未完成：实验"),
    ("3. 价格 10 元\n9. 时间 5 s", "3. 价格 10 元\n9. 时间 5 s"),
    ("| 项目 | 数值 |\n| :--- | ---: |\n| 价格 | 3.5 元 |\n| 时长 | 2 h |", "项目：价格；数值：3.5 元\n项目：时长；数值：2 h"),
    ("A | B\n--- | ---\n`a|b` | x\\|y", "A：a|b；B：x|y"),
    ("[资料](https://example.com/a_b?q=2#ref) 和 <https://example.com/a_b>。", "资料（https://example.com/a_b?q=2#ref） 和 https://example.com/a_b。"),
    ("![图例](https://example.com/a.png)", "图例（https://example.com/a.png）"),
    ("[文档](https://example.com/func(a))", "文档（https://example.com/func(a)）"),
    ("```python\n# comment\na_b = 2 ** 3\nx = a * b\n```\n**这里**只解释。", "# comment\na_b = 2 ** 3\nx = a * b\n这里只解释。"),
    ("~~~csharp\n// C#\nvar a_b = 3;\n~~~", "// C#\nvar a_b = 3;"),
    ("> ```python\n> # comment\n> a_b = 2 ** 3\n> ```", "# comment\na_b = 2 ** 3"),
    ("`x_y ** 2`，``a ` b``", "x_y ** 2，a ` b"),
    ("    # actual comment\n    f_x = 2 ** 3", "# actual comment\nf_x = 2 ** 3"),
    ("- ```python\n  # comment\n  x_y = 2 ** 3\n  ```", "# comment\nx_y = 2 ** 3"),
    ("**example `a**b` code**", "example a**b code"),
    ("```js\n`unclosed *a*\n```", "`unclosed *a*"),
    ("`unclosed *a*", "unclosed *a*"),
    ("**see https://a.test/x**y full URL**", "see https://a.test/x**y full URL"),
    ('__init__ __name__ == "__main__" **kwargs *args', '__init__ __name__ == "__main__" **kwargs *args'),
    ("formula $a_b\n*c* + d$", "formula $a_b\n*c* + d$"),
    ("a*b*c = abc", "a*b*c = abc"),
    ("计算 2**3**2 = 512；f(x) = x**2 + y**2。3*4*5=60", "计算 2**3**2 = 512；f(x) = x**2 + y**2。3*4*5=60"),
    ('print("**bold**")', 'print("**bold**")'),
    ('a_b = {"key": "**bold**"}', 'a_b = {"key": "**bold**"}'),
    ('```bash\necho "$HOME"\n```\n**强调**，费用$5。', 'echo "$HOME"\n强调，费用$5。'),
    ('`echo $HOME` **强调**，费用$5。', 'echo $HOME 强调，费用$5。'),
    ('```py\nprint("\\\\[")\n```\n**强调** \\]', 'print("\\\\[")\n强调 ]'),
    ("| A | B |\n| --- | --- |\n| ```a|b``` | c |", "A：a|b；B：c"),
    ("| A | B |\n| --- | --- |\n| x | y | z |", "A：x；B：y；z"),
    ("这里是**未完成的强调", "这里是未完成的强调"),
    ("~~旧观点~~，仍然**有依据**。", "旧观点，仍然有依据。"),
    ("> | A | B |\n> | --- | --- |\n> | 1 | 2 |", "A：1；B：2"),
    ("[doc][ref]\n\n[ref]: https://example.com/a_b", "doc（https://example.com/a_b）"),
    ("$$\nf_x = a * b\n# mathematical label\n$$\n\\[\na_b ** 2\n\\]", "$$\nf_x = a * b\n# mathematical label\n$$\n\\[\na_b ** 2\n\\]"),
    (r"公式 $a_b * c$ 和 \(x_y ** 2\) 保留。", r"公式 $a_b * c$ 和 \(x_y ** 2\) 保留。"),
    ("C#，a_b，x*y，2 ** 3，5 * 6 = 30，3.5 kg，https://a.test/*x*/a_b#c", "C#，a_b，x*y，2 ** 3，5 * 6 = 30，3.5 kg，https://a.test/*x*/a_b#c"),
    ("***加重***与**外层 *内层***，以及*外层 **内层***。", "加重与外层 内层，以及外层 内层。"),
    ("**未完成的强调", "未完成的强调"),
    ("```python\n# 未闭合代码\na_b = 2 ** 3", "# 未闭合代码\na_b = 2 ** 3"),
    ("`未闭合代码 a_b * c", "未闭合代码 a_b * c"),
    ("我是依据资料呈现的 AI 角色。观点**不变**。", "我是依据资料呈现的 AI 角色。观点不变。"),
    ("a\r\n\r\nb", "a\n\nb"),
])
def test_plain_chat_preserves_meaning_and_technical_content(raw, expected):
    assert prepare_dialogue_reply(raw) == expected


@pytest.mark.parametrize("raw", ["", "\n---\n", "```python\n```", None])
def test_empty_decoration_is_a_retryable_generation_error(raw):
    with pytest.raises(DomainError, match="模型"):
        prepare_dialogue_reply(raw)


class MarkdownProvider(Provider):
    def generate(self, **kwargs):
        self.calls.append(kwargs)
        return {"text": "# 判断\n**先验证**\n```python\n# comment\nx_y = 2 ** 3\n```", "tokens": 30}


def test_chat_saved_preview_next_context_and_retry_share_exact_prepared_text(tmp_path):
    provider = MarkdownProvider()
    service = WotService(tmp_path, provider)
    try:
        room = service.conversation("格式测试", "skill_munger")["id"]
        user_text = "# 用户原文\n**不能改**\nx_y = 2 ** 3"
        first = service.send(room, user_text, True, "general", "same-request")
        expected = "判断\n先验证\n# comment\nx_y = 2 ** 3"
        assert first["agent"]["text"] == expected
        assert first["user"]["text"] == user_text
        assert service.store.fetchone("SELECT text FROM evidence WHERE id=?", (first["evidence_id"],))["text"] == user_text
        retry = service.send(room, user_text, True, "general", "same-request")
        assert retry["agent"] == first["agent"]
        assert len(provider.calls) == 1
        assert len(service.messages(room)) == 2
        assert service.conversation_summary(service.require(room, "conversation"))["last_message_preview"] == expected.replace("\n", " ")
        service.send(room, "下一句", False, "general", "next-request")
        assert provider.calls[-1]["transcript"][1]["text"] == expected
        # Existing records are never normalized on read/restart.
        with service.store.transaction():
            service.store.add_message(room, "AGENT", "## 历史 **原样**", request_key="legacy")
        assert service.messages(room)[-1]["text"] == "## 历史 **原样**"
    finally:
        service.close()


def test_shared_prompt_requires_style_and_direct_identity_honesty():
    assert "不主动提 AI" in DIALOGUE_STYLE_SYSTEM
    assert "直接询问身份" in DIALOGUE_STYLE_SYSTEM
    assert "如实" in DIALOGUE_STYLE_SYSTEM
    assert "不用 Markdown" in DIALOGUE_STYLE_SYSTEM
