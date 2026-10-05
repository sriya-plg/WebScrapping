"""BrowserProvider abstraction. Adapters only use this surface, so the engine is swappable per carrier."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

DEFAULT_BLOCK_MARKERS = ["just a moment", "verify you are human", "attention required", "cf-challenge",
                         "checking your browser", "access denied", "unusual traffic"]


@dataclass
class CapturedResponse:
    url: str
    status: int
    json: Any | None


class BrowserSession(ABC):
    @abstractmethod
    async def goto(self, url: str) -> None: ...
    @abstractmethod
    async def fill(self, selector: str, value: str, human: bool = True) -> None: ...
    @abstractmethod
    async def click(self, selector: str) -> None: ...
    @abstractmethod
    async def select_option(self, selector: str, value: str) -> None: ...
    @abstractmethod
    async def wait_for_selector(self, selector: str, timeout_s: float = 20) -> bool: ...
    @abstractmethod
    async def capture_json(self, url_regex: str, action: Callable[[], Awaitable[Any]],
                           timeout_s: float = 30) -> list[CapturedResponse]:
        """Run `action` (a click/navigation) and return JSON responses the browser received whose URL matches."""
    @abstractmethod
    async def looks_blocked(self, markers: list[str] | None = None) -> bool: ...
    @abstractmethod
    async def save_state(self) -> None: ...
    @abstractmethod
    async def close(self) -> None: ...


class BrowserProvider(ABC):
    @abstractmethod
    async def open_session(self, key: str) -> BrowserSession:
        """`key` = stable id (carrier[:account]) used to persist/reuse cookies between runs."""
    @abstractmethod
    async def close(self) -> None: ...
