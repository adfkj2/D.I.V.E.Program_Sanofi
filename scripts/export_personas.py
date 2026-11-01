"""Export existing, source-reviewed voice data. No provider/credential access."""

import argparse
import json
from pathlib import Path

from wot.catalog import builtin_people
from wot.voice import RIGHTS_NOTICE, encode_voice, packaged_voice, voice_display


def export(directory):
    metadata = {p["slug"]: p for p in builtin_people()}
    metadata["fengge-wangmingtianya"] = {
        "display_name": "峰哥亡命天涯", "category": "CURATED",
        "description": "直播闲聊为主，接话、反问、郑重其事的自夸与自嘲；依据可定位语料整理，拟真待试聊。",
        "permission": "表达档案的归纳与原创示范可用于本地测试和修改。人物与原文未获本人授权，原始来源版权归权利人；资料不授予工具、凭据及私人信息访问权限。",
        "source": "2026-10-31；voice-v1.0.0；社区直播转录、访谈与本人文字，未逐音核验。",
    }
    for slug, person in metadata.items():
        profile = packaged_voice(slug)
        output = directory / slug
        output.mkdir(parents=True, exist_ok=True)
        text = encode_voice(profile)
        markdown = voice_display(profile)
        if max(len(text.encode()), len(markdown.encode())) > 64000:
            raise ValueError(f"{slug}: import document exceeds 64KB")
        doc = {key: person[key] for key in ("display_name", "description", "category", "permission", "source")}
        if RIGHTS_NOTICE not in doc["permission"]:
            doc["permission"] += RIGHTS_NOTICE
        doc["source"] += "；表达语料：" + "；".join(s["url"] for s in profile["sources"])
        doc["text"] = text
        name = "fengge" if slug == "fengge-wangmingtianya" else slug
        (output / (name + ".import.json")).write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (output / "SKILL.md").write_text(markdown, encoding="utf-8")
    return len(metadata)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("artifacts/persona-imports"))
    args = parser.parse_args()
    print(f"Exported {export(args.output)} unvalidated persona documents; model calls: 0")
