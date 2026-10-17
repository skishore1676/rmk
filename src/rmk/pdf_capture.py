"""Opt-in PDF captures for Companion; the scheduled notebook engine stays native.

PDF bytes, physical page bindings and sparse annotation files are one immutable
revision. A missing annotation file is an observation of the desktop source,
never evidence that the tablet has no handwriting or has finished syncing.
"""
from __future__ import annotations

from dataclasses import dataclass
import json

from .capture import CaptureError, CaptureStore, _json_bytes
from .pdf_render import PDF_RENDER_VERSION, PdfError, pdf_page_count, render_pdf_tiles
from .snapshot import Snapshot, active_pages, checked_id, digest, read_snapshot

MAX_ACTIVE_PDF_PAGES = 200
_IDENTITY = {f"m{i}{j}": int(i == j) for i in range(1, 4) for j in range(1, 4)}


def pdf_page_plan(content: dict, original_page_count: int) -> dict[str, int | None]:
    """Authoritative UI order: cPages.redir, otherwise the legacy redirect map.

    A cPages item without redir is an inserted page, even when a stale legacy
    map remains. Never compact sparse .rm files or guess numeric page IDs.
    """
    if content.get("fileType") != "pdf":
        raise PdfError("This document is not a PDF.", "unsupported_document_type")
    if content.get("formatVersion") is not None and (type(content["formatVersion"]) is not int or content["formatVersion"] not in (1, 2)):
        raise PdfError("This PDF uses an unsupported page-list version.")
    if content.get("orientation") not in (None, "", "portrait"):
        raise PdfError("PDFs with a landscape document setting need an alignment check before this renderer can read them.")
    transform = content.get("transform")
    if transform is not None and (not isinstance(transform, dict) or set(transform) != set(_IDENTITY)
            or any(type(transform[k]) not in (int, float) or transform[k] != v for k, v in _IDENTITY.items())):
        raise PdfError("A custom PDF transform is not supported; use a tablet-exported annotated PDF for this layout.")
    if content.get("zoomMode") not in (None, "", "bestFit", "fitToHeight", "fitToWidth"):
        raise PdfError("A custom PDF crop or zoom layout needs an alignment check before capture.")
    ids = active_pages(content)
    if len(ids) > MAX_ACTIVE_PDF_PAGES:
        raise PdfError("PDF capture supports up to 200 active pages; export a smaller document for review.", "pdf_too_large")
    if "cPages" in content:
        active = set(ids)
        entries = {}
        for item in content["cPages"]["pages"]:
            deleted = item.get("deleted", False)
            deleted = deleted.get("value") if isinstance(deleted, dict) else deleted
            if not deleted and item.get("id") in active:
                entries[item["id"]] = item
        mapping = []
        for page_id in ids:
            item = entries[page_id]
            value = item.get("redir", {"value": -1})
            if not isinstance(value, dict) or "value" not in value:
                raise PdfError("The PDF page redirection is malformed.")
            index = value["value"]
            template = item.get("template", {"value": "Blank"})
            if index == -1 and (not isinstance(template, dict) or template.get("value") not in ("", "Blank")):
                raise PdfError("An inserted PDF page uses a template that this renderer cannot reproduce.")
            mapping.append(index)
    else:
        mapping = content.get("redirectionPageMap")
        if mapping is None or mapping == []:
            mapping = list(range(len(ids)))
        if not isinstance(mapping, list) or len(mapping) != len(ids):
            raise PdfError("The PDF redirection map does not match its declared page order.")
    plan = {}
    for page_id, index in zip(ids, mapping):
        if type(index) is not int or index < -1 or index >= original_page_count:
            raise PdfError("A PDF page points outside the synced original. Sync the document and retry.", "pdf_source_incomplete")
        plan[page_id] = None if index == -1 else index
    return plan


def _page_source(page_id, index, source_hashes, notebook_id):
    annotation = source_hashes.get(f"{notebook_id}/{page_id}.rm")
    background = source_hashes[f"{notebook_id}.pdf"] if index is not None else None
    identity = {"annotation_sha256": annotation, "background_sha256": background, "pdf_page_index": index}
    return {**identity, "sha256": digest(_json_bytes(identity)),
            "background": "pdf" if index is not None else "inserted_blank",
            "annotation_presence": "present" if annotation is not None else "absent_in_local_source"}


@dataclass
class PdfSnapshot(Snapshot):
    content: dict
    page_map: dict[str, int | None]
    pdf_bytes: bytes
    original_page_count: int
    missing_annotations: list[str]

    def verify(self, transport, root):
        super().verify(transport, root)
        for path in self.missing_annotations:
            if transport.read_optional_bytes(f"{root.rstrip('/')}/{path}") is not None:
                raise PdfError("An annotation file appeared during capture. Let syncing finish and retry.", "pdf_source_changed")


