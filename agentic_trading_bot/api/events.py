"""A tiny in-process pub/sub bus for pushing live updates to WebSocket clients.

Action endpoints and a background poller publish events (regime changes, P&L and
position updates, new audit entries, learning events). Each connected WebSocket
subscribes to its own bounded queue. The bus carries notifications only; it never
touches the risk gate or order path.
"""
from __future__ import annotations

import asyncio
from typing import Any


class EventBus:
    """Fan-out of JSON-serializable event dicts to per-subscriber queues."""

    def __init__(self, max_queue: int = 256) -> None:
        """Create an empty bus. `max_queue` bounds each subscriber's backlog."""
        self._subscribers: set[asyncio.Queue] = set()
        self._max_queue = max_queue

    def subscribe(self) -> asyncio.Queue:
        """Register and return a new subscriber queue."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=self._max_queue)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        """Remove a subscriber queue."""
        self._subscribers.discard(queue)

    def publish(self, event_type: str, payload: dict[str, Any]) -> None:
        """Publish an event to every subscriber. Drops to slow consumers.

        Non-blocking: a subscriber whose queue is full loses this event rather
        than stalling the publisher. Updates are periodic, so a dropped frame is
        replaced by the next poll.
        """
        event = {"type": event_type, **payload}
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                continue

    @property
    def subscriber_count(self) -> int:
        """Number of currently connected subscribers."""
        return len(self._subscribers)
