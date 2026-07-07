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

The "brain" is [agent-broker](https://github.com/skishore1676/agent-broker) —
rmk never calls a model provider directly. It hands the note to the broker,
which owns provider selection / failover / receipts and runs the logged-in
`claude` CLI (so there's **no API key to manage** — just Claude Code auth).

## How it works

```
 reMarkable 2            rmk (this tool)              agent-broker      claude CLI
 ┌──────────┐  SSH/SFTP ┌────────────────────────┐  ┌───────────┐  vision ┌──────┐
 │ xochitl  │ ────────▶ │ transport → library →  │─▶│ provider  │────────▶│ reads│
 │  .rm     │  (USB)    │ render(.rm→PDF→PNG)→llm │  │ select +  │◀────────│ imgs │
 └──────────┘           └────────────────────────┘  │ receipts  │  text   └──────┘
                                                     └───────────┘
```

1. **transport** — read the tablet's document store. Two implementations today:
   `local` (the **reMarkable desktop app**'s synced folder on this Mac — no
   tablet needed) and `ssh` (USB/Wi-Fi to a physical tablet at `10.11.99.1`).
   The interface is swappable, so the reMarkable Cloud API can slot in later.
2. **library** — turn the flat UUID store into named notebooks and resolve
   `"Roadmap"` (or a path, or a UUID) to the right one.
3. **render** — `rmc`/`rmscene` convert the `.rm` v6 strokes to a PDF; `pypdfium2`
   rasterises pages to PNG files.
4. **llm** — the page images are handed to the **agent broker** as an
   `AgentTask` (raw-prompt passthrough). The broker's `claude` provider views
   the images with its Read tool and returns a summary / Mermaid diagram / answer.

## Setup

You need the [agent-broker](https://github.com/skishore1676/agent-broker) repo
checked out **next to this one** (`../agent-broker`) and the `claude` CLI logged
in:

```bash
uv sync              # installs rmk + agent-broker (editable, from ../agent-broker)
claude auth status   # should show loggedIn: true — that's the LLM auth
uv run rmk init      # writes ~/.config/rmk/config.toml
```

Then pick a source in the config's `[remarkable]` section:

**A) The desktop app (no tablet — recommended, and the tested path).** If you run
the reMarkable desktop app, it keeps a synced copy of your notebooks locally:

```toml
[remarkable]
transport = "local"
root = "/Users/<you>/Library/Containers/com.remarkable.desktop/Data/Library/Application Support/remarkable/desktop"
```

**B) A physical tablet over SSH.** Needs developer mode / SSH enabled (Settings →
General → Software). Set `transport = "ssh"` and `[ssh].password` — shown on the
tablet at Settings → Help → Copyrights and licenses (or use `key_path`).

Then verify:

```bash
uv run rmk doctor
```

`doctor` checks the config, SSH connection, the renderer, and the broker/`claude`
CLI, and tells you exactly what's missing.

### Using your own broker policy

By default rmk runs a single `claude` binding through the broker. To use your
configured provider chain (failover + receipts) instead, point the config at a
policy file:

```toml
[broker]
policy_path = "/Users/suman/code/agent-broker/policies/your_policy.yaml"
actor = "reader"
role  = "note_reader"
```

## Commands

| Command | What it does |
| --- | --- |
| `rmk init` | Write a starter config file. |
| `rmk doctor` | Check config / SSH / renderer / broker. |
| `rmk ls [--tree] [--all]` | List notebooks (optionally as a folder tree). |
| `rmk pull NAME -o out.pdf` | Render a notebook to a local PDF (no LLM). |
| `rmk summary NAME` | Summarise a notebook via the broker. |
| `rmk diagram NAME [-o f.mmd]` | Turn a notebook into a Mermaid flow diagram. |
| `rmk ask NAME "question"` | Ask the broker anything about a notebook. |

`NAME` can be a notebook name, a full path (`Ideas/Roadmap`), or a UUID.

## Status & limits (v1)

- **Read-only** on the tablet — `rmk` never writes to your device.
- **Handwritten notebooks** are the target. Notes annotated *on top of a PDF/EPUB*
  render only the ink layer today (the background PDF is not merged in).
- Very long notebooks are capped at the first 20 pages per broker call (with a
  warning) to keep payloads reasonable.
- Transport is `local` (desktop-app store) or `ssh` (tablet); the cloud path is
  stubbed for later.
- Rendering is pure-Python (`.rm`→SVG via `rmc`, →PDF via `svglib`/`reportlab`,
  →PNG via `pypdfium2`) — no Inkscape or Cairo system deps.
- Depends on a sibling `../agent-broker` checkout (path dependency), so the
  public repo isn't `pip install`-able standalone — it's wired to Suman's brain.

## Development

```bash
uv run pytest        # unit tests for the metadata/tree/render-order logic
```

The tree-parsing and name-resolution logic in `library.py` is pure and tested
without a tablet. The `.rm`→PNG render and the broker vision path are verified
end-to-end (the broker's `claude` provider reads a page image and answers). The
SSH pull needs a real device; `rmk doctor` is the quickest smoke test.
