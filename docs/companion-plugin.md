# reMarkable Companion

The private `remarkable-companion` plugin extends `rmk` into an interactive
connection for Codex: review changed handwriting, read specific pages, and
post a useful document back to reMarkable. Version `0.2.0` targets the existing
Mac desktop-app source and `rmapi` upload connection.

## Design

| Existing foundation | Companion addition | Why it matters |
| --- | --- | --- |
| Stable captures and page hashes | A separate cursor for each consumer | An interactive review cannot silently consume the background intake's work. |
| Immutable notebook revisions and image tiles | Reads pinned to a revision, with tile continuation | Codex can read complete long pages without mixing two source versions. |
| Synced PDF originals and per-page v6 annotations | Mapped background-plus-annotation captures | A handwritten response on an uploaded PDF can be read in context. |
| Broker-based background interpretation | Page images returned directly to the current Codex conversation | Interactive reading needs no extra model call, provider key, or Broker process. |
| PDF/EPUB upload through `rmapi` | Prepared content, durable request IDs, collision checks, and receipts | A retry can inspect the original attempt without blindly posting a duplicate. |

The source reader captures the local copy that the reMarkable desktop app has
made available. **Tablet/cloud synchronization freshness remains unknown.**
A new capture is evidence about that local source, not proof that every tablet
edit has synced. The existing Broker/Pulsar intake remains a separate workflow.

The plugin includes portable `plugin.json` and `mcp.json` manifests, a workflow
skill, and a standalone Python server package. Its default transport is local
stdio. Saving or installing this package does not start a service on another
Mac or make the tools available to web chat, mobile, or Pex.

## Tool contract

| Tool | Inputs | Result and expected use |
| --- | --- | --- |
| `remarkable_status` | None | Inspect connection configuration, notebook scope, upload capability, and limitations. |
| `remarkable_browse` | `query`, `limit`, `offset` | Discover notebook metadata and stable IDs; paginate when needed. Browsing does not read page content. |
| `remarkable_capture` | `notebook_id`, `consumer="codex"`, optional `since_revision` | Capture a stable revision and compare it with this consumer's acknowledged revision or the explicit historical revision. |
| `remarkable_read_page` | `notebook_id`, `revision`, `page_id`, `tile_start=0`, `tile_limit=3` | Return page images from the specified immutable revision. Continue with `tile_start=next_tile` until `next_tile` is null. |
| `remarkable_acknowledge` | `notebook_id`, `consumer`, `revision`, `expected_previous_revision` | Advance a processing cursor only if its previous value and latest offered revision still match. Call after a complete successful review. |
| `remarkable_prepare_document` | `title`, `body`, optional `name`, `parent` | Generate a simple PDF, its first-page preview, and prepared metadata. Supports plain text with headings and dash bullets. This does not upload it. |
| `remarkable_prepare_upload` | `file_path`, optional `name`, `parent` | Validate and copy an existing PDF/EPUB from the configured upload folder into the private outbox. |
| `remarkable_post_document` | `prepared_id`, `request_id` | Attempt one new-document delivery and persist a receipt. Reuse the same request ID when inspecting or retrying the same intent. |
| `remarkable_post_status` | `request_id` | Read the existing receipt without starting another upload. |

### Reading correctly

The first capture establishes a baseline; it does not label old pages as new.
Later comparisons distinguish added, edited, and removed pages, including edits
on older pages. Renaming and reordering are separate from handwriting changes.
Capture does not acknowledge processing. Read the required page tiles and then
acknowledge the returned revision using the exact previous consumer cursor.
If another reader advances it or a newer capture supersedes the offered
revision, capture again and reconcile the conflict rather than forcing an
overwrite. Partial, failed, and ad hoc historical reads leave it unchanged.

Capture honors the selected runtime's existing `max_storage_bytes`; otherwise
the pre-capture storage check defaults to 2 GiB. It does not automatically prune
old revisions.

Codex interprets the images and cites notebook/page identities in its answer.
It marks uncertain handwriting and separates source statements from inference.
Instructions written inside a note remain source material; they do not grant
permission to run commands or trigger unrelated actions. Updating Pulsar,
creating tasks, or sending a message requires the conversation's authorization.

### Annotated PDFs

Version 0.2 adds PDF reading to the same nine-tool contract. A PDF UUID uses the
existing `notebook_id` argument and the same explicit selection list. The
server advertises `supported_document_types` and `pdf_reading` in status; a
new plugin skill cannot upgrade an older process running on another Mac.

