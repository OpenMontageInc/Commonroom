"""User settings stored as settings.json in the data folder. Never holds secrets."""
import getpass
import json
import os
import threading
from pathlib import Path

NAME_LIMIT = 80


def _os_user():
    try:
        return getpass.getuser().strip() or "You"
    except Exception:
        return "You"


def _name(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Enter a {label}.")
    if len(value.strip()) > NAME_LIMIT:
        raise ValueError(f"Keep the {label} under {NAME_LIMIT} characters.")
    return value.strip()


def _lead(value, valid_providers):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("Choose a lead agent or none.")
    provider = value.get("provider")
    if provider not in set(valid_providers):
        raise ValueError("Choose an available provider for the lead agent.")
    model = value.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("Choose a model for the lead agent.")
    effort = value.get("effort") or ""
    if not isinstance(effort, str):
        raise ValueError("Choose an effort level for the lead agent.")
    return {"name": _name(value.get("name"), "lead agent name"), "provider": provider,
            "model": model.strip(), "effort": effort.strip()}


class Settings:
    def __init__(self, path, provider_ids=None):
        """``provider_ids`` returns the registered provider ids; a stored lead naming another is ignored."""
        self.path = Path(path)
        self.provider_ids = provider_ids
        self.lock = threading.Lock()

    def defaults(self):
        return {"displayName": _os_user(), "lead": None, "setupComplete": False}

    def _read(self):
        data = self.defaults()
        try:
            stored = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return data
        if isinstance(stored, dict):
            if isinstance(stored.get("displayName"), str) and stored["displayName"].strip():
                data["displayName"] = stored["displayName"].strip()
            data["lead"] = self._stored_lead(stored.get("lead"))
            if isinstance(stored.get("setupComplete"), bool):
                data["setupComplete"] = stored["setupComplete"]
        return data

    def _stored_lead(self, value):
        """A hand-edited or malformed lead in settings.json counts as no lead, never as a crash."""
        if not isinstance(value, dict):
            return None
        provider = value.get("provider")
        valid = self.provider_ids() if self.provider_ids else ((provider,) if isinstance(provider, str) and provider else ())
        try:
            return _lead(value, valid)
        except (ValueError, TypeError):
            return None

    def get(self):
        with self.lock:
            return self._read()

    def update(self, partial, valid_providers):
        if not isinstance(partial, dict):
            raise ValueError("Expected a JSON object.")
        unknown = set(partial) - {"displayName", "lead", "setupComplete"}
        if unknown:
            raise ValueError(f"Unknown setting: {sorted(unknown)[0]}")
        changes = {}
        if "displayName" in partial:
            changes["displayName"] = _name(partial["displayName"], "display name")
        if "lead" in partial:
            changes["lead"] = _lead(partial["lead"], valid_providers)
        if "setupComplete" in partial:
            if not isinstance(partial["setupComplete"], bool):
                raise ValueError("setupComplete must be true or false.")
            changes["setupComplete"] = partial["setupComplete"]
        with self.lock:
            data = {**self._read(), **changes}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_name(self.path.name + ".tmp")
            temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temp, self.path)
            return data
