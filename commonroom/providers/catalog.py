"""Discover the models each provider offers, in the background and independently per provider."""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Iterable, Optional

from .registry import ProviderRegistry

DISCOVERY_TIMEOUT = 20


class ModelCatalog:
    def __init__(self, registry: ProviderRegistry, workspace):
        self.registry = registry
        self.workspace = Path(workspace).resolve()
        self._lock = threading.Lock()
        self._models: dict[str, list[dict]] = {provider_id: [] for provider_id in registry.ids()}
        self._errors: dict[str, str] = {}
        self._loading: set[str] = set()
        self._discovered: set[str] = set()  # ids whose discovery has finished at least once

    def snapshot(self) -> dict:
        with self._lock:
            return {"models": {key: list(value) for key, value in self._models.items()}, "errors": self._errors.copy(),
                    "loading": [provider_id for provider_id in self.registry.ids() if provider_id in self._loading]}

    def discovered(self) -> set[str]:
        """Provider ids whose model discovery has finished (with models or an error) at least once."""
        with self._lock:
            return set(self._discovered)

    def refresh(self, provider: Optional[str] = None, *, failed_only: bool = False,
                providers: Optional[Iterable[str]] = None) -> None:
        """Start discovery. ``providers`` limits which ids may load (e.g. only installed ones)."""
        if provider is not None and provider not in self.registry:
            raise ValueError("Choose a provider from the model catalog.")
        allowed = None if providers is None else set(providers)
        with self._lock:
            keys = (provider,) if provider else tuple(self._errors) if failed_only else self.registry.ids()
            for key in keys:
                if (allowed is None or key in allowed) and key not in self._loading:
                    self._errors.pop(key, None)
                    self._loading.add(key)
                    threading.Thread(target=self._load, args=(key,), daemon=True).start()

    def _load(self, provider_id: str) -> None:
        cancel = threading.Event()
        timer = threading.Timer(DISCOVERY_TIMEOUT, cancel.set)
        timer.daemon = True
        timer.start()
        try:
            discovered = self.registry.get(provider_id).list_models(self.workspace, cancel)
            if not discovered:
                raise RuntimeError("The provider did not advertise any models.")
            models = {}
            for model in discovered:
                if not isinstance(model.id, str) or not model.id:
                    raise ValueError("The provider returned a model without an ID.")
                models[model.id] = model.to_dict()
        except Exception as exc:
            if cancel.is_set() or isinstance(exc, TimeoutError):
                error = "Model discovery timed out. Retry or enter the model manually."
            elif isinstance(exc, (ValueError, TypeError, KeyError, StopIteration, AttributeError)):
                error = "The provider returned an unexpected model catalog. Retry or enter the model manually."
            else:
                error = str(exc).strip() or "The provider could not load its models."
            with self._lock:
                self._errors[provider_id] = error
                self._loading.discard(provider_id)
                self._discovered.add(provider_id)
        else:
            with self._lock:
                self._models[provider_id] = list(models.values())
                self._loading.discard(provider_id)
                self._discovered.add(provider_id)
        finally:
            timer.cancel()
