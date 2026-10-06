"""BrowserProvider abstraction. Adapters only use this surface, so the engine is swappable per carrier."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

DEFAULT_BLOCK_MARKERS = [
    "just a moment",
    "verify you are human",
    "attention required",
    "cf-challenge",
    "checking your browser",
    "access denied",
    "unusual traffic",
]


class BrowserSession(ABC):
    @abstractmethod
    async def goto(self, url: str) -> None: ...

    @abstractmethod
    async def fill(self, selector: str, value: str, human: bool = True, frame: str | None = None) -> None: ...

    @abstractmethod
    async def click(self, selector: str, frame: str | None = None) -> None: ...

    @abstractmethod
    async def press(self, selector: str, key: str, frame: str | None = None) -> None: ...

    @abstractmethod
    async def select_option(self, selector: str, value: str, frame: str | None = None) -> None: ...

    @abstractmethod
    async def wait_for_selector(self, selector: str, timeout_s: float = 20, frame: str | None = None) -> bool: ...

    @abstractmethod
    async def is_visible(self, selector: str, frame: str | None = None) -> bool: ...

    @abstractmethod
    async def evaluate(self, expression: str, arg: Any = None) -> Any: ...

    @abstractmethod
    async def discover_search_box(self) -> dict | None:
        """Best-guess {"input": css, "submit": css|None} for the page's main search/tracking box."""

    @abstractmethod
    async def content(self) -> str:
        """Return the current page HTML content for Scrapling extraction."""

    @abstractmethod
    async def looks_blocked(self, markers: list[str] | None = None) -> bool: ...

    @abstractmethod
    async def save_state(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...


class BrowserProvider(ABC):
    @abstractmethod
    async def open_session(self, key: str) -> BrowserSession:
        """`key` = stable id (client_carrier) used to persist/reuse cookies between runs."""

    @abstractmethod
    async def close(self) -> None: ...
