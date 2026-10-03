"""Render reMarkable ``.rm`` stroke files to PDF and PNG.

The reMarkable 2 stores handwriting in the block-based ``.rm`` v6 format. Pipeline:

    .rm  --rmc-->  SVG  --svglib/reportlab-->  PDF  --pypdfium2-->  PNG

We deliberately do NOT use ``rmc``'s own PDF export: it shells out to Inkscape
(a heavy external app) and silently writes a 0-byte PDF when Inkscape is absent.
``rmc``'s SVG export is pure-Python and reliable, so we rasterise from there with
``svglib``+``reportlab`` (SVG→PDF) and ``pypdfium2`` (PDF→PNG) — all pip-only,
no system libraries (Cairo/Inkscape) required.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pypdfium2 as pdfium
from pypdf import PdfWriter
from reportlab.graphics import renderPDF
from svglib.svglib import svg2rlg


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


def _page_to_svg(rm_bytes: bytes, out_svg: str) -> None:
    with tempfile.NamedTemporaryFile(suffix=".rm", delete=False) as f:
        f.write(rm_bytes)
        tmp_rm = f.name
    try:
        proc = subprocess.run(
            [_rmc_bin(), "-t", "svg", "-o", out_svg, tmp_rm],
            capture_output=True,
            text=True,
        )
        # rmc emits a harmless "some data not read" warning for newer .rm blocks
        # on stderr but still returns 0 and writes a valid SVG. Only a nonzero
        # exit or a missing/empty file is a real failure.
        if proc.returncode != 0 or not os.path.exists(out_svg) or os.path.getsize(out_svg) == 0:
            raise RenderError(
                f"rmc failed to render a page to SVG:\n"
                f"{proc.stderr.strip() or proc.stdout.strip()}"
            )
    finally:
        os.unlink(tmp_rm)


def _page_to_pdf(rm_bytes: bytes, out_pdf: str) -> None:
    with tempfile.TemporaryDirectory() as td:
        svg = os.path.join(td, "page.svg")
        _page_to_svg(rm_bytes, svg)
        drawing = svg2rlg(svg)
        if drawing is None:
            raise RenderError("Could not parse the rendered SVG for a page.")
        renderPDF.drawToFile(drawing, out_pdf)


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
            pil = page.render(scale=scale).to_pil()
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


def pages_to_png_files(pages: list[bytes], out_dir: str, scale: float = 2.0) -> list[str]:
    """Render ``.rm`` pages to ``page-NN.png`` files under ``out_dir``.

    Returns the ordered file paths. Used to hand the note to the agent broker,
    whose vision provider reads image *files* (in its working directory).
    """
    paths: list[str] = []
    for i, data in enumerate(pages_to_pngs(pages, scale=scale), 1):
        p = os.path.join(out_dir, f"page-{i:02d}.png")
        with open(p, "wb") as f:
            f.write(data)
        paths.append(p)
    return paths


# Increment when the capture rendering recipe changes.
CAPTURE_RENDER_VERSION = "ink-tiles-v1"


def tile_ranges(height: int, size: int = 1800, overlap: int = 150):
    """Cover a full page with overlapping sections, including the bottom."""
    if height <= 0 or size <= 0 or not 0 <= overlap < size:
        raise ValueError("Invalid tile dimensions")
    top = 0
    while top < height:
        bottom = min(top + size, height)
        yield top, bottom
        if bottom == height:
            break
        top = bottom - overlap


def render_tiles(data: bytes, output: Path) -> dict:
    """Render full ink bounds using the Career review recipe; no page cap."""
    import math
    import xml.etree.ElementTree as ET

    with tempfile.TemporaryDirectory(prefix="rmk-tiles-") as tmp:
        svg = Path(tmp) / "page.svg"
        _page_to_svg(data, str(svg))
        tree = ET.parse(svg)
        node = tree.getroot()
        x, y, width, height = map(float, node.attrib["viewBox"].split())
        if not all(math.isfinite(v) for v in (x, y, width, height)) or width <= 0 or height <= 0:
            raise RenderError("Invalid SVG ink bounds")
        node.set("viewBox", f"{x-12} {y-12} {width+24} {height+24}")
        node.set("width", str(width + 24))
        node.set("height", str(height + 24))
        tree.write(svg)
        drawing = svg2rlg(str(svg))
        if drawing is None:
            raise RenderError("Could not parse rendered SVG")
        pdf = pdfium.PdfDocument(renderPDF.drawToString(drawing))
        try:
            page = pdf[0]
            try:
                bitmap = page.render(scale=3)
                try:
                    image = bitmap.to_pil().convert("RGB")
                finally:
                    bitmap.close()
            finally:
                page.close()
        finally:
            pdf.close()
    try:
        tiles = []
        for index, (top, bottom) in enumerate(tile_ranges(image.height), 1):
            file = output / f"tile-{index:02d}.png"
            tile = image.crop((0, top, image.width, bottom))
            try:
                tile.save(file)
            finally:
                tile.close()
            file.chmod(0o600)
            tiles.append({"file": file.name, "y_start": top, "y_end": bottom})
        return {"width": image.width, "height": image.height, "tiles": tiles}
    finally:
        image.close()
