# Local capture milestone 1 verification

October 3, 2026 Central. Implementation base: `2e6cbb4` in the Remarkable owner.
No source notebook edits, Broker/model calls, runtime activation or project delivery.

## Synthetic acceptance

The focused snapshot/state/render tests cover baseline and unchanged replay;
new/edited/older edited pages; rename/reorder/deletion/orphans/empty order/missing
pages; malformed schemas and observed desktop integer tombstones; source mutation
during the initial read and rendering; renderer/coverage failures; interrupted
atomic writes and actual process exit; corrupt state/artifacts; overlapping
process locks; reuse of previous/completed revisions; source preservation,
private permissions, output guards, CLI behavior and 25 active pages.

The long-page rasterizer test injects synthetic SVG at the `.rm` parser boundary
and uses the real SVG -> PDF -> PNG stack. Its bottom ink marker survives;
overlapping sections cover the complete page. Capture state tests use synthetic
stroke bytes and a deterministic renderer; they do not claim private notebook
edit tests. Existing Remarkable tests and Career-helper tests are also run.

## Real Air proof

Only selected Thoughts UUID `61d354e3-1e35-4de2-ac78-880592331dd5` was captured.
The existing local desktop configuration was reused. Each command had an outer
180-second bound. Initial capture took 3.870 seconds; replay took 0.230 seconds.

- Four declared active pages produced 16 PNG sections. Three rendered pages
  contain ink through their bottom tiles; the fourth renders blank. Two long
  pages are 10,228 and 9,500 pixels tall. Every tile's actual PNG dimensions
  match the manifest's page width and vertical range.
- Baseline reports no historical pages as newly added. Replay is unchanged with
  source revision `172e7846124f167c9e8682c6f1290977be9e00aeaa023307ae41d1ca959af493`.
- The six selected source files (metadata, content, four active stroke files)
  retain identical SHA-256, size, nanosecond mtime, mode and inode before/after.
- The published Career helper matches the existing local helper SHA-256. The
  extracted renderer produces five pixel-identical tiles against that unchanged
  helper on a synthetic long SVG, with identical dimensions and coordinates.
- A final unchanged replay also passed after adding artifact coverage validation.

Private evidence on the Air:
`/Users/suman/Library/Caches/rmk/milestone-1-20261003-xv27uwif/`.
It contains the real receipt, capture logs, immutable source/renders/manifest,
PNG dimension/bottom-ink checks, synthetic Career renderer comparison and final
replay. Raw content is deliberately absent from this repository.

The real capture initially failed closed on an observed integer deletion marker;
no successful checkpoint was created. The validator now accepts boolean and 0/1
markers, with a regression. That failure led to a source-format correction, not
broader source access.

## Reproduce

From this implementation checkout, reuse the existing Remarkable environment
without changing its unrelated lockfile or reinstalling dependencies:

```bash
PYTHONPATH=src /Users/suman/code/remarkable/.venv/bin/python -m pytest -q
PYTHONPATH=src /Users/suman/code/remarkable/.venv/bin/python -m unittest discover \
  -s /Users/suman/code/pulsar-workspace/projects/career/tests \
  -p test_remarkable_review.py -v
PYTHONPATH=src /Users/suman/code/remarkable/.venv/bin/python -m compileall -q src tests
git diff --check
```

Final verification: focused snapshot/state/render suite **46 passed**; full
Remarkable suite **59 passed**; Career helper **4 tests passed** (including
3 bad-order subcases); compileall and diff whitespace checks passed. The source
commit is supplied in the implementation handoff.
No CI workflow is configured in this repository. Validation runs locally.

## Limits and architecture recommendation

The real source was not edited for testing; edit/retry/concurrency cases are
synthetic. Desktop reads do not verify current tablet/cloud sync, recognize
handwriting, date a reflection, or establish planning usefulness. No later
milestone is complete. Existing broker commands retain their existing 20-page
warning/cap; the new model-free capture path covers every page.

Clarify the capture contract that an explicitly declared empty active order
represents a fully emptied notebook and records removals; missing/malformed
order remains a failure. The initial capture baseline does not choose or read
the monthly reflection. That deliberate first interpretation belongs to
milestone 2. Production source setup/retention and activation remain later work.
