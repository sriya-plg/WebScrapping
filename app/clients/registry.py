"""Client policy registry. It deliberately contains no list of client accounts."""
from __future__ import annotations
from app.clients.base_policy import BaseClientPolicy
from app.clients.policies.default import DefaultClientPolicy


class ClientPolicyRegistry:
    def __init__(self) -> None:
        self._policies: dict[str, type[BaseClientPolicy]] = {}
        self._default = DefaultClientPolicy

    def register(self, client_key: str, policy: type[BaseClientPolicy]) -> None:
        self._policies[client_key.strip().casefold()] = policy

    def create(self, client_key: str | None) -> BaseClientPolicy:
        policy = self._policies.get(str(client_key or "").strip().casefold(), self._default)
        return policy()


client_policy_registry = ClientPolicyRegistry()
