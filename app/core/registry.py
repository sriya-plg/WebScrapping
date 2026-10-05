"""Adapter registry. Most carriers need NO adapter: they use GenericAdapter. A carrier folder may contain an
adapter.py whose CarrierAdapter subclass is auto-registered for (client, carrier) by its FOLDER location."""
from __future__ import annotations

import importlib
import pkgutil

_REG: dict[tuple[str | None, str], type] = {}


def register(*codes: str, client: str | None = None):
    """Manual registration (tests / unusual layouts)."""
    def deco(cls):
        for c in codes:
            _REG[(client.upper() if client else None, c.upper())] = cls
        return cls
    return deco


def discover() -> list[str]:
    """Import every app/clients/<client>/carriers/<carrier>/adapter.py. A broken adapter is reported, not fatal."""
    import app.clients as pkg
    from app.core.adapter import CarrierAdapter
    errors = []
    for m in pkgutil.walk_packages(pkg.__path__, "app.clients."):
        parts = m.name.split(".")          # app clients <client> carriers <carrier> adapter
        if len(parts) == 6 and parts[3] == "carriers" and parts[5] == "adapter":
            try:
                mod = importlib.import_module(m.name)
            except Exception as e:
                errors.append(f"{m.name}: {e}")
                continue
            for obj in vars(mod).values():
                if isinstance(obj, type) and issubclass(obj, CarrierAdapter) and obj.__module__ == m.name:
                    _REG[(parts[2].upper(), parts[4].upper())] = obj
    return errors


def get_adapter_class(code: str, client: str | None = None) -> type:
    for k in ((client.upper() if client else None, code.upper()), (None, code.upper())):
        if k in _REG:
            return _REG[k]
    from app.core.adapter import GenericAdapter
    return GenericAdapter
