"""The providers Commonroom knows about, in display order."""
from __future__ import annotations

from typing import Iterable, Iterator

from ..platform.base import Platform
from .antigravity import AntigravityProvider
from .base import Provider
from .claude import ClaudeProvider
from .codex import CodexProvider

#: Add a provider class here (and nowhere else) to make it available everywhere.
BUILTIN_PROVIDERS = (CodexProvider, ClaudeProvider, AntigravityProvider)


class ProviderRegistry:
    def __init__(self, providers: Iterable[Provider]):
        self._providers: dict[str, Provider] = {}
        for provider in providers:
            if provider.id in self._providers:
                raise ValueError(f"Two providers use the id {provider.id!r}.")
            self._providers[provider.id] = provider

    def get(self, provider_id: str) -> Provider:
        try:
            return self._providers[provider_id]
        except (KeyError, TypeError):
            raise ValueError(f"Choose an available provider. '{provider_id}' is not supported by this version of Commonroom.") from None

    def ids(self) -> tuple[str, ...]:
        return tuple(self._providers)

    def __iter__(self) -> Iterator[Provider]:
        return iter(tuple(self._providers.values()))

    def __contains__(self, provider_id) -> bool:
        try:
            return provider_id in self._providers
        except TypeError:
            return False

    def __len__(self) -> int:
        return len(self._providers)


def build_registry(platform: Platform, provider_classes=BUILTIN_PROVIDERS) -> ProviderRegistry:
    return ProviderRegistry(cls(platform) for cls in provider_classes)