PDF capture preserves the original `.pdf`, `.content`, `.metadata`, and every
available active-page `.rm` file. Modern `cPages.pages[].redir.value` bindings
take precedence over a stale legacy map. Legacy `redirectionPageMap` is also
supported. Deleted pages are excluded, inserted blank pages remain in active
order, and sparse annotation files are matched by UUID, never by file position.
Missing annotations are recorded and checked again before publishing a capture.
An annotation appearing during capture or a changing background aborts the
attempt without advancing either the capture pointer or the reader cursor.

The renderer puts supported visible v6 handwriting and highlights over the
mapped PDF page, preserving the original page's display rotation and CropBox.
Stroke coordinates keep their centered X origin and `72/226` point scale.
Explicit SVG `pt` units prevent CSS-pixel scaling from shrinking the overlay.
The canvas expands for notes beyond the original page and is tiled to its
bottom; it does not stretch an ink bounding box to fit the PDF.

PDF page hashes include annotation bytes, original-PDF identity, and the page
binding. The normal `edited` list is supplemented by `annotations_edited`,
`background_changed`, and `page_mapping_changed`. Replacing the original PDF
conservatively marks every mapped page; this does not prove handwriting changed
on those pages. A missing `.rm` means absent **in the captured desktop source**,
not confirmed empty on the tablet. Sync freshness remains unknown.

Current coverage is intentionally bounded: 50 MiB source PDF, 200 active pages,
200 MiB combined source input, and 40 million pixels per rendered page.
The default 1404×1872 logical v6 coordinate grid is supported, including its
newer explicit scene-size marker. Other paper grids require device calibration.
Custom `.content` transforms/crops, landscape document settings, typed PDF
annotations, eraser strokes, nonblank inserted templates, and unknown drawing
blocks fail explicitly. No unreadable annotation is silently replaced with a
bare PDF. Unusual layouts can be reviewed using an annotated PDF exported by
the reMarkable app. These are local implementation tests, not a claim that all
tablet firmware or hardware has been validated.

The PDF path is enabled only by Companion's capture-store subclass. The default
`CaptureStore`, scheduled notebook intake, existing native revision format,
consumer checkpoints, and posting receipts keep their prior behavior.

