# rmk — a reMarkable ↔ LLM bridge

Pull a handwritten note off your reMarkable and push it to Claude for a summary
or a flow diagram — all from the command line.

```
rmk ls                          # list your notebooks
rmk summary "Roadmap"           # Claude reads the handwriting and summarises it
rmk diagram "Roadmap" -o r.mmd  # turn the ideas into a Mermaid flow diagram
rmk ask "Roadmap" "what did I decide about pricing?"
rmk pull "Roadmap" -o r.pdf     # just render the note to a local PDF
```

Inspired by [ghostwriter](https://github.com/awwaiid/ghostwriter) and
[Riddle](https://github.com/MaximeRivest/Riddle) — but where those take over the
e-ink screen on-device, `rmk` is a plain scriptable bridge: the note comes to
your laptop and the answer prints to your terminal.

## How it works

```
 reMarkable 2                rmk (this tool)                     Claude
 ┌──────────┐   SSH/SFTP   ┌───────────────────────────┐  vision  ┌────────┐
 │ xochitl  │ ───────────▶ │ transport → library →     │ ───────▶ │ Opus   │
 │  .rm/.md │  (USB now)   │ render (.rm→PDF→PNG) → llm │ ◀─────── │        │
 └──────────┘              └───────────────────────────┘  text    └────────┘
```

1. **transport** — SSH/SFTP into the tablet and read its document store. Today
   this is USB (`10.11.99.1`); the `Transport` interface is swappable so the
   reMarkable Cloud API can slot in later.
2. **library** — turn the flat UUID store into named notebooks and resolve
   `"Roadmap"` (or a path, or a UUID) to the right one.
3. **render** — `rmc`/`rmscene` convert the `.rm` v6 strokes to a PDF; `pypdfium2`
   rasterises pages to PNG.
4. **llm** — the page images go to Claude's vision model, which reads your
   handwriting and returns a summary / Mermaid diagram / freeform answer.

## Setup

You need a reMarkable 2 in **developer mode** with SSH enabled (Settings → General
→ Software / Help). Then:

```bash
uv sync                       # install rmk + dependencies
export ANTHROPIC_API_KEY=...  # your Claude API key
uv run rmk init               # writes ~/.config/rmk/config.toml
```

Edit the config and set `[ssh].password` — it's shown **on the tablet** at
Settings → Help → Copyrights and licenses. (Or set up SSH key auth and use
`key_path` instead.) Then plug in the tablet over USB and verify:

```bash
uv run rmk doctor
```

`doctor` checks the config, SSH connection, the renderer, and the API key, and
tells you exactly what's missing.

## Commands

| Command | What it does |
| --- | --- |
| `rmk init` | Write a starter config file. |
| `rmk doctor` | Check config / SSH / renderer / API key. |
| `rmk ls [--tree] [--all]` | List notebooks (optionally as a folder tree). |
| `rmk pull NAME -o out.pdf` | Render a notebook to a local PDF (no LLM). |
| `rmk summary NAME` | Summarise a notebook with Claude. |
| `rmk diagram NAME [-o f.mmd]` | Turn a notebook into a Mermaid flow diagram. |
| `rmk ask NAME "question"` | Ask Claude anything about a notebook. |

`NAME` can be a notebook name, a full path (`Ideas/Roadmap`), or a UUID.

## Status & limits (v1)

- **Read-only** on the tablet — `rmk` never writes to your device.
- **Handwritten notebooks** are the target. Notes annotated *on top of a PDF/EPUB*
  render only the ink layer today (the background PDF is not merged in).
- Very long notebooks are capped at the first 20 pages per LLM call (with a
  warning) to keep payloads reasonable.
- Transport is USB-SSH; the cloud path is stubbed for later.

## Development

```bash
uv run pytest        # unit tests for the metadata/tree/render-order logic
```

The tree-parsing and name-resolution logic in `library.py` is pure and tested
without a tablet. The SSH, render, and LLM paths need a real device / API key to
exercise end-to-end (`rmk doctor` is the quickest smoke test).
