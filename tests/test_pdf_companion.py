"""Synthetic PDF + real v6 bytes through the actual renderer and MCP service.

No private notebooks, network calls, tablet writes or model inference.
"""
import base64
import io
import json
from pathlib import Path
import struct
import uuid

from PIL import Image
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas
import pytest
from rmscene import CrdtId, LwwValue, simple_text_document, write_blocks
from rmscene.crdt_sequence import CrdtSequenceItem
from rmscene.scene_items import GlyphRange, Line, Pen, PenColor, Point, Rectangle
from rmscene.scene_stream import SceneGlyphItemBlock, SceneInfo, SceneLineItemBlock, SceneTombstoneItemBlock

from rmk.capture import CaptureStore
from rmk.companion import Companion, _SourceTransport
from rmk.config import Config
from rmk import pdf_capture, pdf_render
from rmk.pdf_capture import pdf_page_plan
from rmk.pdf_render import PdfError, SCALE, render_pdf_tiles
from rmk.snapshot import SnapshotError

NB, P1, P2, P3, P4 = [str(uuid.UUID(int=n)) for n in (100, 1, 2, 3, 4)]


def source_pdf(*, pages=3, marker=(0.2, 0.5, 0.9), rotation=0, crop=False):
    out = io.BytesIO()
    writer = canvas.Canvas(out, pagesize=(400, 500), invariant=1)
    for i in range(pages):
        writer.setFont("Helvetica", 18)
        writer.drawString(25, 455, f"Original PDF page {i+1}")
        writer.setFillColorRGB(*marker)
        writer.rect(50, 410, 40, 40, fill=1, stroke=0)
        writer.setFillColorRGB(0, 0, 0)
        writer.drawString(25, 20, "Original bottom edge")
        writer.showPage()
    writer.save()
    if rotation or crop:
        result = PdfWriter()
        for page in PdfReader(io.BytesIO(out.getvalue())).pages:
            if rotation:
                page.rotate(rotation)
            if crop:
                page.cropbox.lower_left = (50, 60)
                page.cropbox.upper_right = (350, 460)
            result.add_page(page)
        out = io.BytesIO()
        result.write(out)
    return out.getvalue()


def annotation(*, point=(70, 70), page_width=400, tool=Pen.FINELINER_2,
               text="", highlight=False, paper=None, hidden=False, scene_extra=None):
    """A cross at a known PDF-point location, serialized as real v6 strokes."""
    blocks = list(simple_text_document(text, author_uuid=uuid.UUID(int=42)))
    if hidden:
        blocks[-2].group.visible = LwwValue(CrdtId(1, 90), False)
    if paper is not None:
        blocks.append(SceneInfo(LwwValue(CrdtId(0, 1), CrdtId(0, 11)),
                                LwwValue(CrdtId(0, 1), True), LwwValue(CrdtId(0, 1), True),
                                extra_data=b"\x5c\x08\x00\x00\x00" + struct.pack("<II", *paper)))
    if scene_extra is not None:
        blocks.append(SceneInfo(LwwValue(CrdtId(0, 1), CrdtId(0, 11)),
                                LwwValue(CrdtId(0, 1), True), LwwValue(CrdtId(0, 1), True),
                                extra_data=scene_extra))
    x, y = point
    for i, pts in enumerate(([(x-10, y), (x+10, y)], [(x, y-10), (x, y+10)])):
        line = Line(PenColor.BLACK, tool,
                    [Point((px-page_width/2)/SCALE, py/SCALE, 0, 0, 16, 90) for px, py in pts], 2, 0)
        blocks.append(SceneLineItemBlock(CrdtId(0, 11), CrdtSequenceItem(
            CrdtId(1, 40+i), CrdtId(0, 0) if i == 0 else CrdtId(1, 40), CrdtId(0, 0), 0, line)))
    if highlight:
        glyph = GlyphRange(0, 4, "test", PenColor.YELLOW,
                           [Rectangle((120-page_width/2)/SCALE, 150/SCALE, 100/SCALE, 20/SCALE)])
        blocks.append(SceneGlyphItemBlock(CrdtId(0, 11), CrdtSequenceItem(
            CrdtId(1, 50), CrdtId(1, 41), CrdtId(0, 0), 0, glyph)))
    out = io.BytesIO()
    write_blocks(out, blocks, {"version": "3.1"})
    return out.getvalue()


