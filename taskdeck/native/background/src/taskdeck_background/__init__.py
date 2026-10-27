"""TaskDeck background work: an Android foreground service that keeps the app running (and its
network on) while a long task runs, with a progress notification. See TaskService.java."""

from typing import Optional

from flet.controls.base_control import control
from flet.controls.services.service import Service

__all__ = ["BackgroundWork"]


@control("BackgroundWork")
class BackgroundWork(Service):
    async def start(self, title: str, text: str = "", progress: Optional[int] = None) -> bool:
        """Start the service with its ongoing notification. False if Android refused (e.g. the app is
        in the background)."""
        return bool(await self._invoke_method("start", {"title": title, "text": text, "progress": _pct(progress)}))

    async def update(self, title: str, text: str = "", progress: Optional[int] = None) -> None:
        await self._invoke_method("update", {"title": title, "text": text, "progress": _pct(progress)})

    async def stop(self, title: str = "", text: str = "") -> None:
        """Stop the service; with a title, leave a "finished" notification."""
        await self._invoke_method("stop", {"title": title, "text": text})


def _pct(progress: Optional[int]) -> int:
    return -1 if progress is None else max(0, min(100, int(progress)))
