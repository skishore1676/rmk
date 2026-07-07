"""Configuration loading for rmk.

Config lives at ``~/.config/rmk/config.toml`` (via platformdirs). Environment
variables override the file:

    RMK_SSH_HOST        override [ssh].host
    RMK_SSH_PASSWORD    override [ssh].password
    RMK_MODEL           override [broker].model

The LLM is reached through the agent broker, whose ``claude`` provider uses the
logged-in ``claude`` CLI — so there is no API key to store here.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import platformdirs

# Where handwritten notebooks live on a reMarkable 2 (xochitl store).
DEFAULT_XOCHITL_ROOT = "/home/root/.local/share/remarkable/xochitl"


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
class BrokerConfig:
    """How rmk hires a brain via ~/code/agent-broker."""

    provider: str = "claude"  # provider id for the explicit-binding path
    model: str = "sonnet"  # broker/claude CLI model alias (e.g. sonnet, opus)
    timeout: int = 600
    # Optional: point at a broker policy file to use your configured provider
    # chain (failover + receipts) instead of a single explicit binding.
    policy_path: str | None = None
    actor: str = "reader"
    role: str = "note_reader"
    lane: str = "rmk"


@dataclass
class Config:
    ssh: SSHConfig = field(default_factory=SSHConfig)
    broker: BrokerConfig = field(default_factory=BrokerConfig)
    # "ssh" reads from the tablet over SSH; "local" reads a local xochitl dir
    # (e.g. the reMarkable desktop app's synced store).
    transport: str = "ssh"
    root: str = DEFAULT_XOCHITL_ROOT

    @classmethod
    def load(cls) -> "Config":
        cfg = cls()
        path = config_path()
        if path.exists():
            data = tomllib.loads(path.read_text())
            _apply(cfg.ssh, data.get("ssh", {}))
            _apply(cfg.broker, data.get("broker", {}))
            rm = data.get("remarkable", {})
            if "root" in rm:
                cfg.root = rm["root"]
            if "transport" in rm:
                cfg.transport = rm["transport"]

        # Environment overrides (secrets and quick tweaks).
        if v := os.environ.get("RMK_SSH_HOST"):
            cfg.ssh.host = v
        if v := os.environ.get("RMK_SSH_PASSWORD"):
            cfg.ssh.password = v
        if v := os.environ.get("RMK_MODEL"):
            cfg.broker.model = v
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
# Paste it here, OR set up key auth and use key_path instead.
password = ""
# key_path = "~/.ssh/id_rsa"

[broker]
# The brain: rmk routes the note through ~/code/agent-broker, which runs the
# logged-in `claude` CLI (no API key needed — just `claude auth status` = ok).
provider = "claude"
model = "sonnet"          # try "opus" for messier handwriting
timeout = 600
# To use your own broker policy chain (failover + receipts) instead of a single
# claude binding, point at a policy file and set the actor/role:
# policy_path = "/Users/suman/code/agent-broker/policies/your_policy.yaml"
# actor = "reader"
# role  = "note_reader"

[remarkable]
# "ssh" = read from the tablet; "local" = read a local xochitl directory such as
# the reMarkable desktop app's synced store (no tablet needed).
transport = "ssh"
root = "/home/root/.local/share/remarkable/xochitl"
# For transport = "local", point root at the desktop app store, e.g.:
# transport = "local"
# root = "/Users/suman/Library/Containers/com.remarkable.desktop/Data/Library/Application Support/remarkable/desktop"
"""
