"""Adapter registry. Discovery is by FOLDER: app/clients/<client>/carriers/<carrier>/adapter.py -- the
CarrierAdapter subclass defined there is registered for (client, CARRIER). No decorators needed."""
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


def discover() -> None:
    import app.clients as pkg
    from app.core.adapter import CarrierAdapter
    for m in pkgutil.walk_packages(pkg.__path__, "app.clients."):
        parts = m.name.split(".")          # app clients <client> carriers <carrier> adapter
        if len(parts) == 6 and parts[3] == "carriers" and parts[5] == "adapter":
            mod = importlib.import_module(m.name)
            for obj in vars(mod).values():
                if isinstance(obj, type) and issubclass(obj, CarrierAdapter) and obj is not CarrierAdapter \
                        and obj.__module__ == m.name:
                    _REG[(parts[2].upper(), parts[4].upper())] = obj


def get_adapter_class(code: str, client: str | None = None) -> type:
    for k in ((client.upper() if client else None, code.upper()), (None, code.upper())):
        if k in _REG:
            return _REG[k]
    raise KeyError(f"no adapter for client={client!r} carrier={code!r}; known: {sorted(_REG, key=str)}")
