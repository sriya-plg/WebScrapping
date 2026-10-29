"""Dependencies shared by the runner; intentionally contains no persistence layer."""
from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class Services:
    settings: Any
    backend: Any
    queue: Any
    provider_factory: Callable
