"""The transport contract.

Everything above the transport (library, render) only needs to *read* files from
the tablet's document store, so the surface is deliberately tiny. A future
``CloudTransport`` implements the same four methods against the reMarkable Cloud
API and everything else keeps working.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Transport(Protocol):
    def connect(self) -> None:
        """Establish the connection (idempotent)."""

    def close(self) -> None:
        """Tear down the connection."""

    def listdir(self, path: str) -> list[str]:
        """Return the entry names directly under ``path``."""

    def read_bytes(self, path: str) -> bytes:
        """Return the full contents of the remote file at ``path``."""

    def read_many(self, directory: str, suffixes: tuple[str, ...]) -> dict[str, bytes]:
        """Bulk-read every file in ``directory`` whose name ends with one of
        ``suffixes``. Implementations may optimise this into a single round trip;
        the return maps filename -> bytes."""
