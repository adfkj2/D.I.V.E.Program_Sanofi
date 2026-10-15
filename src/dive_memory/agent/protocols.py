"""Small ports: orchestration never imports storage, UI or benchmark readers."""

from typing import Protocol


class ModelError(Exception):
    """Sanitized provider failure suitable for display and persistence."""


class ChatModel(Protocol):
    def complete(self, messages: list[dict], tools: list[dict]) -> dict: ...


class ProjectTools(Protocol):
    def context(self, project: str) -> dict: ...

    def read(self, project: str, name: str, arguments: dict) -> dict: ...

    def validate_actions(self, project: str, actions: list[dict]) -> list[dict]: ...
