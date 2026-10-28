"""Explicit local/offline versus online composition. Credentials stay in memory."""

import os
from pathlib import Path

from ..reading.providers import OpenAICompatibleChatClient
from .readers import create_reader
from .runtime import MemoryApplication


def application_from_environment(db_path: str | None = None) -> MemoryApplication:
    path = str(db_path or os.getenv("DIVE_DB_PATH", "memory.db"))
    if path != ":memory:":
        Path(path).resolve().parent.mkdir(parents=True, exist_ok=True)
    mode = os.getenv("DIVE_MODE", "evidence_only")
    if mode not in {"evidence_only", "live"}:
        raise ValueError("DIVE_MODE must be evidence_only or live")
    reader = None
    if mode == "live":
        endpoint = os.getenv("DIVE_READER_ENDPOINT", "")
        model = os.getenv("DIVE_READER_MODEL", "")
        key = os.getenv("DIVE_READER_API_KEY", "")
        if not all((endpoint, model, key)):
            raise ValueError(
                "live mode requires DIVE_READER_ENDPOINT, DIVE_READER_MODEL and DIVE_READER_API_KEY"
            )
        strategy = os.getenv("DIVE_READER_STRATEGY", "indexed")
        provider = os.getenv("DIVE_READER_PROVIDER", "openai-compatible")
        normal_options = (
            {"thinking": {"type": "disabled"}} if provider == "deepseek" else {}
        )
        normal = OpenAICompatibleChatClient(
            endpoint, key, provider=provider, extra_body=normal_options
        )
        thinking = None
        effort = None
        if strategy in {
            "r40",
            "adaptive",
            "verified",
            "deliberative",
            "deliberative-refs",
            "deliberative-staged",
            "deliberative-grounded",
        }:
            effort = os.getenv("DIVE_REASONING_EFFORT", "low")
            if effort not in {"low", "high", "max"}:
                raise ValueError("DIVE_REASONING_EFFORT must be low, high or max")
            if provider != "deepseek":
                raise ValueError(
                    "environment reasoning preset requires deepseek mode support; inject transports for another provider"
                )
            thinking = OpenAICompatibleChatClient(
                endpoint,
                key,
                provider=provider,
                extra_body={
                    "thinking": {"type": "enabled"},
                    "reasoning_effort": effort,
                },
            )
        reader = create_reader(
            normal,
            thinking,
            provider=provider,
            model=model,
            strategy=strategy,
            reasoning_effort=effort,
        )
    return MemoryApplication(path, reader=reader)
