"""One app-owned intake cycle and its external-app status/control boundary."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

from .capture import CaptureError, CaptureStore, _atomic_write, _private_dir
from .config import Config, config_path
from .intake import Intake, broker_reader
from .snapshot import checked_id, digest, read_snapshot
from .transport.local import LocalTransport

SCHEMA = "rmk.runtime.v1"
LABEL = "ai.remarkable.intake"
ORIGINS = {"https://github.com/skishore1676/pulsar-workspace.git", "git@github.com:skishore1676/pulsar-workspace.git"}


def now():
    return datetime.now(timezone.utc).isoformat()


def runtime_path():
    return config_path().parent / "runtime.json"


def command(*args, cwd=None, timeout=60, strip=True):
    result = subprocess.run(list(args), cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        # Preserve diagnostics locally without reflecting auth/error bodies.
        raise CaptureError(f"{Path(args[0]).name} command failed (exit {result.returncode})")
    return result.stdout.strip() if strip else result.stdout


class Runtime:
    def __init__(self, path: Path, cfg=None):
        self.path = path.expanduser().resolve()
        self.settings = json.loads(self.path.read_text())
        s = self.settings
        if s["schema"] != SCHEMA or not isinstance(s["paused"], bool):
            raise CaptureError("Invalid runtime configuration")
        checked_id(s["notebook_id"])
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,100}", s["project"]):
            raise CaptureError("Invalid project slug")
        for pid in s["baseline_pages"]:
            checked_id(pid)
        if not isinstance(s["max_storage_bytes"], int) or s["max_storage_bytes"] <= 0:
            raise CaptureError("Invalid private storage bound")
        ZoneInfo(s["timezone"])
        if not 0 <= s["hour"] <= 23 or not 0 <= s["minute"] <= 59:
            raise CaptureError("Invalid daily capture time")
        self.cfg = cfg or Config.load()
        if self.cfg.transport != "local":
            raise CaptureError("Runtime requires the approved local desktop source")
        self.store = CaptureStore(Path(s["state_dir"]), LocalTransport(), self.cfg.root, s["notebook_id"])
        self.intake = Intake(self.store)
        self.workspace = Path(s["workspace"]).expanduser().resolve()
        if command("git", "remote", "get-url", "origin", cwd=self.workspace) not in ORIGINS:
            raise CaptureError("Canonical workspace origin required")
        self.receipt_path = self.store.directory / "runtime-receipt.json"
        self.publication_path = self.store.directory / "publication.json"

    @contextmanager
    def lock(self):
        _private_dir(self.store.directory)
        with (self.store.directory / ".refresh.lock").open("a") as file:
            file.flush(); os.chmod(file.name, 0o600)
            try:
                fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise CaptureError("An intake cycle already owns this notebook") from None
            try:
                yield
            finally:
                fcntl.flock(file, fcntl.LOCK_UN)

    def _clean(self, allowed=()):
        changed = command("git", "status", "--porcelain", "--untracked-files=all", "-z", cwd=self.workspace, strip=False)
        paths = [entry[3:] for entry in changed.split("\0") if entry]
        if any(path not in allowed for path in paths):
            raise CaptureError("Publication checkout has edits; preserve and reconcile")
        return not paths

    def _main(self):
        command("git", "fetch", "origin", "main", cwd=self.workspace)
        return command("git", "rev-parse", "origin/main", cwd=self.workspace)

    def _published(self, staged, revision):
        for path in staged["paths"]:
            result = subprocess.run(["git", "show", f"{revision}:{path['path']}"], cwd=self.workspace, capture_output=True, timeout=60)
            if result.returncode or digest(result.stdout) != path["sha256"]:
                return False
        return True

    def _publish(self, staged):
        if not staged["paths"]:
            return {"status": "no_extracts"}
        revision = self._main()
        pending = json.loads(self.publication_path.read_text()) if self.publication_path.exists() else None
        if pending and pending.get("status") == "confirmed":
            pending = None
        batch = digest(json.dumps(staged["paths"], sort_keys=True).encode())
        if pending and pending["batch"] != batch:
            raise CaptureError("Resolve the previous publication before a different batch")
        if self._published(staged, revision):
            result = self.intake.confirm(self.workspace, self.settings["project"], revision)
            if pending:
                _atomic_write(self.publication_path, {**pending, "status": "confirmed", "workspace_revision": revision})
            return {**result, "pr": pending["pr"] if pending else None}
        branch = f"codex/project-update-{self.settings['project']}-rmk-{batch[:16]}"
        if pending is None:
            command("git", "add", "--", *[p["path"] for p in staged["paths"]], cwd=self.workspace)
            command("git", "diff", "--cached", "--check", cwd=self.workspace)
            if command("git", "diff", "--cached", "--name-only", cwd=self.workspace):
                command("git", "commit", "-m", "Intake updated Remarkable thoughts", cwd=self.workspace)
            pending = {"batch": batch, "staged": staged, "branch": branch,
                       "commit": command("git", "rev-parse", "HEAD", cwd=self.workspace), "pr": None}
            _atomic_write(self.publication_path, pending)
        command("git", "push", "origin", f"HEAD:refs/heads/{branch}", cwd=self.workspace)
        if not pending["pr"]:
            prs = json.loads(command("gh", "pr", "list", "--head", branch, "--state", "all", "--json", "url", cwd=self.workspace))
            if prs:
                pending["pr"] = prs[0]["url"]
            else:
                pending["pr"] = command("gh", "pr", "create", "--base", "main", "--head", branch,
                    "--title", "Intake updated Remarkable thoughts", "--body",
                    "Bounded source-linked notebook changes for the existing planning project. No accepted priorities, tasks, Calendar, memory or sending effects. The trusted project publisher validates the complete candidate before delivery.", cwd=self.workspace)
            _atomic_write(self.publication_path, pending)
        attempts = pending.get("recovery_attempts", 0) + 1
        if attempts > 3:
            raise CaptureError("Publication recovery exhausted; reconcile the existing PR")
        pending["recovery_attempts"] = attempts
        _atomic_write(self.publication_path, pending)
        for _ in range(24):
            revision = self._main()
            try:
                if self._published(staged, revision):
                    result = self.intake.confirm(self.workspace, self.settings["project"], revision)
                    # Keep the last receipt; completion is explicit, not deletion.
                    _atomic_write(self.publication_path, {**pending, "status": "confirmed", "workspace_revision": revision})
                    return {**result, "pr": pending["pr"]}
            except CaptureError:
                pass
            time.sleep(5)
        check = json.loads(command("gh", "pr", "view", pending["pr"], "--json", "state,mergeable,statusCheckRollup", cwd=self.workspace))
        failures = {"FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED"}
        if check["state"] == "CLOSED" or check.get("mergeable") == "CONFLICTING" or any(
            item.get("conclusion") in failures or item.get("state") in {"FAILURE", "ERROR"}
            for item in check.get("statusCheckRollup") or []):
            raise CaptureError("Publication failed validation or conflicts; reconcile the existing PR")
        return {"status": "awaiting_publication", "pr": pending["pr"]}

    def refresh(self, reader=None):
        with self.lock():
            if self.settings["paused"]:
                return {"status": "paused", "effect": "none"}
            started = now()
            try:
                size = sum(p.stat().st_size for p in self.store.directory.rglob("*") if p.is_file())
                if size > self.settings["max_storage_bytes"]:
                    raise CaptureError("Private retention bound reached; review before further capture")
                previous = json.loads(self.publication_path.read_text()) if self.publication_path.exists() else None
                state = self.intake._load()
                owned = [f"projects/{self.settings['project']}/inbox/remarkable-{p['id']}.md" for p in state["pages"].values()]
                clean = self._clean(owned)
                if previous and previous.get("status") != "confirmed":
                    if "staged" not in previous:
                        raise CaptureError("Publication recovery needs its exact staged receipt")
                    publication = self._publish(previous["staged"])
                    if publication["status"] != "confirmed_on_main":
                        receipt = {"schema": SCHEMA, "started_at": started, "finished_at": now(),
                                   "status": "recovering", "publication": publication,
                                   "attention_required": False, "source_access": "unknown", "sync_freshness": "unknown"}
                        _atomic_write(self.receipt_path, receipt)
                        return receipt
                    previous = json.loads(self.publication_path.read_text())
                if clean and (not previous or previous.get("status") == "confirmed"):
                    revision = self._main()
                    command("git", "checkout", "--detach", revision, cwd=self.workspace)
                capture = self.store.capture()
                state = self.intake._load()
                selected = self.settings["baseline_pages"] if not state["pages"] else []
                reading = self.intake.interpret(selected, reader or (lambda *a: broker_reader(self.cfg, *a)))
                staged = self.intake.stage(self.workspace, self.settings["project"])
                command("./scripts/workspace-git.sh", "check", cwd=self.workspace)
                publication = self._publish(staged)
                receipt = {"schema": SCHEMA, "started_at": started, "finished_at": now(),
                    "status": "succeeded" if publication["status"] in ("confirmed_on_main", "no_extracts") else "recovering",
                    "capture": capture, "reading": reading, "publication": publication,
                    "attention_required": False, "source_access": "available", "sync_freshness": "unknown"}
            except Exception as exc:
                receipt = {"schema": SCHEMA, "started_at": started, "finished_at": now(),
                    "status": "blocked", "attention_required": True,
                    "error_type": type(exc).__name__, "error": str(exc) if isinstance(exc, CaptureError) else "Owner cycle failed; inspect private diagnostics",
                    "source_access": "unavailable" if isinstance(exc, FileNotFoundError) else "unknown", "sync_freshness": "unknown"}
            _atomic_write(self.receipt_path, receipt)
            return receipt

    def status(self):
        receipt = json.loads(self.receipt_path.read_text()) if self.receipt_path.exists() else None
        try:
            read_snapshot(LocalTransport(), self.cfg.root, self.settings["notebook_id"])
            available = True
        except (OSError, ValueError):
            available = False
        service = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"], capture_output=True, text=True, timeout=10) if sys.platform == "darwin" else None
        loaded = bool(service and service.returncode == 0)
        attention = bool(receipt and receipt["attention_required"])
        paused = self.settings["paused"]
        return {"schema": SCHEMA, "generated_at": now(), "source_access": "available" if available else "unavailable",
            "sync_freshness": "unknown", "capture": self.store.status(), "units": [{
                "unit_id": "remarkable:thought-intake", "label": LABEL, "title": "Remarkable thoughts",
                "declared_enabled": not paused, "effective_enabled": loaded and not paused,
                "schedule": f"Daily {self.settings['hour']:02}:{self.settings['minute']:02} {self.settings['timezone']}",
                "lifecycle": "paused" if paused else "stuck" if attention or not available else "idle",
                "last_run_status": receipt["status"] if receipt else "not_run",
                "last_run_at": receipt["finished_at"] if receipt else None,
                "available_actions": ["run-now", "pause-schedule", "resume-schedule"],
                "action_requirements": {"run-now": {"requires_confirmation": True, "reason": "May make bounded Broker reads and publish private project extracts."}},
                "last": {"domain": {"attention_required": attention or not available, "status": receipt["status"] if receipt else "not_run",
                                    "reason": receipt.get("error") if receipt else "Selected source absent" if not available else None},
                         "transport": {"owner_alert_sent": False}},
                "details": {"source_access": "available" if available else "unavailable", "sync_freshness": "unknown", "receipt": str(self.receipt_path)},
                "compute": {"llm_usage": "conditional", "brokered": True, "metering": "covered", "cost_class": "medium"}}]}

    def install(self, load=False):
        if sys.platform != "darwin":
            raise CaptureError("This service is owned by macOS launchd")
        # Launchd calendar values use the owner's local timezone.
        local_zone = str(Path("/etc/localtime").resolve()).split("/zoneinfo/")[-1]
        if local_zone != self.settings["timezone"]:
            raise CaptureError("Host timezone does not match the selected planning schedule")
        plist = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
        plist.parent.mkdir(parents=True, exist_ok=True)
        logs = self.path.parent / "logs"; _private_dir(logs)
        definition = {"Label": LABEL, "ProgramArguments": [sys.executable, "-m", "rmk.runtime", "refresh", "--runtime-file", str(self.path)],
            "StartCalendarInterval": {"Hour": self.settings["hour"], "Minute": self.settings["minute"]},
            "RunAtLoad": False, "StandardOutPath": str(logs/"intake.out.log"), "StandardErrorPath": str(logs/"intake.err.log"),
            "EnvironmentVariables": {"PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"}}
        data = plistlib.dumps(definition)
        if plist.exists() and plist.read_bytes() != data:
            raise CaptureError("Existing service definition differs; preserve and reconcile")
        if not plist.exists():
            plist.write_bytes(data); plist.chmod(0o600)
        if load:
            if not self.status()["source_access"] == "available":
                raise CaptureError("Pair/sync selected source before activation")
            command("launchctl", "enable", f"gui/{os.getuid()}/{LABEL}")
            command("launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist))
        return {"status": "loaded" if load else "prepared", "plist": str(plist)}

    def control(self, action):
        if action == "run-now":
            return self.refresh()
        if action not in ("pause-schedule", "resume-schedule"):
            raise CaptureError("Unsupported intake control")
        if action == "resume-schedule" and self.status()["source_access"] != "available":
            raise CaptureError("Pair/sync selected source before activation")
        with self.lock():
            s = dict(self.settings, paused=action == "pause-schedule")
            _atomic_write(self.path, s); self.settings = s
        if sys.platform == "darwin":
            target = f"gui/{os.getuid()}/{LABEL}"
            if s["paused"]:
                command("launchctl", "disable", target)
                subprocess.run(["launchctl", "bootout", target], capture_output=True, timeout=15)
            elif not self.status()["units"][0]["effective_enabled"]:
                self.install(load=True)
        return {"status": "paused" if s["paused"] else "resumed"}


def main():
    import argparse
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=("refresh", "status", "install", "run-now", "pause-schedule", "resume-schedule"))
    p.add_argument("--runtime-file", type=Path, default=runtime_path())
    p.add_argument("--load", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("--confirmed", action="store_true")
    p.add_argument("--action-id")
    args = p.parse_args()
    try:
        runtime = Runtime(args.runtime_file)
        result = runtime.refresh() if args.action == "refresh" else runtime.status() if args.action == "status" else runtime.install(args.load) if args.action == "install" else runtime.control(args.action)
        print(json.dumps(result, indent=2))
        if result.get("status") == "blocked":
            raise SystemExit(1)
    except CaptureError as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
