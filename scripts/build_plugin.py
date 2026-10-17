#!/usr/bin/env python3
"""Build the private reMarkable plugin with Python 3.11+ and the standard library.

Only selected plugin files and Python package source enter the archive. Runtime
configuration, credentials, captures, dependencies and tests are never copied.
The ZIP is reproducible for identical inputs on the same Python/zlib version.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tempfile
import tomllib
import zipfile


REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_NAME = "remarkable-companion"
PLUGIN_ROOT = "$" + "{PLUGIN_ROOT}"
PLUGIN_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
MCP_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
EXPECTED_ARGS = [
    "run", "--directory", PLUGIN_ROOT + "/server", "--",
    "python", "-m", "rmk.mcp_server",
]
ZIP_TIME = (1980, 1, 1, 0, 0, 0)
EXCLUDED_DIRS = {
    "__pycache__", "node_modules", "tests", "fixtures", "venv", "env",
    "site-packages", "dist", "build",
}


class BuildError(Exception):
    pass


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise BuildError(message)


def _nonblank(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _source_bytes(path: Path, root: Path) -> bytes:
    """Read a regular source file without accepting a symlink in its path."""
    relative = path.relative_to(root)
    current = root
    _require(not current.is_symlink(), f"Source directory is a symlink: {root}")
    for part in relative.parts:
        current = current / part
        _require(not current.is_symlink(), f"Source path is a symlink: {current}")
    _require(path.is_file(), f"Missing source file: {path}")
    _require(stat.S_ISREG(path.stat().st_mode), f"Not a regular source file: {path}")
    return path.read_bytes()


def _validate_manifests(files: dict[str, bytes]) -> dict:
    plugin = json.loads(files["plugin.json"])
    mcp = json.loads(files["mcp.json"])
    _require(isinstance(plugin, dict) and plugin.get("$schema") == PLUGIN_SCHEMA,
             "plugin.json must use the portable Agent Plugins 1.0 schema.")
    _require(plugin.get("name") == PLUGIN_NAME, "Unexpected plugin name.")
    version = plugin.get("version", "")
    _require(isinstance(version, str) and re.fullmatch(
        r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
        r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
        r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?", version) is not None,
        "Plugin version must be semantic versioning.")
    _require(not {"skills", "mcpServers", "apps", "interface"} & plugin.keys(),
             "Portable plugin settings must not use legacy top-level fields.")
    _require(_nonblank(plugin.get("description")), "Plugin description is required.")
    author = plugin.get("author")
    _require(isinstance(author, dict) and _nonblank(author.get("name")),
             "Plugin author must be a named object.")
    interface = plugin.get("extensions", {}).get("com.openai", {}).get("interface")
    _require(isinstance(interface, dict), "OpenAI interface metadata is required.")
    for key in ("displayName", "shortDescription", "longDescription", "developerName", "category"):
        _require(_nonblank(interface.get(key)), f"Missing interface {key}.")
    _require(len(interface["shortDescription"]) <= 30, "Plugin subtitle exceeds 30 characters.")
    capabilities = interface.get("capabilities")
    _require(isinstance(capabilities, list) and all(_nonblank(v) for v in capabilities),
             "Interface capabilities must be an array of strings.")
    prompts = interface.get("defaultPrompt")
    _require(_nonblank(prompts) or (
        isinstance(prompts, list) and 1 <= len(prompts) <= 3
        and all(_nonblank(p) for p in prompts)),
        "Default prompts must be a string or up to three strings.")
    _require(isinstance(mcp, dict) and mcp.get("$schema") == MCP_SCHEMA,
             "mcp.json must use the portable MCP schema.")
    servers = mcp.get("mcpServers")
    _require(isinstance(servers, dict) and set(servers) == {PLUGIN_NAME},
             "Package must contain the one intended local MCP server.")
    expected = {
        "type": "stdio", "command": "uv", "args": EXPECTED_ARGS,
        "cwd": PLUGIN_ROOT + "/server",
    }
    _require(servers[PLUGIN_NAME] == expected,
             "Local MCP command differs from the packaged runtime entrypoint.")
    return plugin


def _yaml_scalar(raw: str) -> str:
    """Parse the plain/JSON-quoted string subset emitted for this package."""
    if raw.startswith('"'):
        value = json.loads(raw)
    else:
        _require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", raw) is not None,
                 "Quote YAML values outside the generated plain-string subset.")
        _require(raw.lower() not in {"true", "false", "null", "yes", "no"},
                 "YAML metadata must contain strings.")
        value = raw
    _require(_nonblank(value), "YAML metadata contains an empty or non-string value.")
    return value


def _validate_skill(files: dict[str, bytes]) -> None:
    # These authored files intentionally use this small YAML subset, keeping
    # the build independent of PyYAML and the optional Broker dependency.
    prefix = f"skills/{PLUGIN_NAME}/"
    skill = files[prefix + "SKILL.md"].decode("utf-8")
    match = re.match(r"\A---\n(.*?)\n---\n(.+)\Z", skill, re.DOTALL)
    _require(match is not None, "Skill needs YAML frontmatter and instructions.")
    frontmatter: dict[str, str] = {}
    for line in match[1].splitlines():
        key, separator, value = line.partition(": ")
        _require(bool(separator) and key not in frontmatter,
                 "Malformed or duplicate skill frontmatter field.")
        frontmatter[key] = _yaml_scalar(value)
    _require(set(frontmatter) == {"name", "description"}, "Unexpected skill metadata.")
    _require(frontmatter["name"] == PLUGIN_NAME, "Skill name and folder must match.")
    _require("[TODO" not in skill and len(files[prefix + "SKILL.md"]) <= 1024 * 1024,
             "Skill is unfinished or exceeds the size limit.")
    lines = files[prefix + "agents/openai.yaml"].decode("utf-8").splitlines()
    _require(bool(lines) and lines[0] == "interface:", "Unexpected agent YAML shape.")
    interface: dict[str, str] = {}
    for line in lines[1:]:
        if not line.strip():
            continue
        match = re.fullmatch(r"  ([a-z_]+): (.+)", line)
        _require(match is not None and match[1] not in interface,
                 "Malformed or duplicate agent interface field.")
        interface[match[1]] = _yaml_scalar(match[2])
    _require(set(interface) == {"display_name", "short_description", "default_prompt"},
             "Unexpected generated agent interface fields.")
    _require(25 <= len(interface["short_description"]) <= 64,
             "Skill short description must contain 25–64 characters.")
    _require("$" + PLUGIN_NAME in interface["default_prompt"],
             "Skill default prompt must name the skill.")


def _runtime_project(source: bytes, plugin: dict) -> bytes:
    project = tomllib.loads(source.decode("utf-8"))["project"]
    dependencies = project.get("dependencies")
    _require(isinstance(dependencies, list), "Missing runtime dependencies.")
    public_dependencies: list[str] = []
    for requirement in dependencies:
        _require(isinstance(requirement, str), "Dependency must be a string.")
        name = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", requirement)
        _require(name is not None, "Malformed dependency.")
        if re.sub(r"[-_.]+", "-", name[0]).lower() == "agent-broker":
            continue
        _require("@" not in requirement and "://" not in requirement
                 and "\n" not in requirement and "\r" not in requirement,
                 "Review direct-source dependencies before packaging.")
        public_dependencies.append(requirement)
    _require(public_dependencies, "Standalone server has no runtime dependencies.")
    quote = lambda value: json.dumps(value, ensure_ascii=False)
    dependency_lines = "\n".join("    " + quote(v) + "," for v in public_dependencies)
    text = (
        "[project]\n"
        'name = "remarkable-companion-server"\n'
        f"version = {quote(plugin['version'])}\n"
        'description = "Interactive reMarkable notebook and document MCP tools."\n'
        f"requires-python = {quote(project['requires-python'])}\n"
        f"authors = [{{ name = {quote(plugin['author']['name'])} }}]\n"
        "dependencies = [\n" + dependency_lines + "\n]\n\n"
        "[project.scripts]\n"
        'rmk-mcp = "rmk.mcp_server:main"\n\n'
        "[build-system]\n"
        'requires = ["hatchling"]\n'
        'build-backend = "hatchling.build"\n\n'
        "[tool.hatch.build.targets.wheel]\n"
        'packages = ["src/rmk"]\n'
    )
    parsed = tomllib.loads(text)
    _require("tool" not in parsed or "uv" not in parsed["tool"],
             "A local dependency override entered the standalone project.")
    return text.encode("utf-8")


def _collect_files(repo: Path) -> dict[str, bytes]:
    plugin_source = repo / "plugins" / PLUGIN_NAME
    fixed = (
        "plugin.json", "mcp.json", f"skills/{PLUGIN_NAME}/SKILL.md",
        f"skills/{PLUGIN_NAME}/agents/openai.yaml",
    )
    files = {name: _source_bytes(plugin_source / name, repo) for name in fixed}
    plugin = _validate_manifests(files)
    _validate_skill(files)
    files["README.md"] = _source_bytes(repo / "docs/companion-plugin.md", repo)
    files["server/pyproject.toml"] = _runtime_project(
        _source_bytes(repo / "pyproject.toml", repo), plugin)
    package_source = repo / "src/rmk"
    _require(package_source.is_dir() and not package_source.is_symlink(),
             "Missing regular rmk source directory.")
    for directory, children, filenames in os.walk(package_source, followlinks=False):
        here = Path(directory)
        for child in children:
            _require(not (here / child).is_symlink(), f"Source directory is a symlink: {here / child}")
        children[:] = sorted(
            child for child in children
            if not child.startswith(".") and child not in EXCLUDED_DIRS
        )
        for filename in sorted(filenames):
            if not filename.endswith(".py") or filename.startswith((".", "test_")):
                continue
            path = here / filename
            data = _source_bytes(path, repo)
            name = "server/src/rmk/" + path.relative_to(package_source).as_posix()
            compile(data, name, "exec")
            files[name] = data
    _require("server/src/rmk/mcp_server.py" in files
             and "server/src/rmk/__init__.py" in files,
             "Standalone runtime is missing its module entrypoint.")
    return files


def _check_import(staging: Path, files: dict[str, bytes]) -> None:
    plugin = json.loads(files["plugin.json"])
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]); "
        "import rmk.mcp_server as server; "
        "assert callable(server.main); "
        "assert server.SERVER_INFO == {'name': sys.argv[2], 'version': sys.argv[3]}"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", code, str(staging / "server/src"),
         PLUGIN_NAME, plugin["version"]],
        cwd=staging, capture_output=True, text=True, timeout=20,
    )
    _require(result.returncode == 0,
             "Packaged MCP transport failed its isolated import check.")


def _write_archive(path: Path, files: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, date_time=ZIP_TIME)
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, files[name], compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    with zipfile.ZipFile(path) as archive:
        _require(archive.namelist() == sorted(files), "Unexpected or duplicate archive entries.")
        _require(archive.testzip() is None, "Archive CRC validation failed.")
        for entry in archive.infolist():
            name = PurePosixPath(entry.filename)
            _require(not name.is_absolute() and ".." not in name.parts
                     and "\\" not in entry.filename
                     and stat.S_ISREG(entry.external_attr >> 16),
                     "Archive contains an unsafe path or nonregular file.")
            _require(entry.date_time == ZIP_TIME and archive.read(entry) == files[entry.filename],
                     "Archive changed file content or deterministic metadata.")


def build_plugin(output: Path, repo: Path = REPO_ROOT) -> dict:
    _require(output.is_absolute(), "--output must be an absolute path.")
    _require(output.suffix.lower() == ".zip", "--output must end with .zip.")
    _require(not output.is_symlink(), "Refusing to replace a symlink.")
    output = output.resolve()
    for source in (repo / "plugins", repo / "src", repo / "scripts", repo / "docs"):
        _require(not output.is_relative_to(source.resolve()),
                 "Write the archive outside the source directories.")
    files = _collect_files(repo)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="rmk-plugin-build-") as temporary:
        staging = Path(temporary) / PLUGIN_NAME
        staging.mkdir()
        _require(not output.is_relative_to(staging), "Archive must be outside the staging directory.")
        for name, data in sorted(files.items()):
            path = staging / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            path.chmod(0o644)
        _check_import(staging, files)
        descriptor, archive_name = tempfile.mkstemp(
            prefix=".remarkable-companion-", suffix=".zip", dir=output.parent)
        os.close(descriptor)
        pending = Path(archive_name)
        try:
            _write_archive(pending, files)
            os.replace(pending, output)
            output.chmod(0o644)
        finally:
            pending.unlink(missing_ok=True)
    return {
        "archive": str(output),
        "files": len(files),
        "bytes": output.stat().st_size,
        "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path,
                        default=REPO_ROOT / "dist" / f"{PLUGIN_NAME}.zip",
                        help="Absolute ZIP destination (default: repository dist/remarkable-companion.zip).")
    args = parser.parse_args(argv)
    try:
        result = build_plugin(args.output.expanduser())
    except (BuildError, OSError, ValueError, KeyError, SyntaxError, subprocess.TimeoutExpired) as error:
        parser.exit(1, f"Plugin build failed: {error}\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
