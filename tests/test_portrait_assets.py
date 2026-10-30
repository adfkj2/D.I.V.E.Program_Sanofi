import hashlib
import json
import struct
from pathlib import Path

from wot.catalog import builtin_people


def test_all_fifteen_portraits_are_distinct_valid_square_pngs_with_prompts():
    root = Path(__file__).parents[1]
    manifest = json.loads((root / "docs/implementation/AVATAR_ASSETS.json").read_text(encoding="utf-8"))
    entries = {item["slug"]: item for item in manifest["entries"]}
    hashes = set()
    assert len(entries) == manifest["count"] == 15
    for person in builtin_people():
        path = root / "frontend/public" / person["avatar_url"].lstrip("/")
        blob = path.read_bytes()
        assert blob[:8] == b"\x89PNG\r\n\x1a\n"
        width, height = struct.unpack(">II", blob[16:24])
        assert width == height and width >= 256
        hashes.add(hashlib.sha256(blob).hexdigest())
        entry = entries[person["slug"]]
        assert entry["prompt"] and entry["source_output_file"].endswith(".png")
        assert (root / entry["saved_path"]) == path
    assert len(hashes) == 15
