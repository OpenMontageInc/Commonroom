"""Machine scan for the setup screen: OS, Python, and which providers are installed."""
from __future__ import annotations

import copy
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

from .platform.base import Platform
from .providers.base import Detection, Provider
from .providers.registry import ProviderRegistry


def _detect(provider: Provider) -> Detection:
    try:
        return provider.detect()
    except Exception as exc:  # One broken provider must not break the whole scan.
        return Detection(provider.id, provider.label, False, provider.install_url, error=str(exc) or type(exc).__name__)


class MachineScan:
    def __init__(self, platform: Platform, registry: ProviderRegistry):
        self.platform = platform
        self.registry = registry
        self._lock = threading.Lock()
        self._result: dict | None = None

    def result(self) -> dict:
        """The last scan, scanning first if none has run yet."""
        with self._lock:
            if self._result is None:
                self._result = self._scan()
            return copy.deepcopy(self._result)

    def peek(self) -> dict | None:
        """The last finished scan without waiting; None while the first scan is still running."""
        result = self._result
        return copy.deepcopy(result) if result is not None else None

    def rescan(self) -> dict:
        with self._lock:
            self.platform.refresh()  # pick up tools installed since the last scan
            self._result = self._scan()
            return copy.deepcopy(self._result)

    def installed_ids(self) -> tuple[str, ...]:
        return tuple(item["id"] for item in self.result()["providers"] if item["installed"])

    def _scan(self) -> dict:
        providers = list(self.registry)
        detections = []
        if providers:
            with ThreadPoolExecutor(max_workers=len(providers), thread_name_prefix="commonroom-scan") as pool:
                detections = list(pool.map(_detect, providers))
        version = sys.version_info
        return {"platform": self.platform.describe(),
                "python": {"version": f"{version.major}.{version.minor}.{version.micro}"},
                "providers": [detection.to_dict() for detection in detections]}
