# Deploy PDF annotation reading to the existing Companion connection

Version 0.2 keeps the same nine tools and adds a bounded annotated-PDF capture
path. Deploy the server to the machine that holds the existing reMarkable
desktop source. Updating the account plugin or its skill alone does not restart
that remote process.

## Preserve the working installation

Inspect the installed plugin's effective MCP configuration to locate the
actual SSH command, server directory, Python environment and launcher. Keep
the current connection, SSH identity, selected native notebook, source path,
runtime file, daily schedule, Broker/Pulsar configuration, posting state and
reader checkpoints. Do not replace the deployed MCP configuration with the
repository's generic local-stdio example.

Use an isolated checkout if the host's source has unrelated edits. Fetch the
reviewed version from `skishore1676/rmk`, branch
`codex/remarkable-companion-plugin`, PR #7. Record the exact fetched commit.
If the installed service uses the source checkout, update its code through
the existing deployment method. If it uses the bundled standalone server,
build `scripts/build_plugin.py`, then deploy the new `server/` payload using
the existing launcher and environment. The standalone package has no Broker
dependency and this release adds no new runtime dependencies.

Reload the existing Companion MCP session after deploying the server. Confirm
that discovery still exposes nine tools, then call `remarkable_status` and
require version `0.2.0`, `pdf` in `supported_document_types`, and the expected
existing source/selection. Stop if the running process still reports an older
version; do not infer a successful host upgrade from the account plugin version.

## Reuse the already-uploaded test PDF

Resolve its exact stable UUID with `remarkable_browse`. Add only that requested
test document alongside the existing selected notebook in the Companion
launcher selection. Do not change the scheduled intake's notebook or reset any
cursor. The source files need to be synced to the desktop app on the server host.

1. Capture the PDF and read every relevant tile. Verify that the expected PDF
   text and the user's existing handwriting are both visible and correctly
   aligned. The first successful PDF review is its baseline, regardless of
   when those marks were written. Acknowledge only after that review.
2. Have the user add a fresh short handwritten mark after the baseline. Sync
   the tablet and desktop app, capture again, and require that page in
   `annotations_edited`. Read the exact mark from the new immutable revision.
3. Acknowledge, then repeat without editing; the annotation delta should be
   empty. Check the native notebook still reads normally and the existing
   daily intake configuration and checkpoints were preserved.

Do not invoke preparation/posting tools during this upgrade test: the PDF
already exists. If rendering fails, preserve all checkpoints and report the
error code. Share only nonsecret format details needed to reproduce the gap
(file type, format version, page mapping shape, paper grid, renderer error),
not private page contents or credentials. A tablet-exported annotated PDF is
a separate fallback for layouts outside this renderer's supported coverage.

## Evidence to report

Report the server commit/version, successful tool discovery, the document and
page reviewed, baseline versus later change result, and actual image-reading
evidence. Distinguish source sync from physical-tablet confirmation. Synthetic
test success establishes the implementation; only this host/device round trip
establishes compatibility with the user's real annotated document.
