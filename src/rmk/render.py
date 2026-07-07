"""Render reMarkable ``.rm`` stroke files to PDF and PNG.

The reMarkable 2 stores handwriting in the block-based ``.rm`` v6 format. We use
``rmc`` (which wraps ``rmscene``) to convert each page to a one-page PDF, merge
the pages into a notebook PDF with ``pypdf``, and rasterise pages to PNG with
``pypdfium2`` (self-contained — no Cairo/system deps) for the vision model.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pypdfium2 as pdfium
from pypdf import PdfWriter


class RenderError(RuntimeError):
    pass


def _rmc_bin() -> str:
    exe = shutil.which("rmc")
    if exe:
        return exe
    # When installed as a dependency, rmc's console script sits next to the
    # active Python interpreter (same venv/bin).
    cand = Path(sys.executable).parent / "rmc"
    if cand.exists():
        return str(cand)
    raise RenderError(
        "The 'rmc' renderer was not found. Install dependencies with `uv sync` "
        "(or `pip install rmc`)."
    )


def _page_to_pdf(rm_bytes: bytes, out_pdf: str) -> None:
    with tempfile.NamedTemporaryFile(suffix=".rm", delete=False) as f:
        f.write(rm_bytes)
        tmp_rm = f.name
    try:
        proc = subprocess.run(
            [_rmc_bin(), "-t", "pdf", "-o", out_pdf, tmp_rm],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0 or not os.path.exists(out_pdf):
            raise RenderError(
                f"rmc failed to render a page:\n{proc.stderr.strip() or proc.stdout.strip()}"
            )
    finally:
        os.unlink(tmp_rm)


def pages_to_pdf(pages: list[bytes], out_pdf: str) -> str:
    """Render each ``.rm`` page and merge into a single PDF at ``out_pdf``."""
    if not pages:
        raise RenderError("This notebook has no pages to render.")
    writer = PdfWriter()
    with tempfile.TemporaryDirectory() as td:
        for i, rm_bytes in enumerate(pages):
            page_pdf = os.path.join(td, f"page_{i:04d}.pdf")
            _page_to_pdf(rm_bytes, page_pdf)
            writer.append(page_pdf)
        with open(out_pdf, "wb") as fh:
            writer.write(fh)
    return out_pdf


def pdf_to_pngs(pdf_path: str, scale: float = 2.0) -> list[bytes]:
    """Rasterise every page of a PDF to PNG bytes.

    ``scale`` 2.0 gives ~2x the PDF's point resolution — enough for a vision
    model to read handwriting without producing huge payloads.
    """
    doc = pdfium.PdfDocument(pdf_path)
    try:
        out: list[bytes] = []
        for page in doc:
            bitmap = page.render(scale=scale)
            pil = bitmap.to_pil()
            import io

            buf = io.BytesIO()
            pil.save(buf, format="PNG")
            out.append(buf.getvalue())
            page.close()
        return out
    finally:
        doc.close()


def pages_to_pngs(pages: list[bytes], scale: float = 2.0) -> list[bytes]:
    """Convenience: ``.rm`` pages straight to a list of PNG bytes."""
    with tempfile.TemporaryDirectory() as td:
        pdf = os.path.join(td, "notebook.pdf")
        pages_to_pdf(pages, pdf)
        return pdf_to_pngs(pdf, scale=scale)
