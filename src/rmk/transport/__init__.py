"""Transport layer: how rmk reaches the tablet's files.

``Transport`` is the abstract surface the rest of rmk talks to. ``SSHTransport``
implements it over USB/Wi-Fi SSH today; a cloud implementation can slot in later
without touching the library/render/LLM code.
"""

from .base import Transport
from .ssh import SSHTransport

__all__ = ["Transport", "SSHTransport"]
