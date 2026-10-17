"""Compose a mapped source PDF page and supported v6 annotations, without fitting
ink bounds to the PDF. Center-origin stroke coordinates retain the rmc 72/226
point scale. The canvas expands for margin notes instead of clipping them.

This deliberately fails on unknown annotation blocks, custom paper grids,
typed annotation layouts and eraser strokes. It never silently returns a bare
PDF when an existing annotation layer could not be rendered.
"""
from __future__ import annotations

import io
import math
from pathlib import Path
import re
import struct

from PIL import Image
import pypdfium2 as pdfium
from reportlab.graphics import renderPDF
from svglib.svglib import svg2rlg

from .render import tile_ranges

PDF_RENDER_VERSION = "pdf-v6-tiles-v1"
SCALE = 72.0 / 226.0
RASTER_SCALE = 3
MAX_PAGE_PIXELS = 40_000_000


class PdfError(ValueError):
    def __init__(self, message, code="unsupported_pdf_layout"):
        super().__init__(message)
        self.code = code


def pdf_page_count(data: bytes) -> int:
    if not data.startswith(b"%PDF-") or b"%%EOF" not in data[-2048:]:
        raise PdfError("The original PDF is missing or incomplete. Let the desktop app sync and retry.", "pdf_source_incomplete")
    try:
        with pdfium.PdfDocument(data) as pdf:
            count = len(pdf)
            if not 1 <= count <= 2000:
                raise PdfError("The source PDF has an unsupported page count.", "pdf_too_large")
            return count
    except PdfError:
        raise
    except Exception as exc:
        raise PdfError("The original PDF could not be opened; encrypted or damaged PDFs are unsupported.", "pdf_source_incomplete") from exc


def _annotation_layer(data):
    """Return SVG body and point-space bounds from visible, understood objects."""
    from rmc.exporters import svg as rmc_svg
    from rmc.exporters.writing_tools import RM_PALETTE
    from rmscene import SceneTree, read_blocks
    from rmscene import scene_items as si
    from rmscene.scene_stream import SceneInfo, SceneTextItemBlock, SceneTombstoneItemBlock, UnreadableBlock, build_tree
    from rmscene.text import TextDocument

    blocks = list(read_blocks(io.BytesIO(data)))
    for block in blocks:
        if isinstance(block, UnreadableBlock):
            raise PdfError("An annotation uses an unknown or damaged v6 block. Export the annotated PDF from reMarkable for this page.", "unsupported_pdf_annotations")
        if isinstance(block, SceneInfo):
            if block.root_document_visible is not None and not block.root_document_visible.value:
                raise PdfError("This page hides its PDF background; this layout needs a separate review.")
            # rmscene 0.6 retains the newer paper-size subblock as extra_data.
            # Accept only its documented tag 5, eight-byte uint32 pair and the
            # existing 1404x1872 logical grid. Do not guess Paper Pro scaling.
            extra = block.extra_data
            paper = getattr(block, "paper_size", None)
            if extra:
                if len(extra) != 13 or extra[:5] != b"\x5c\x08\x00\x00\x00":
                    raise PdfError("The annotation has an unsupported scene layout.", "unsupported_pdf_annotations")
                paper = struct.unpack("<II", extra[5:])
            if paper is not None and tuple(paper) != (1404, 1872):
                raise PdfError("This annotation uses a paper coordinate grid that needs device calibration.", "unsupported_pdf_annotations")
        elif (isinstance(block, (SceneTextItemBlock, SceneTombstoneItemBlock)) or getattr(block, "extra_value_data", b"")
              or (block.extra_data and block.get_block_type() in (1, 2, 3, 4, 5, 6, 7, 8))):
            raise PdfError("The annotation has an unsupported text or drawing extension.", "unsupported_pdf_annotations")
    tree = SceneTree()
    build_tree(tree, blocks)
    if tree.root_text is not None and any(str(p).strip() for p in TextDocument.from_scene_item(tree.root_text).contents):
        raise PdfError("Typed annotations on PDF pages are not yet supported; export the annotated PDF from reMarkable.", "unsupported_pdf_annotations")
    anchors = rmc_svg.build_anchor_pos(tree.root_text)
    out = io.StringIO()
    xs, ys = [], []
    seen = set()

    def bounds(x, y):
        if not math.isfinite(x) or not math.isfinite(y):
            raise PdfError("An annotation has invalid coordinates.", "unsupported_pdf_annotations")
        xs.append(x * SCALE)
        ys.append(y * SCALE)

    def group(node, offset_x=0.0, offset_y=0.0):
        if node.node_id in seen:
            raise PdfError("An annotation has an invalid group structure.", "unsupported_pdf_annotations")
        seen.add(node.node_id)
        if not node.visible.value:
            return
        if node.anchor_id is not None and node.anchor_id.value not in anchors:
            raise PdfError("An annotation has an unresolved text anchor.", "unsupported_pdf_annotations")
        ax, ay = rmc_svg.get_anchor(node, anchors)
        offset_x, offset_y = offset_x + ax, offset_y + ay
        out.write(f'<g transform="translate({ax*SCALE} {ay*SCALE})">')
        for child in node.children.values():
            if child is None:
                continue
            if isinstance(child, si.Group):
                group(child, offset_x, offset_y)
            elif isinstance(child, si.Line):
                if child.tool in (si.Pen.ERASER, si.Pen.ERASER_AREA):
                    raise PdfError("This annotation contains an eraser stroke that requires native export.", "unsupported_pdf_annotations")
                if not child.points:
                    continue
                if not math.isfinite(child.thickness_scale) or child.thickness_scale <= 0:
                    raise PdfError("An annotation has invalid pen geometry.", "unsupported_pdf_annotations")
                for point in child.points:
                    bounds(point.x + offset_x, point.y + offset_y)
                rmc_svg.draw_stroke(child, out)
            elif isinstance(child, si.GlyphRange):
                color = RM_PALETTE.get(child.color, (247, 232, 81))
                for rectangle in child.rectangles:
                    if rectangle.w < 0 or rectangle.h < 0:
                        raise PdfError("A PDF highlight has invalid geometry.", "unsupported_pdf_annotations")
                    bounds(rectangle.x + offset_x, rectangle.y + offset_y)
                    bounds(rectangle.x + rectangle.w + offset_x, rectangle.y + rectangle.h + offset_y)
                    out.write(f'<rect x="{rectangle.x*SCALE}" y="{rectangle.y*SCALE}" '
                              f'width="{rectangle.w*SCALE}" height="{rectangle.h*SCALE}" '
                              f'fill="rgb{color}" fill-opacity="0.3"/>')
            else:
                raise PdfError("An annotation object is not supported by this renderer.", "unsupported_pdf_annotations")
        out.write("</g>")

    group(tree.root)
    body = out.getvalue()
    widths = [float(width) for width in re.findall(r"stroke-width:([0-9.eE+-]+)", body)]
    if any(not math.isfinite(width) or width < 0 for width in widths):
        raise PdfError("An annotation has invalid stroke widths.", "unsupported_pdf_annotations")
    padding = max([12.0, *[width / 2 + 2 for width in widths]])
    box = (min(xs)-padding, min(ys)-padding, max(xs)+padding, max(ys)+padding) if xs else None
    return body, box


