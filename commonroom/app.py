"""Composition root: the only module that knows the concrete platform and providers."""
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

from . import __version__
from .core.projects import ProjectRooms
from .core.room import Room
from .core.settings import Settings

PACKAGE = Path(__file__).resolve().parent


@contextmanager
def own_data_folder(data, platform):
    """Hold the data folder's single-service lock (raises DataFolderInUse when taken)."""
    data = Path(data)
    data.mkdir(parents=True, exist_ok=True)
    with (data / "room.lock").open("a+b") as lock:
        platform.lock_exclusive(lock)
        yield


class App:
    version = __version__

    def __init__(self, platform, registry, scan, catalog, settings, projects):
        self.platform, self.registry, self.scan = platform, registry, scan
        self.catalog, self.settings, self.projects = catalog, settings, projects

    def setup(self):
        """The machine scan (platform, Python, providers) plus the user's settings."""
        scan, settings = self.scan.result(), self.settings.get()
        logos = {provider.id: self._logo_url(provider) for provider in self.registry}
        return {"platform": scan.get("platform") or self.platform.describe(),
                "python": scan.get("python") or {"version": sys.version.split()[0]},
                "providers": [dict(item, logo=logos.get(item["id"])) for item in scan["providers"]],
                "settings": settings, "setupComplete": settings["setupComplete"]}

    def providers(self):
        installed = set(self.scan.installed_ids())
        return {"providers": [{"id": p.id, "label": p.label, "installed": p.id in installed, "logo": self._logo_url(p)}
                              for p in self.registry]}

    @staticmethod
    def _logo_url(provider):
        """Where the UI loads a provider's logo, or None when it has none (the UI then draws an initial)."""
        return f"/api/providers/{quote(provider.id, safe='')}/logo.svg" if provider.logo_file() else None

    def provider_logo(self, provider_id):
        """The SVG bytes of a registered provider's own logo, or None. Never reads any other file."""
        try:
            provider = self.registry.get(provider_id)
        except ValueError:
            return None
        path = provider.logo_file()
        return path.read_bytes() if path else None

    def update_settings(self, partial):
        return self.settings.update(partial, self.registry.ids())

    def models(self):
        lead = self.settings.get()["lead"]
        defaults = {lead["provider"]: {"model": lead["model"], "effort": lead.get("effort") or ""}} if lead else {}
        snapshot = self.catalog.snapshot()
        # Until a provider's first discovery finishes, report it as loading so the UI keeps polling
        # instead of showing raw model ids: every provider while the first machine scan runs, then
        # every installed provider not yet discovered. Providers that are not installed never load.
        scan = self.scan.peek()
        if scan is None:
            pending = set(self.registry.ids())
        else:
            installed = {item["id"] for item in scan["providers"] if item["installed"]}
            pending = installed - self.catalog.discovered()
        loading = set(snapshot["loading"]) | pending
        snapshot["loading"] = [provider_id for provider_id in self.registry.ids() if provider_id in loading]
        return {**snapshot, "defaults": defaults}

    def rescan(self):
        """Detect installed providers again, then discover models for the installed ones."""
        self.scan.rescan()
        self.catalog.refresh(providers=list(self.scan.installed_ids()))
        return self.setup()

    def start(self):
        threading.Thread(target=self.rescan, name="commonroom-setup-scan", daemon=True).start()


def build_app(data_dir, initial_project=None, platform=None):
    from .platform import current_platform
    from .providers.catalog import ModelCatalog
    from .providers.registry import build_registry
    from .setup import MachineScan

    platform = platform or current_platform()
    data = Path(data_dir)
    registry = build_registry(platform)
    settings = Settings(data / "settings.json", registry.ids)
    projects = ProjectRooms(data, platform, lambda storage, path: Room(storage, registry, settings, project_path=path),
                            initial_project=initial_project)
    return App(platform, registry, MachineScan(platform, registry), ModelCatalog(registry, workspace=PACKAGE),
               settings, projects)
