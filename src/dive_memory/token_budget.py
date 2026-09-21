from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol


class TokenCounter(Protocol):
    name: str
    exact: bool

    def count(self, text: str) -> int: ...


@dataclass(frozen=True, slots=True)
class ApproximateTokenCounter:
    name: str = "utf8-character-heuristic-v1"
    exact: bool = False

    def count(self, text: str) -> int:
        return 0 if not text else max(1, len(text) // 4)


@dataclass(frozen=True, slots=True)
class CallableTokenCounter:
    name: str
    function: Callable[[str], int]
    exact: bool = True

    def count(self, text: str) -> int:
        count = self.function(text)
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError("token counter must return a non-negative integer")
        return count
