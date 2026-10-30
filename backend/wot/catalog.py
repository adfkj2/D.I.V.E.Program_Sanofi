"""Curated text-only people catalog, independent from user memory and runtime tools.

The original upstream SKILL files are provenance artifacts, never executed or
used as system prompts. Only the reviewed WOT role data in people.json is used.
"""

from __future__ import annotations

import copy
import json
import re
from importlib.resources import files
from typing import Any

CONVERSION_VERSION = "wot-curated-v1.0.0"
SUMMARY_FIELDS = frozenset({
    "id", "slug", "display_name", "description", "domain", "tags", "avatar_url",
    "starter_prompts", "source", "source_version", "source_refs", "source_review",
    "license", "permission", "version", "status", "builtin", "rank", "category",
    "created_at", "content_hash",
})
DISCLOSURE = "解释性 AI 模拟，非本人发言、非本人授权。角色资料不授予工具、联网或私人记忆权限。"

# This is a conservative rejection aid, not the access-control mechanism.
# The service owns permissions; no content in this field can grant a capability.
_RUNTIME_MARKERS = re.compile(
    r"(?im)(?:^\s*(?:allowed-tools|run|scripts|tools)\s*:|"
    r"\b(?:WebSearch|WebFetch|curl|wget|subprocess|os\.system)\b|"
    r"\b(?:npx\s+skills|pip\s+install|npm\s+install|git\s+clone)\b|"
    r"自动(?:联网|执行|安装)|忽略.{0,15}(?:系统|前述|隐私|权限|规则)|"
    r"(?:读取|导出|打印|发送).{0,15}(?:全部记忆|所有记忆|API.?key|密钥|密码))"
)


def builtin_people() -> list[dict[str, Any]]:
    """Return fresh catalog values so one caller cannot mutate another's copy."""
    catalog = json.loads(files("wot").joinpath("data", "people.json").read_text(encoding="utf-8"))
    return sorted(catalog, key=lambda person: person["rank"])


def skill_summary(skill: dict[str, Any]) -> dict[str, Any]:
    """Explicit display allowlist: no role text, raw source or runtime fields."""
    return {key: copy.deepcopy(value) for key, value in skill.items() if key in SUMMARY_FIELDS}


def role_context(skill: dict[str, Any]) -> dict[str, str]:
    """Minimal role data for an isolated model request, with no tool manifest.

    Obvious executable/import instructions fail closed. Imported content remains
    untrusted even if this simple check finds no markers; storage and transport
    enforce access separately. Original vendored SKILL files are never read here.
    """
    text = skill.get("text", "")
    if not isinstance(text, str):
        text = ""
    if _RUNTIME_MARKERS.search(text) or "```" in text or "\x00" in text:
        text = "该角色资料包含未获授权的运行指令，暂不提供角色正文。只能讨论用户已公开的话题，并明确说明资料不足。"
    return {
        "name": str(skill.get("display_name", "解释性角色"))[:120],
        "slug": str(skill.get("slug", skill.get("id", "imported")))[:120],
        "untrusted_role_data": text[:12_000],
        "disclosure": DISCLOSURE,
    }