def read_document_snapshot(transport, root, notebook_id):
    checked_id(notebook_id)
    root = root.rstrip("/")
    content_bytes = transport.read_bytes(f"{root}/{notebook_id}.content")
    content = json.loads(content_bytes)
    if not isinstance(content, dict) or content.get("fileType") != "pdf":
        return read_snapshot(transport, root, notebook_id)
    metadata_bytes = transport.read_bytes(f"{root}/{notebook_id}.metadata")
    metadata = json.loads(metadata_bytes)
    if (not isinstance(metadata, dict) or metadata.get("type") != "DocumentType"
            or metadata.get("deleted") or metadata.get("parent") == "trash"):
        raise PdfError("The selected PDF is unavailable or deleted.", "pdf_source_incomplete")
    pdf_bytes = transport.read_bytes(f"{root}/{notebook_id}.pdf")
    count = pdf_page_count(pdf_bytes)
    plan = pdf_page_plan(content, count)
    observed = {f"{notebook_id}.content": content_bytes, f"{notebook_id}.metadata": metadata_bytes,
                f"{notebook_id}.pdf": pdf_bytes}
    pages, missing = [], []
    input_bytes = sum(map(len, observed.values()))
    for page_id in plan:
        path = f"{notebook_id}/{page_id}.rm"
        data = transport.read_optional_bytes(f"{root}/{path}")
        if data is None:
            missing.append(path)
        else:
            if not data:
                raise PdfError("An annotation file is empty. Let the desktop app finish syncing and retry.", "pdf_source_incomplete")
            observed[path] = data
            input_bytes += len(data)
            if input_bytes > 200 * 1024 * 1024:
                raise PdfError("This PDF and its annotations exceed the supported capture input size.", "pdf_too_large")
        pages.append((page_id, data))
    result = PdfSnapshot(notebook_id, metadata, pages, observed, content, plan, pdf_bytes, count, missing)
    result.verify(transport, root)
    return result


class CompanionCaptureStore(CaptureStore):
    """Native revisions remain byte-compatible; only Companion opts into PDFs."""
    def _snapshot(self):
        return read_document_snapshot(self.transport, self.root, self.notebook_id)

    def _render_version(self, snapshot):
        return PDF_RENDER_VERSION if isinstance(snapshot, PdfSnapshot) else super()._render_version(snapshot)

    def _render_page(self, snapshot, page_id, data, output):
        if not isinstance(snapshot, PdfSnapshot):
            return super()._render_page(snapshot, page_id, data, output)
        index = snapshot.page_map[page_id]
        return {"id": page_id, **_page_source(page_id, index, snapshot.source_hashes, self.notebook_id),
                **render_pdf_tiles(snapshot.pdf_bytes, index, data, output)}

    def _snapshot_fields(self, snapshot):
        if not isinstance(snapshot, PdfSnapshot):
            return {}
        return {"document_type": "pdf", "original_pdf_page_count": snapshot.original_page_count,
                "missing_annotation_files": snapshot.missing_annotations,
                "coverage": "All declared active pages with mapped PDF backgrounds and supported visible annotations; inserted blank pages retained. Missing .rm files mean absent in the local source, not verified empty on the tablet."}

    def _validate_sources(self, directory, manifest):
        if manifest.get("document_type") != "pdf":
            return super()._validate_sources(directory, manifest)
        if manifest["render_version"] not in {"pdf-v6-tiles-v1", PDF_RENDER_VERSION}:
            raise CaptureError("Unsupported PDF rendering revision")
        content = json.loads((directory / "source" / f"{self.notebook_id}.content").read_bytes())
        count = manifest["original_pdf_page_count"]
        if type(count) is not int or not 1 <= count <= 2000:
            raise CaptureError("Invalid PDF source page count")
        plan = pdf_page_plan(content, count)
        if list(plan) != manifest["active_order"]:
            raise CaptureError("PDF source page order does not match the revision")
        hashes = manifest["source_hashes"]
        source_paths = [f"{self.notebook_id}.{suffix}" for suffix in ("metadata", "content", "pdf")]
        missing = []
        for page in manifest["pages"]:
            page_id = page["id"]
            path = f"{self.notebook_id}/{page_id}.rm"
            if path in hashes:
                source_paths.append(path)
            else:
                missing.append(path)
            for key, expected in _page_source(page_id, plan[page_id], hashes, self.notebook_id).items():
                if page.get(key) != expected:
                    raise CaptureError("PDF page source identity mismatch")
        if set(hashes) != set(source_paths) or manifest.get("missing_annotation_files") != missing:
            raise CaptureError("Incomplete PDF revision source coverage")
        return source_paths


def pdf_changes(previous, current):
    """Separate handwriting edits from conservative background-file changes."""
    if current.get("document_type") != "pdf":
        return {}
    before = {p["id"]: p for p in previous["pages"]} if previous else {}
    common = [(before[p["id"]], p) for p in current["pages"] if p["id"] in before]
    return {
        "annotations_edited": [b["id"] for a, b in common if a.get("annotation_sha256") != b["annotation_sha256"]],
        "background_changed": [b["id"] for a, b in common if a.get("background_sha256") != b["background_sha256"]],
        "page_mapping_changed": [b["id"] for a, b in common if a.get("pdf_page_index") != b["pdf_page_index"]],
    }