def modern_content():
    return {"fileType": "pdf", "formatVersion": 2, "cPages": {"pages": [
        {"id": P1, "redir": {"value": 2}},
        {"id": P4, "redir": {"value": 1}, "deleted": {"value": 1}},
        {"id": P2}, {"id": P3, "redir": {"value": 0}}]}, "redirectionPageMap": [0, 1, 2]}


@pytest.fixture
def service(tmp_path):
    root = tmp_path / "source"
    (root / NB).mkdir(parents=True)
    (root / f"{NB}.metadata").write_text(json.dumps({"type": "DocumentType", "visibleName": "PDF annotation test"}))
    (root / f"{NB}.content").write_text(json.dumps(modern_content()))
    (root / f"{NB}.pdf").write_bytes(source_pdf())
    (root / NB / f"{P1}.rm").write_bytes(annotation(highlight=True))
    (root / NB / f"{P2}.rm").write_bytes(annotation(page_width=1404*SCALE))
    (root / NB / f"{P4}.rm").write_bytes(b"Deleted page must never be read")
    return Companion(config=Config(transport="local", root=str(root)), notebook_ids=[NB],
                     state_dir=tmp_path / "capture", runtime_file=tmp_path / "absent.json")


def call(service, name, **args):
    result = service.call_tool("remarkable_" + name, args)
    assert not result["isError"], result
    return result["structuredContent"]


def capture(service):
    return call(service, "capture", notebook_id=NB)


def acknowledge(service, result):
    return call(service, "acknowledge", notebook_id=NB, revision=result["revision"],
                expected_previous_revision=result["expected_previous_revision"])


def failure(service):
    result = service.call_tool("remarkable_capture", {"notebook_id": NB})
    assert result["isError"], result
    return json.loads(result["content"][0]["text"])["error"]


def test_modern_page_mapping_is_authoritative_and_deleted_page_is_excluded():
    assert pdf_page_plan(modern_content(), 3) == {P1: 2, P2: None, P3: 0}


@pytest.mark.parametrize("redirect,expected", [(None, [0, 1, 2]), ([], [0, 1, 2]), ([2, -1, 0], [2, None, 0])])
def test_legacy_mapping(redirect, expected):
    content = {"fileType": "pdf", "pages": [P1, P2, P3], "redirectionPageMap": redirect}
    assert list(pdf_page_plan(content, 3).values()) == expected


@pytest.mark.parametrize("patch", [
    {"redirectionPageMap": [0]}, {"redirectionPageMap": [0, 1, 3]},
    {"redirectionPageMap": [False, 1, 2]}, {"redirectionPageMap": [-2, 1, 2]},
    {"transform": {"m11": 2}}, {"orientation": "landscape"}, {"zoomMode": "customFit"},
    {"formatVersion": True}, {"formatVersion": 3},
    {"cPages": {"pages": [{"id": P1, "redir": None}]}},
    {"cPages": {"pages": [{"id": P1, "template": {"value": "Grid"}}]}},
])
def test_unsupported_or_ambiguous_mapping_is_rejected(patch):
    with pytest.raises(PdfError):
        pdf_page_plan({"fileType": "pdf", "pages": [P1, P2, P3], **patch}, 3)


