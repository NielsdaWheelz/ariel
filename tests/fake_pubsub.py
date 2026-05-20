"""Plain-Python in-memory fakes for the Pub/Sub subscriber.

The subscriber sidecar (``src/ariel/pubsub_subscriber.py``) calls only a tiny
slice of the Pub/Sub message surface: ``message_id``, ``data``, ``publish_time``,
``ack_with_response()`` (whose return value's ``.result(timeout=...)`` is
awaited), and ``nack()``. This module provides drop-in fakes that record calls
without touching the real Google SDK.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class FakeAckResponse:
    """Future returned by ``ack_with_response()``; ``.result()`` returns SUCCESS."""

    succeeded: bool = True

    def result(self, timeout: float | None = None) -> str:
        del timeout
        return "SUCCESS" if self.succeeded else "PERMISSION_DENIED"


@dataclass
class FakePubSubMessage:
    """In-memory Pub/Sub message matching the subscriber's narrow surface."""

    message_id: str
    data: bytes
    publish_time: datetime
    ack_calls: list[None] = field(default_factory=list)
    nack_calls: list[None] = field(default_factory=list)

    def ack_with_response(self) -> FakeAckResponse:
        self.ack_calls.append(None)
        return FakeAckResponse()

    def nack(self) -> None:
        self.nack_calls.append(None)
