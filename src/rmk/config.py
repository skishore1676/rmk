"""Configuration loading for rmk.

Config lives at ``~/.config/rmk/config.toml`` (via platformdirs). Environment
variables override the file so secrets never have to be written to disk:

    ANTHROPIC_API_KEY   the Claude API key (never stored in the config file)
    RMK_SSH_HOST        override [ssh].host
    RMK_SSH_PASSWORD    override [ssh].password
    RMK_MODEL           override [llm].model
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import platformdirs

# Where handwritten notebooks live on a reMarkable 2 (xochitl store).
DEFAULT_XOCHITL_ROOT = "/home/root/.local/share/remarkable/xochitl"
DEFAULT_MODEL = "claude-opus-4-8"


def config_path() -> Path:
    return Path(platformdirs.user_config_dir("rmk")) / "config.toml"


@dataclass
class SSHConfig:
    host: str = "10.11.99.1"  # the USB ethernet address of the tablet
    user: str = "root"
    port: int = 22
    password: str | None = None
    key_path: str | None = None


@dataclass
class LLMConfig:
    model: str = DEFAULT_MODEL
    api_key: str | None = None  # falls back to ANTHROPIC_API_KEY env var
    max_tokens: int = 16000


@dataclass
class Config:
    ssh: SSHConfig = field(default_factory=SSHConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    root: str = DEFAULT_XOCHITL_ROOT

    @classmethod
    def load(cls) -> "Config":
        cfg = cls()
        path = config_path()
        if path.exists():
            data = tomllib.loads(path.read_text())
            _apply(cfg.ssh, data.get("ssh", {}))
            _apply(cfg.llm, data.get("llm", {}))
            rm = data.get("remarkable", {})
            if "root" in rm:
                cfg.root = rm["root"]

        # Environment overrides (secrets and quick tweaks).
        cfg.llm.api_key = os.environ.get("ANTHROPIC_API_KEY", cfg.llm.api_key)
        if v := os.environ.get("RMK_SSH_HOST"):
            cfg.ssh.host = v
        if v := os.environ.get("RMK_SSH_PASSWORD"):
            cfg.ssh.password = v
        if v := os.environ.get("RMK_MODEL"):
            cfg.llm.model = v
        return cfg


def _apply(obj, data: dict) -> None:
    for k, v in data.items():
        if hasattr(obj, k):
            setattr(obj, k, v)


CONFIG_TEMPLATE = """\
# rmk configuration — see `rmk doctor` to verify it works.

[ssh]
# reMarkable 2 over USB is reachable at 10.11.99.1. Over Wi-Fi, use its IP.
host = "10.11.99.1"
user = "root"
port = 22
# The SSH password is shown ON THE TABLET at:
#   Settings -> Help -> Copyrights and licenses  (the "GPLv3 Compliance" screen)
# It looks like a short random string. Paste it here, OR set up key auth and
# use key_path instead (then you can delete the password line).
password = ""
# key_path = "~/.ssh/id_rsa"

[llm]
model = "claude-opus-4-8"
max_tokens = 16000
# The API key is read from the ANTHROPIC_API_KEY environment variable.

[remarkable]
root = "/home/root/.local/share/remarkable/xochitl"
"""