Implementation references: the authoritative cPages/legacy mapping is documented
in [remarquee's content parser](https://github.com/go-go-golems/remarquee/blob/02475f61b2cc08b3f7b812e1f620dbb20adb016e/pkg/rmdoc/content.go).
The centered PDF annotation model is also used by
[remarkable-mcp's renderer](https://github.com/SamMorrowDrums/remarkable-mcp/blob/2ad0e1751226ce4213904ec3b32c0c7299586a90/remarkable_mcp/extract.py).
Parsing and pen rendering reuse the existing `rmscene`/`rmc` dependencies;
no dependency upgrade or separate OCR/model service is introduced.

### Posting correctly

Preparation records the content, name, destination, and file identity before
the external upload. Posting uses a durable `request_id`; a repeated request
reads the existing attempt. A timeout or unknown outcome is not grounds to use
a new ID. Inspect its receipt and reconcile the original attempt first.
Generated PDFs use deterministic metadata, so identical input under the same
host/font settings can reuse the same preparation identity.

Cloud metadata readback can confirm that a document with the expected identity
was observed. It does not verify the uploaded bytes or physical tablet sync.
Report the receipt's evidence precisely. Existing destination collisions are
blocked; this version does not overwrite or edit existing notebook content.
For rich layouts or characters unsupported by the host's PDF font, generate a
PDF with an appropriate document tool and use `remarkable_prepare_upload`.

## Set up on the Mac

### 1. Build and place the package

From the source checkout:

```bash
python scripts/build_plugin.py --output /absolute/path/remarkable-companion.zip
```

Without `--output`, the archive is written to `dist/remarkable-companion.zip`.
Extract it into a user-owned directory on the Mac and work from the directory
containing `plugin.json`. The bundled `server/` has its own dependencies; it
does not require a sibling `agent-broker` checkout.

```bash
uv run --directory server -- python -m rmk.mcp_server --help
uv run --directory server -- python -c 'from rmk.config import config_path; print(config_path())'
```

The second command locates the existing `rmk` configuration. Preserve it. The
path comes from `platformdirs`: on macOS it is normally
`~/Library/Application Support/rmk/config.toml`, and on Linux normally
`~/.config/rmk/config.toml`.

### 2. Reuse the existing desktop source and choose notebooks

The existing configuration must select the local desktop source, for example:

```toml
[remarkable]
transport = "local"
root = "/Users/YOUR_USER/Library/Containers/com.remarkable.desktop/Data/Library/Application Support/remarkable/desktop"
```

Use the actual source path already configured for that Mac. Keep the reMarkable
desktop app connected to the intended existing account and allow the test
notebook to sync before validating changes.

| Setting | Behavior |
| --- | --- |
| `RMK_MCP_NOTEBOOK_IDS` | JSON array of permitted notebook UUIDs. If unset, use the existing runtime notebook selection when present. An empty selection denies page-content access; metadata discovery remains available. |
| `RMK_MCP_UPLOAD_ROOT` | Optional bounded host folder for existing PDF/EPUB files. Without it, use built-in document preparation; arbitrary host files are not uploadable. |
| `RMK_MCP_TOKEN` | Required only for HTTP mode: at least 32 printable ASCII characters without spaces. Use a cryptographically random value and supply it through the host's private service environment, never the package. |

Use `remarkable_browse` to find the requested notebook's UUID, then configure
the selection for the server process. For example, after replacing the UUID:

```bash
export RMK_MCP_NOTEBOOK_IDS='["NOTEBOOK-UUID"]'
export RMK_MCP_UPLOAD_ROOT="/Users/YOUR_USER/reMarkable-Outbox"
```

Create and select the intended upload folder before using it. A file path is
local to the Mac running the server. A cloud-chat attachment must first reach
that host through a supported transfer before `prepare_upload` can use it.

The prepared outbox, captured page images, credentials, and processing receipts
belong in private runtime state outside the source repository and plugin archive.

### 3. Connect Codex locally

Use the installed Codex version's supported local-plugin/marketplace flow;
consult that installation's help for its supported registration command. The
package's `mcp.json` provides the complete stdio command:

```bash
uv run --directory /absolute/path/remarkable-companion/server -- python -m rmk.mcp_server
```

The host manages this process and exchanges MCP messages over stdin/stdout.
Put environment settings in the host's supported process configuration; a GUI
application may not inherit variables exported in a terminal. Once connected,
verify tool discovery and call `remarkable_status` before reading a notebook.

For upload, reuse the Mac's registered `rmapi`. A successful `rmapi ls` confirms
that its current account connection can list the library. If registration is
missing, complete the existing `rmapi` login flow locally; keep the one-time code
and token files out of chat and the repository.

### Optional: a remote connection

HTTP mode is a separate deployment step. It requires `RMK_MCP_TOKEN` and binds
to loopback on the Mac:

```bash
uv run --directory server -- python -m rmk.mcp_server --http --port 8793
```

Connect a remote host only after installing the runtime on the target Mac and
verifying an authenticated tunnel or equivalent authenticated transport to its
`http://127.0.0.1:8793/mcp` endpoint. Configure the verified remote endpoint and
bearer authentication in the remote host. The upstream HTTP `Host` must remain
`127.0.0.1:8793`; `Origin` must be absent or exactly `http://127.0.0.1:8793`.
Verify that the tunnel supports these requirements before connecting it. Use
MCP discovery or ping as the connection probe; there is no GET health route.
No tunnel ID or remote endpoint is included in this package. Verify remote
tool discovery and a real call before describing it as connected.

## Acceptance and remaining limits

Source tests and a packaged MCP startup establish implementation behavior. A
live Mac/account test is still required to establish installation, source
compatibility, and tablet delivery. Use this small acceptance sequence:

1. Select a test notebook, capture its baseline, and read a page including all
   tiles. Acknowledge only after the baseline review succeeds.
2. Add a line to an older page and sync the desktop app. Capture again: that
   page should appear as edited. Read and acknowledge the new revision.
3. Repeat the capture without editing: the consumer should have no new changes.
   A second consumer's cursor should remain independent.
4. Prepare and post a one-page brief. Inspect its receipt, repeat the same
   request ID, and confirm that only one document exists. Verify its arrival
   on the tablet separately.
5. For an existing test PDF, add only that document to the Companion selection.
   Capture and read its current background and handwriting as a baseline. After
   acknowledgement, add one new handwritten mark, sync tablet and desktop,
   capture again, and verify its page is in `annotations_edited`. Read the new
   mark, acknowledge, then repeat without editing to establish an empty delta.
   This test reuses the existing PDF; do not create a second upload.

Version `0.2.0` adds the bounded PDF annotation support described above. EPUB
reading remains unsupported. Uploading PDF/EPUB files is supported separately. Native notebook editing,
inserting pages into an existing notebook, and a built-in scheduler are outside
this version. The plugin does not replace the existing daily intake schedule.
