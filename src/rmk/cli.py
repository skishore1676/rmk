"""rmk command-line interface.

    rmk init                 write a starter config file
    rmk doctor               check config, SSH, renderer, and API key
    rmk ls [--tree]          list notebooks on the tablet
    rmk pull NAME -o out.pdf render a notebook to a local PDF
    rmk ask  NAME "..."      ask Claude anything about a notebook
    rmk summary NAME         summarise a notebook
    rmk diagram NAME         turn a notebook into a Mermaid flow diagram
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.tree import Tree

from . import llm, render
from .config import CONFIG_TEMPLATE, Config, config_path
from .library import Library, full_path
from .transport import SSHTransport

app = typer.Typer(
    add_completion=False,
    help="Bridge your reMarkable tablet to an LLM: pull a note, push it to Claude.",
    no_args_is_help=True,
)
console = Console()
err = Console(stderr=True)


# -- helpers -----------------------------------------------------------------
def _transport(cfg: Config) -> SSHTransport:
    return SSHTransport(
        host=cfg.ssh.host,
        user=cfg.ssh.user,
        password=cfg.ssh.password,
        key_path=cfg.ssh.key_path,
        port=cfg.ssh.port,
    )


def _library(cfg: Config, transport: SSHTransport) -> Library:
    return Library(transport, cfg.root)


def _fatal(msg: str) -> None:
    err.print(f"[bold red]error:[/] {msg}")
    raise typer.Exit(1)


def _render_pngs(cfg: Config, name: str) -> tuple[str, list[bytes]]:
    """Resolve a notebook, pull its pages, and rasterise to PNG. Returns
    (path, pngs)."""
    with _transport(cfg) as t:
        lib = _library(cfg, t)
        try:
            doc = lib.resolve(name)
        except LookupError as e:
            _fatal(str(e))
        path = lib.path_of(doc.uuid)
        console.print(f"Pulling [bold]{path}[/] …")
        pages = lib.read_pages(doc.uuid)
    if not pages:
        _fatal(f"{path!r} has no handwritten pages to render.")
    if len(pages) > llm.MAX_PAGES:
        err.print(
            f"[yellow]note:[/] {len(pages)} pages; sending the first "
            f"{llm.MAX_PAGES} to the model."
        )
        pages = pages[: llm.MAX_PAGES]
    console.print(f"Rendering {len(pages)} page(s) …")
    return path, render.pages_to_pngs(pages)


# -- commands ----------------------------------------------------------------
@app.command()
def init(force: bool = typer.Option(False, help="Overwrite an existing config.")) -> None:
    """Write a starter config file."""
    path = config_path()
    if path.exists() and not force:
        _fatal(f"Config already exists at {path}. Use --force to overwrite.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(CONFIG_TEMPLATE)
    console.print(f"Wrote config to [bold]{path}[/].")
    console.print(
        "Now set the SSH password (Settings -> Help -> Copyrights and licenses "
        "on the tablet) and export ANTHROPIC_API_KEY, then run [bold]rmk doctor[/]."
    )


@app.command()
def doctor() -> None:
    """Check config, SSH connectivity, the renderer, and the API key."""
    cfg = Config.load()
    ok = True

    path = config_path()
    console.print(
        f"config file: {'[green]found[/]' if path.exists() else '[yellow]missing[/] (using defaults; run `rmk init`)'} "
        f"({path})"
    )

    # Renderer availability.
    try:
        render._rmc_bin()
        console.print("renderer (rmc): [green]ok[/]")
    except render.RenderError as e:
        ok = False
        console.print(f"renderer (rmc): [red]missing[/] — {e}")

    # API key.
    if cfg.llm.api_key:
        console.print("ANTHROPIC_API_KEY: [green]set[/]")
    else:
        console.print(
            "ANTHROPIC_API_KEY: [yellow]not set[/] "
            "(needed for ask/summary/diagram, not for ls/pull)"
        )

    # SSH + a real read.
    try:
        with _transport(cfg) as t:
            lib = _library(cfg, t)
            docs = lib.documents()
        console.print(
            f"tablet SSH ({cfg.ssh.user}@{cfg.ssh.host}): [green]ok[/] "
            f"— {len(docs)} notebook(s) found"
        )
    except Exception as e:  # noqa: BLE001
        ok = False
        console.print(f"tablet SSH ({cfg.ssh.user}@{cfg.ssh.host}): [red]failed[/] — {e}")

    raise typer.Exit(0 if ok else 1)


@app.command("ls")
def list_notebooks(
    tree: bool = typer.Option(False, "--tree", help="Show the folder tree."),
    all_: bool = typer.Option(False, "--all", help="Include deleted notebooks."),
) -> None:
    """List notebooks on the tablet."""
    cfg = Config.load()
    try:
        with _transport(cfg) as t:
            lib = _library(cfg, t)
            docs_index = lib.load()
            docs = lib.documents(include_deleted=all_)
    except Exception as e:  # noqa: BLE001
        _fatal(str(e))

    if not docs:
        console.print("No notebooks found.")
        return

    if tree:
        root = Tree(f"[bold]{cfg.ssh.host}[/]")
        # Build folder nodes lazily by full path.
        nodes: dict[str, Tree] = {"": root}

        def node_for(uuid: str) -> Tree:
            d = docs_index.get(uuid)
            if d is None or uuid in ("", "trash"):
                return root
            if uuid in nodes:
                return nodes[uuid]
            parent = node_for(d.parent)
            n = parent.add(f"[blue]{d.name}/[/]")
            nodes[uuid] = n
            return n

        for d in docs:
            node_for(d.parent).add(d.name)
        console.print(root)
    else:
        for d in docs:
            console.print(full_path(docs_index, d.uuid))


@app.command()
def pull(
    name: str = typer.Argument(..., help="Notebook name, path, or UUID."),
    out: Path = typer.Option(None, "-o", "--out", help="Output PDF path."),
) -> None:
    """Render a notebook to a local PDF."""
    cfg = Config.load()
    try:
        with _transport(cfg) as t:
            lib = _library(cfg, t)
            doc = lib.resolve(name)
            path = lib.path_of(doc.uuid)
            console.print(f"Pulling [bold]{path}[/] …")
            pages = lib.read_pages(doc.uuid)
    except LookupError as e:
        _fatal(str(e))
    except Exception as e:  # noqa: BLE001
        _fatal(str(e))

    if not pages:
        _fatal(f"{path!r} has no handwritten pages to render.")

    out_path = out or Path(f"{doc.name}.pdf")
    console.print(f"Rendering {len(pages)} page(s) → [bold]{out_path}[/] …")
    try:
        render.pages_to_pdf(pages, str(out_path))
    except render.RenderError as e:
        _fatal(str(e))
    console.print(f"[green]Wrote[/] {out_path}")


@app.command()
def ask(
    name: str = typer.Argument(..., help="Notebook name, path, or UUID."),
    prompt: str = typer.Argument(..., help="What to ask Claude about the note."),
) -> None:
    """Ask Claude anything about a notebook."""
    cfg = Config.load()
    _, pngs = _render_pngs(cfg, name)
    console.print("Asking Claude …\n")
    try:
        answer = llm.ask(
            pngs, prompt, model=cfg.llm.model, api_key=cfg.llm.api_key,
            max_tokens=cfg.llm.max_tokens,
        )
    except llm.LLMError as e:
        _fatal(str(e))
    console.print(answer)


@app.command()
def summary(
    name: str = typer.Argument(..., help="Notebook name, path, or UUID."),
) -> None:
    """Summarise a notebook."""
    cfg = Config.load()
    _, pngs = _render_pngs(cfg, name)
    console.print("Summarising with Claude …\n")
    try:
        answer = llm.ask(
            pngs, llm.SUMMARY_PROMPT, model=cfg.llm.model, api_key=cfg.llm.api_key,
            max_tokens=cfg.llm.max_tokens,
        )
    except llm.LLMError as e:
        _fatal(str(e))
    console.print(answer)


@app.command()
def diagram(
    name: str = typer.Argument(..., help="Notebook name, path, or UUID."),
    out: Path = typer.Option(None, "-o", "--out", help="Write the Mermaid to a file."),
) -> None:
    """Turn a notebook into a Mermaid flow diagram."""
    cfg = Config.load()
    _, pngs = _render_pngs(cfg, name)
    console.print("Drawing a flow diagram with Claude …\n")
    try:
        answer = llm.ask(
            pngs, llm.DIAGRAM_PROMPT, model=cfg.llm.model, api_key=cfg.llm.api_key,
            max_tokens=cfg.llm.max_tokens,
        )
    except llm.LLMError as e:
        _fatal(str(e))
    console.print(answer)
    if out:
        out.write_text(answer + "\n")
        console.print(f"\n[green]Wrote[/] {out}")
    else:
        console.print(
            "\n[dim]Paste this into an Obsidian ```mermaid block, or "
            "https://mermaid.live to render it.[/]"
        )


if __name__ == "__main__":
    app()