def _raster_page(page, *, transparent=False):
    bitmap = page.render(scale=RASTER_SCALE, fill_color=(255, 255, 255, 0 if transparent else 255))
    try:
        return bitmap.to_pil().convert("RGBA")
    finally:
        bitmap.close()


def _check_geometry(width, height):
    if (not all(math.isfinite(v) and v > 0 for v in (width, height))
            or math.ceil(width * RASTER_SCALE) * math.ceil(height * RASTER_SCALE) > MAX_PAGE_PIXELS
            or max(width, height) * RASTER_SCALE > 30000):
        raise PdfError("This PDF page or its margin notes exceed the supported render size.", "pdf_too_large")


def render_pdf_tiles(pdf_bytes, pdf_page_index, annotation_bytes, output: Path):
    background = image = overlay = None
    try:
        if pdf_page_index is None:
            width, height = 1404 * SCALE, 1872 * SCALE
        else:
            with pdfium.PdfDocument(pdf_bytes) as pdf:
                page = pdf[pdf_page_index]
                try:
                    width, height = page.get_size()
                    _check_geometry(width, height)
                    background = _raster_page(page)
                finally:
                    page.close()
        body, ink_box = _annotation_layer(annotation_bytes) if annotation_bytes is not None else ("", None)
        left, top, right, bottom = -width / 2, 0.0, width / 2, height
        if ink_box is not None:
            left, top = min(left, ink_box[0]), min(top, ink_box[1])
            right, bottom = max(right, ink_box[2]), max(bottom, ink_box[3])
        canvas_width, canvas_height = right - left, bottom - top
        _check_geometry(canvas_width, canvas_height)
        image = Image.new("RGBA", (math.ceil(canvas_width * RASTER_SCALE), math.ceil(canvas_height * RASTER_SCALE)), "white")
        background_x, background_y = round((-width / 2 - left) * RASTER_SCALE), round(-top * RASTER_SCALE)
        if background is not None:
            image.alpha_composite(background, (background_x, background_y))
        if body:
            # Unitless SVG dimensions are CSS pixels (0.75 PDF points in
            # current svglib). Explicit pt units preserve the coordinate scale.
            svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{canvas_width}pt" height="{canvas_height}pt" '
                   f'viewBox="{left} {top} {canvas_width} {canvas_height}">{body}</svg>')
            drawing = svg2rlg(io.BytesIO(svg.encode()))
            if drawing is None:
                raise PdfError("The PDF annotation drawing could not be decoded.", "unsupported_pdf_annotations")
            with pdfium.PdfDocument(renderPDF.drawToString(drawing)) as pdf:
                page = pdf[0]
                try:
                    overlay = _raster_page(page, transparent=True)
                finally:
                    page.close()
            # PDF page boxes are float32 in PDFium. A fractional stroke bound
            # can round the empty outer edge by one pixel; never stretch ink.
            if any(abs(a-b) > 1 for a, b in zip(overlay.size, image.size)):
                raise PdfError("The PDF and annotation canvases did not align.", "unsupported_pdf_annotations")
            image.alpha_composite(overlay)
        tiles = []
        for index, (start, end) in enumerate(tile_ranges(image.height), 1):
            path = output / f"tile-{index:02d}.png"
            tile = image.crop((0, start, image.width, end)).convert("RGB")
            try:
                tile.save(path)
            finally:
                tile.close()
            path.chmod(0o600)
            tiles.append({"file": path.name, "y_start": start, "y_end": end})
        return {"width": image.width, "height": image.height, "tiles": tiles,
                "pdf_background_box_px": [background_x, background_y,
                                          background_x + math.ceil(width * RASTER_SCALE),
                                          background_y + math.ceil(height * RASTER_SCALE)],
                "annotation_rendering": "visible_supported_v6", "coordinate_grid": "1404x1872"}
    except PdfError:
        raise
    except Exception as exc:
        raise PdfError("The PDF or its annotations could not be rendered completely. Use a native annotated PDF export for this page.", "unsupported_pdf_annotations") from exc
    finally:
        for item in (overlay, image, background):
            if item is not None:
                item.close()
