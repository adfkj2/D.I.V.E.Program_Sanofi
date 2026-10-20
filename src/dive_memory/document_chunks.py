"""Deterministic, source-offset-preserving passages for document memories.

The canonical memory remains the *whole* source document.  These chunks are a
rebuildable search projection, not new facts or independently writable memories.
Every passage is an exact slice of the canonical text, which lets a reader
trace an excerpt back to both its source event and its character range.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import lexical

DEFAULT_CHUNK_CHARS = 640
DEFAULT_OVERLAP_CHARS = 80


@dataclass(frozen=True, slots=True)
class DocumentChunk:
    index: int
    start: int
    end: int
    content: str


@dataclass(frozen=True, slots=True)
class DocumentChunkHit:
    parent_memory_id: str
    index: int
    start: int
    end: int
    content: str
    score: float


_HEADING = re.compile(r"(?m)^(?:[ \t]{0,3}#{1,6}[ \t]+|[ \t]*\d+(?:\.\d+)*[.)][ \t]+)")
_PARAGRAPH = re.compile(r"\n[ \t]*\n")
_LINE = re.compile(r"\n")
_SENTENCE = re.compile(r"[.!?。！？][\"'”’）)]?(?=\s|$)")
_SPACE = re.compile(r"\s")
_LATIN_WORD = re.compile(r"[a-z0-9]+(?:'[a-z0-9]+)?")
_LATIN_STOP = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "did", "do", "does",
    "for", "from", "had", "has", "have", "how", "i", "in", "is", "it",
    "me", "my", "of", "on", "or", "our", "that", "the", "their", "this",
    "to", "was", "were", "what", "when", "where", "which", "who", "why",
    "with", "you", "your",
})


def _break_at(text: str, start: int, ceiling: int, max_chars: int) -> int:
    """Prefer a section/paragraph boundary near the requested ceiling."""

    floor = start + max(1, int(max_chars * 0.55))
    if floor >= ceiling:
        return ceiling
    window = text[floor:ceiling]
    # A heading starts the *next* passage; a paragraph or sentence terminator
    # ends the current one.  The floor prevents a short early heading from
    # creating many tiny passages.
    heading_starts = [floor + match.start() for match in _HEADING.finditer(window)]
    if heading_starts:
        return heading_starts[-1]
    for pattern in (_PARAGRAPH, _LINE, _SENTENCE, _SPACE):
        boundaries = [floor + match.end() for match in pattern.finditer(window)]
        if boundaries:
            return boundaries[-1]
    return ceiling


def split_document(
    text: str,
    *,
    max_chars: int = DEFAULT_CHUNK_CHARS,
    overlap_chars: int | None = None,
) -> list[DocumentChunk]:
    """Split one original text into bounded, overlapping, exact-slice passages.

    Breaks favour headings and paragraphs, then lines, sentences and spaces.
    ``overlap_chars`` protects a phrase straddling a boundary; it never changes
    offsets or creates a synthetic passage.  The function reads *only* the text
    it receives, not benchmark answers or any external corpus metadata.
    """

    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    if overlap_chars is None:
        overlap_chars = min(DEFAULT_OVERLAP_CHARS, max_chars // 8)
    if overlap_chars < 0 or overlap_chars >= max_chars:
        raise ValueError("overlap_chars must be non-negative and below max_chars")
    if not text:
        return []
    chunks: list[DocumentChunk] = []
    start = 0
    while start < len(text):
        ceiling = min(len(text), start + max_chars)
        end = len(text) if ceiling == len(text) else _break_at(text, start, ceiling, max_chars)
        if end <= start:
            end = ceiling
        chunks.append(DocumentChunk(len(chunks), start, end, text[start:end]))
        if end == len(text):
            break
        start = max(start + 1, end - overlap_chars)
    return chunks


def _terms(text: str) -> set[str]:
    lowered = text.casefold()
    latin = {word for word in _LATIN_WORD.findall(lowered)
             if len(word) > 1 and word not in _LATIN_STOP}
    return latin | set(lexical.cjk_bigrams(lowered))


def expand_document_span(text: str, start: int, end: int, max_chars: int) -> DocumentChunk:
    """Grow a matched exact span, recovering its paragraph/list-item beginning.

    No archive access: ``text`` is the already-admitted canonical document.
    The match remains contained in the result. Prefer beginning at an actual
    paragraph/line boundary; this prevents an overlap chunk from exposing only
    a list item's explanation while hiding the item's name.
    """
    if not 0 <= start < end <= len(text) or max_chars < end - start:
        raise ValueError("invalid anchor span or insufficient expansion cap")
    if len(text) <= max_chars:
        return DocumentChunk(0, 0, len(text), text)
    spare = max_chars - (end - start)
    left = max(0, start - spare // 2)
    right = min(len(text), left + max_chars)
    left = max(0, right - max_chars)
    # Move the left edge to the nearest preceding boundary that still permits
    # the entire matched span. Fallback stays bounded even for a huge paragraph.
    boundaries = [0, *(m.end() for m in _LINE.finditer(text, max(0, end-max_chars), start))]
    valid = [p for p in boundaries if end - max_chars <= p <= start]
    if valid:
        left = max(valid)
        right = min(len(text), left + max_chars)
    # End on a complete line/sentence if doing so does not remove the anchor.
    endings = [m.end() for m in _SENTENCE.finditer(text, end, right)]
    endings += [m.end() for m in _LINE.finditer(text, end, right)]
    if endings and right < len(text):
        right = max(endings)
    return DocumentChunk(0, left, right, text[left:right])


def select_document_chunk(text: str, query: str, max_chars: int) -> DocumentChunk:
    """Choose the most query-relevant exact passage, with an early fallback.

    The passage is always an exact ``text[start:end]`` slice and never exceeds
    ``max_chars``.  The scoring is deliberately transparent lexical overlap;
    a semantic selector can be evaluated separately without changing this
    deterministic baseline.  Ties and no-match cases favour the earliest span.
    """

    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    if not text:
        return DocumentChunk(0, 0, 0, "")
    chunks = split_document(
        text, max_chars=max_chars, overlap_chars=min(DEFAULT_OVERLAP_CHARS, max_chars // 8),
    )
    asked = _terms(query)
    if not asked:
        return chunks[0]
    seen = [_terms(chunk.content) for chunk in chunks]
    frequency = {term: sum(term in terms for terms in seen) for term in asked}
    scores = [sum(1.0 / frequency[term] for term in asked & terms)
              for terms in seen]
    return max(chunks, key=lambda chunk: (scores[chunk.index], -chunk.index))
