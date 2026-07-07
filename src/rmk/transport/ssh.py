"""SSH/SFTP transport for a reMarkable tablet in developer mode.

Over USB the tablet is at 10.11.99.1 as user ``root``; the password is on the
device under Settings -> Help -> Copyrights and licenses. No subscription is
needed and this is the fastest path.

``read_many`` uses a single ``tar`` stream over SSH to pull all of a directory's
metadata in one round trip (a xochitl store can hold hundreds of files), falling
back to per-file SFTP if the remote ``tar`` is unavailable.
"""

from __future__ import annotations

import io
import os
import shlex
import tarfile

import paramiko


class SSHError(RuntimeError):
    pass


class SSHTransport:
    def __init__(
        self,
        host: str,
        user: str = "root",
        password: str | None = None,
        key_path: str | None = None,
        port: int = 22,
        timeout: float = 10.0,
    ) -> None:
        self.host = host
        self.user = user
        self.password = password or None
        self.key_path = os.path.expanduser(key_path) if key_path else None
        self.port = port
        self.timeout = timeout
        self._client: paramiko.SSHClient | None = None
        self._sftp: paramiko.SFTPClient | None = None

    # -- lifecycle -----------------------------------------------------------
    def connect(self) -> None:
        if self._client is not None:
            return
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        use_key = bool(self.key_path)
        try:
            client.connect(
                hostname=self.host,
                port=self.port,
                username=self.user,
                password=self.password,
                key_filename=self.key_path,
                look_for_keys=use_key,
                allow_agent=use_key,
                timeout=self.timeout,
            )
        except Exception as e:  # noqa: BLE001 — surface a friendly message
            raise SSHError(
                f"Could not SSH to {self.user}@{self.host}:{self.port}: {e}\n"
                "Is the tablet plugged in / on the network, and is the password "
                "in your config correct? (Settings -> Help -> Copyrights and licenses)"
            ) from e
        self._client = client
        self._sftp = client.open_sftp()

    def close(self) -> None:
        if self._sftp is not None:
            self._sftp.close()
            self._sftp = None
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> "SSHTransport":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- reads ---------------------------------------------------------------
    def listdir(self, path: str) -> list[str]:
        assert self._sftp is not None, "not connected"
        return self._sftp.listdir(path)

    def read_bytes(self, path: str) -> bytes:
        assert self._sftp is not None, "not connected"
        with self._sftp.open(path, "rb") as f:
            f.prefetch()
            return f.read()

    def read_many(self, directory: str, suffixes: tuple[str, ...]) -> dict[str, bytes]:
        result = self._read_many_tar(directory, suffixes)
        if result is not None:
            return result
        return self._read_many_sftp(directory, suffixes)

    # -- internals -----------------------------------------------------------
    def _read_many_tar(
        self, directory: str, suffixes: tuple[str, ...]
    ) -> dict[str, bytes] | None:
        assert self._client is not None, "not connected"
        globs = " ".join(f"*{s}" for s in suffixes)
        cmd = f"cd {shlex.quote(directory)} && tar cf - {globs} 2>/dev/null"
        try:
            _, stdout, _ = self._client.exec_command(cmd, timeout=self.timeout)
            data = stdout.read()
        except Exception:  # noqa: BLE001 — fall back to SFTP
            return None
        if not data:
            return None
        try:
            out: dict[str, bytes] = {}
            with tarfile.open(fileobj=io.BytesIO(data)) as tf:
                for member in tf.getmembers():
                    if not member.isfile():
                        continue
                    fh = tf.extractfile(member)
                    if fh is not None:
                        out[os.path.basename(member.name)] = fh.read()
            return out or None
        except tarfile.TarError:
            return None

    def _read_many_sftp(
        self, directory: str, suffixes: tuple[str, ...]
    ) -> dict[str, bytes]:
        out: dict[str, bytes] = {}
        for name in self.listdir(directory):
            if name.endswith(suffixes):
                out[name] = self.read_bytes(f"{directory}/{name}")
        return out
