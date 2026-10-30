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

    grants: dict[str, set[str] | dict[str, set[str]]]

    def authorize(self, token: str, namespace: str, action: str) -> bool:
        allowed = self.grants.get(token, set())
        # Backward-compatible namespace-only grants authorize every action in
        # that scope. New production configuration should use the nested form.
        if isinstance(allowed, set):
            return "*" in allowed or namespace in allowed
        actions = allowed.get(namespace, set()) | allowed.get("*", set())
        return "*" in actions or action in actions