def test_real_pdf_capture_contains_original_annotations_and_sparse_pages(service):
    root = Path(service._config.root)
    originals = {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert call(service, "status")["supported_document_types"] == ["notebook", "pdf"]
    assert call(service, "browse")["documents"][0]["readable"]
    result = capture(service)
    assert result["document_type"] == "pdf" and result["changes"]["baseline"]
    assert [p["id"] for p in result["pages"]] == [P1, P2, P3]
    assert result["pages"][0]["pdf_page_index"] == 2
    assert result["pages"][1]["background"] == "inserted_blank"
    assert result["pages"][2]["annotation_presence"] == "absent_in_local_source"
    response = service.call_tool("remarkable_read_page", {"notebook_id": NB, "revision": result["revision"], "page_id": P1})
    image = Image.open(io.BytesIO(base64.b64decode(response["content"][1]["data"]))).convert("RGB")
    assert max(image.getpixel((210, 210))) < 30  # v6 cross exactly over the PDF marker
    blue = image.getpixel((175, 175))
    assert blue[2] > 180 and blue[0] < 80  # original PDF remains visible under the overlay
    assert image.getpixel((390, 465))[2] < 235  # visible smart highlight on the PDF
    assert image.size == (1200, 1500)
    image.close()
    assert originals == {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert not list(service._state_dir.rglob("intake-state.json"))


def test_annotation_delta_historical_read_and_no_duplicate_delta(service):
    initial = capture(service)
    acknowledge(service, initial)
    (Path(service._config.root) / NB / f"{P1}.rm").write_bytes(annotation(point=(120, 120)))
    current = capture(service)
    assert current["changes"]["edited"] == current["changes"]["annotations_edited"] == [P1]
    assert current["changes"]["background_changed"] == []
    prior_page = call(service, "read_page", notebook_id=NB, revision=initial["revision"], page_id=P1)
    assert prior_page["revision"] == initial["revision"]
    acknowledge(service, current)
    assert capture(service)["changes"]["edited"] == []
    assert call(service, "capture", notebook_id=NB, consumer="other")["changes"]["baseline"]


def test_viewport_metadata_does_not_translate_or_scale_known_pdf_ink(tmp_path):
    from test_scene_info import modern_metadata
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir(); new.mkdir()
    first = render_pdf_tiles(source_pdf(), 0, annotation(), old)
    second = render_pdf_tiles(source_pdf(), 0, annotation(scene_extra=modern_metadata(include_flag=True)), new)
    assert (first["width"], first["height"]) == (second["width"], second["height"])
    assert len(first["tiles"]) == len(second["tiles"])
    for a, b in zip(first["tiles"], second["tiles"]):
        assert (old / a["file"]).read_bytes() == (new / b["file"]).read_bytes()
    assert second["scene_info"][0]["fields"] == [5, 6, 7, 8, 9]
    image = Image.open(new / second["tiles"][0]["file"]).convert("RGB")
    assert max(image.getpixel((210, 210))) < 30
    assert image.getpixel((175, 175))[2] > 180
    image.close()


def test_unqualified_scene_profile_preserves_all_checkpoints(service):
    from test_scene_info import modern_metadata
    initial = capture(service)
    acknowledge(service, initial)
    store = service._store(NB)
    before_state = (store.directory / "state.json").read_bytes()
    before_reader = service._reader_path(store, "codex").read_bytes()
    (Path(service._config.root) / NB / f"{P1}.rm").write_bytes(annotation(scene_extra=modern_metadata(include_flag=True, flag=2)))
    result = service.call_tool("remarkable_capture", {"notebook_id": NB})
    assert result["isError"]
    error = json.loads(result["content"][0]["text"])
    assert error["error"] == "unsupported_pdf_annotations" and "field 9" in error["message"]
    assert (store.directory / "state.json").read_bytes() == before_state
    assert service._reader_path(store, "codex").read_bytes() == before_reader


def test_previous_pdf_recipe_revision_remains_readable_after_upgrade(service, monkeypatch):
    monkeypatch.setattr(pdf_capture, "PDF_RENDER_VERSION", "pdf-v6-tiles-v1")
    old = capture(service)
    acknowledge(service, old)
    monkeypatch.setattr(pdf_capture, "PDF_RENDER_VERSION", "pdf-v6-tiles-v2")
    page = call(service, "read_page", notebook_id=NB, revision=old["revision"], page_id=P1)
    assert page["revision"] == old["revision"] and page["covers_entire_page"]
    current = capture(service)
    assert current["revision"] != old["revision"]
    assert current["expected_previous_revision"] == old["revision"]
    assert current["changes"]["annotations_edited"] == []
    assert service._reader(service._store(NB), "codex")["revision"] == old["revision"]


def test_new_annotation_file_on_previously_unmarked_page_is_detected(service):
    initial = capture(service)
    acknowledge(service, initial)
    (Path(service._config.root) / NB / f"{P3}.rm").write_bytes(annotation())
    assert capture(service)["changes"]["annotations_edited"] == [P3]


def test_background_change_is_separate_from_handwriting(service):
    initial = capture(service)
    acknowledge(service, initial)
    (Path(service._config.root) / f"{NB}.pdf").write_bytes(source_pdf(marker=(0.8, 0.1, 0.2)))
    delta = capture(service)["changes"]
    assert delta["edited"] == delta["background_changed"] == [P1, P3]
    assert delta["annotations_edited"] == []


def test_reorder_and_remapping_are_not_mislabeled_as_handwriting(service):
    initial = capture(service)
    acknowledge(service, initial)
    content = modern_content()
    content["cPages"]["pages"][0]["redir"]["value"] = 1
    content["cPages"]["pages"].reverse()
    (Path(service._config.root) / f"{NB}.content").write_text(json.dumps(content))
    delta = capture(service)["changes"]
    assert delta["reordered"] and delta["page_mapping_changed"] == [P1]
    assert delta["annotations_edited"] == []


@pytest.mark.parametrize("bad", [b"", b"not a v6 annotation", annotation(text="Typed annotation"),
                                 annotation(tool=Pen.ERASER), annotation(paper=(1620, 2160))],
                         ids=["empty", "damaged", "typed", "eraser", "uncalibrated-grid"])
def test_failed_annotation_capture_preserves_reader_and_capture_checkpoint(service, bad):
    initial = capture(service)
    acknowledge(service, initial)
    store = service._store(NB)
    before = (store.directory / "state.json").read_bytes()
    (Path(service._config.root) / NB / f"{P1}.rm").write_bytes(bad)
    assert failure(service) in ("unsupported_pdf_annotations", "pdf_source_incomplete")
    assert (store.directory / "state.json").read_bytes() == before
    assert service._reader(store, "codex")["revision"] == initial["revision"]


def test_annotation_appearing_during_capture_is_not_lost(service, monkeypatch):
    original_render = pdf_capture.render_pdf_tiles
    missing = Path(service._config.root) / NB / f"{P3}.rm"
    def render_and_sync(*args):
        result = original_render(*args)
        missing.write_bytes(annotation())
        return result
    monkeypatch.setattr(pdf_capture, "render_pdf_tiles", render_and_sync)
    assert failure(service) == "pdf_source_changed"
    assert not (service._store(NB).directory / "state.json").exists()


def test_background_changing_during_capture_is_not_published(service, monkeypatch):
    original_render = pdf_capture.render_pdf_tiles
    def render_and_sync(*args):
        result = original_render(*args)
        (Path(service._config.root) / f"{NB}.pdf").write_bytes(source_pdf(marker=(0.8, 0.1, 0.1)))
        return result
    monkeypatch.setattr(pdf_capture, "render_pdf_tiles", render_and_sync)
    assert failure(service) == "capture_unavailable"
    assert not (service._store(NB).directory / "state.json").exists()


def test_missing_pdf_never_falls_back_to_ink_only(service):
    (Path(service._config.root) / f"{NB}.pdf").unlink()
    assert failure(service) == "invalid_file"


def test_pdf_scope_is_enforced_and_native_capture_still_rejects_pdf(service):
    root = Path(service._config.root)
    native = CaptureStore(service._state_dir, _SourceTransport(root), str(root), NB)
    with pytest.raises(SnapshotError):
        native.capture()
    service._notebook_ids = []
    assert failure(service) == "notebook_not_selected"


def test_optional_annotation_read_does_not_accept_symlinks(service, tmp_path):
    outside = tmp_path / "outside.rm"
    outside.write_bytes(annotation())
    (Path(service._config.root) / NB / f"{P3}.rm").symlink_to(outside)
    assert failure(service) == "invalid_file"


def test_margin_annotations_and_page_bottom_survive_tiling(tmp_path):
    rendered = render_pdf_tiles(source_pdf(pages=1), 0, annotation(point=(70, 700)), tmp_path)
    assert rendered["height"] > 2100 and len(rendered["tiles"]) == 2
    assert rendered["tiles"][-1]["y_end"] == rendered["height"]
    tile = rendered["tiles"][-1]
    with Image.open(tmp_path / tile["file"]) as image:
        assert max(image.getpixel((210, 2100-tile["y_start"]))) < 30


def test_scene_paper_size_extension_and_hidden_layers(tmp_path):
    rendered = render_pdf_tiles(source_pdf(pages=1), 0, annotation(paper=(1404, 1872), hidden=True), tmp_path)
    with Image.open(tmp_path / rendered["tiles"][0]["file"]) as image:
        assert image.getpixel((210, 210))[2] > 180  # hidden ink does not cover the PDF


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_rotated_pdf_background_geometry(rotation, tmp_path):
    width, height = (500, 400) if rotation in (90, 270) else (400, 500)
    rendered = render_pdf_tiles(source_pdf(pages=1, rotation=rotation), 0,
                                annotation(point=(150, 150), page_width=width), tmp_path)
    assert (rendered["width"], rendered["height"]) == (width*3, height*3)
    with Image.open(tmp_path / rendered["tiles"][0]["file"]) as image:
        assert max(image.getpixel((450, 450))) < 30
        bx, by = {0: (70, 70), 90: (430, 70), 180: (330, 430), 270: (70, 330)}[rotation]
        assert image.getpixel((bx*3, by*3))[2] > 180


def test_cropbox_background_and_annotations_use_same_display_origin(tmp_path):
    rendered = render_pdf_tiles(source_pdf(pages=1, crop=True), 0,
                                annotation(point=(30, 30), page_width=300), tmp_path)
    assert (rendered["width"], rendered["height"]) == (900, 1200)
    with Image.open(tmp_path / rendered["tiles"][0]["file"]) as image:
        assert max(image.getpixel((90, 90))) < 30
        assert image.getpixel((30, 40))[2] > 180


def test_oversized_canvas_fails_before_allocating_bitmap(tmp_path):
    with pytest.raises(PdfError, match="render size"):
        render_pdf_tiles(source_pdf(pages=1), 0, annotation(point=(70, 30000)), tmp_path)


def test_unknown_annotation_block_is_not_silently_dropped(tmp_path):
    unknown = struct.pack("<IBBBB", 0, 0, 1, 1, 99)
    with pytest.raises(PdfError, match="unknown or damaged"):
        render_pdf_tiles(source_pdf(pages=1), 0, annotation() + unknown, tmp_path)


def test_unimplemented_tombstone_deletion_is_not_ignored(tmp_path):
    block = SceneTombstoneItemBlock(CrdtId(0, 11), CrdtSequenceItem(
        CrdtId(1, 99), CrdtId(1, 41), CrdtId(0, 0), 1, None))
    out = io.BytesIO()
    write_blocks(out, [block], {"version": "3.1"})
    from rmscene.tagged_block_common import HEADER_V6
    with pytest.raises(PdfError, match="unsupported text or drawing extension"):
        render_pdf_tiles(source_pdf(pages=1), 0, annotation() + out.getvalue()[len(HEADER_V6):], tmp_path)
