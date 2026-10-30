"""Lexical helpers for the SQLite FTS projection.

FTS5's default ``unicode61`` tokenizer treats a whole run of CJK characters as a
single token, because CJK ideographs are "alphanumeric" in the Unicode sense and
there is no whitespace to split on. A stored fact such as
``preference: 咖啡并且每天早上喝咖啡`` therefore becomes the token
``咖啡并且每天早上喝咖啡`` and can never be matched by the query ``咖啡`` — the
memory is written but unreachable.

Indexing the character bigrams of every CJK run alongside the raw text keeps
those memories reachable without pulling in a segmentation dependency. Latin
text is unaffected: it already tokenises on whitespace and punctuation.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable

# CJK ideographs plus the kana ranges, which tokenise the same way.
CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u309f\u30a0-\u30ff]+")


def cjk_bigrams(text: str) -> list[str]:
    """Character bigrams for every CJK run; single characters are kept whole."""
    grams: list[str] = []
    for run in CJK_RUN.findall(text):
        if len(run) == 1:
            grams.append(run)
            continue
        grams.extend(run[index : index + 2] for index in range(len(run) - 1))
    return grams


def _dedupe(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))


def index_document(text: str) -> str:
    """Value stored in the FTS columns: the text plus its CJK bigrams."""
    grams = cjk_bigrams(text)
    if not grams:
        return text
    return text + " " + " ".join(_dedupe(grams))


def structured_document(structured: dict) -> str:
    """Searchable form of ``structured_content``.

    ``ensure_ascii=False`` matters: the default escaping turns 咖啡 into
    ``\\u5496\\u5561``, which leaves no CJK characters for the tokenizer to see.
    """
    return index_document(json.dumps(structured, ensure_ascii=False))


def query_terms(query: str) -> list[str]:
    """FTS terms for a user query: whitespace tokens plus CJK bigrams."""
    lowered = query.lower()
    terms = [token for token in lowered.split() if token]
    for run in CJK_RUN.findall(lowered):
        if len(run) == 1:
            terms.append(run)
            continue
        terms.extend(run[index : index + 2] for index in range(len(run) - 1))
    return _dedupe(terms)


def match_expression(query: str) -> str | None:
    """Build a safe FTS5 ``MATCH`` expression, or ``None`` when there is none.

    Terms are quoted, so FTS5 treats them literally; embedded quotes are
    stripped rather than escaped because a partially quoted expression would
    raise ``OperationalError`` and lose the lexical channel entirely.
    """
    lowered = query.lower()
    groups: list[str] = []

    # A multi-character CJK query must match every adjacent bigram in that
    # run. Joining every bigram with OR made one common pair (for example
    # "每天") enough to retrieve an unrelated sentence.
    for run in CJK_RUN.findall(lowered):
        terms = cjk_bigrams(run)
        quoted = ['"' + term.replace('"', "") + '"' for term in _dedupe(terms) if term]
        if quoted:
            groups.append(" AND ".join(quoted))

    # Keep ordinary Latin/number tokens as independent alternatives. CJK runs
    # are removed first so the full unsegmented sentence is not added again.
    non_cjk = CJK_RUN.sub(" ", lowered)
    for term in non_cjk.split():
        cleaned = term.replace('"', "").strip()
        if cleaned:
            groups.append('"' + cleaned + '"')
    return " OR ".join(f"({group})" for group in groups) if groups else None
