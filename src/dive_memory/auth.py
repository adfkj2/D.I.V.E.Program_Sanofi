from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class NamespaceAuthorizer(Protocol):
    """Authorization boundary used by the provider-neutral HTTP adapter."""

    def authorize(self, token: str, namespace: str, action: str) -> bool: ...


@dataclass(slots=True)
class StaticTokenAuthorizer:
    """Small deployment-ready authorizer for service tokens.

    Each token maps to an explicit set of namespaces. ``"*"`` grants the
    administrative routes that operate across namespaces. Production systems
    can provide the same protocol from an IAM/RBAC service.
    """

    grants: dict[str, set[str]]

    def authorize(self, token: str, namespace: str, action: str) -> bool:
        allowed = self.grants.get(token, set())
        return "*" in allowed or namespace in allowed
