"""Local-filesystem transport.

Reads notebooks straight off a local xochitl-format directory — most usefully
the reMarkable **desktop app**'s synced store, e.g.:

    ~/Library/Containers/com.remarkable.desktop/Data/Library/Application Support/remarkable/desktop

Same file layout as the tablet (``<uuid>.metadata`` / ``.content`` / ``<uuid>/``
of ``.rm`` pages), so the library and render layers work unchanged — no SSH, no
device, no subscription. Point ``[remarkable].root`` at that directory and set
``transport = "local"``.
"""

from __future__ import annotations

import os
from pathlib import Path


class LocalTransport:
    def connect(self) -> None:  # nothing to connect
        pass

    def close(self) -> None:
        pass

    def __enter__(self) -> "LocalTransport":
        return self

    def __exit__(self, *exc) -> None:
        pass

    def listdir(self, path: str) -> list[str]:
        return os.listdir(path)

    def read_bytes(self, path: str) -> bytes:
        return Path(path).read_bytes()

    def read_many(self, directory: str, suffixes: tuple[str, ...]) -> dict[str, bytes]:
        out: dict[str, bytes] = {}
        for name in os.listdir(directory):
            if name.endswith(suffixes):
                fp = os.path.join(directory, name)
                if os.path.isfile(fp):
                    out[name] = Path(fp).read_bytes()
        return out
