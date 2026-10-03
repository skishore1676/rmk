"""Real renderer-stack coverage with synthetic SVG ink and bottom markers."""
from pathlib import Path

from PIL import Image
import pytest

from rmk import render


def test_tile_ranges_cover_bottom_and_overlap():
    ranges = list(render.tile_ranges(8501))
    assert ranges[0][0] == 0 and ranges[-1][1] == 8501
    assert all(a[1] - b[0] == 150 for a, b in zip(ranges, ranges[1:]))
    assert list(render.tile_ranges(1800)) == [(0, 1800)]


@pytest.mark.parametrize("args", [(0,), (10, 0), (100, 10, 10), (100, 10, -1)])
def test_bad_tile_dimensions_fail(args):
    with pytest.raises(ValueError):
        list(render.tile_ranges(*args))


def test_long_page_real_rasterization_has_bottom_ink(tmp_path, monkeypatch):
    def svg(data, output):
        Path(output).write_text('''<svg xmlns="http://www.w3.org/2000/svg"
            width="400" height="3100" viewBox="-50 -100 400 3100">
            <rect x="-20" y="-80" width="60" height="30" fill="black"/>
            <rect x="-20" y="2940" width="60" height="40" fill="black"/>
            </svg>''')
    monkeypatch.setattr(render, "_page_to_svg", svg)
    result = render.render_tiles(b"synthetic SVG injected at parser boundary", tmp_path)
    assert result["height"] > 6000 and len(result["tiles"]) > 4
    assert result["tiles"][-1]["y_end"] == result["height"]
    for a, b in zip(result["tiles"], result["tiles"][1:]):
        assert a["y_end"] - b["y_start"] == 150
    # The distinctive ink at the very bottom survived SVG -> PDF -> PNG.
    with Image.open(tmp_path / result["tiles"][-1]["file"]) as image:
        assert image.convert("L").getextrema()[0] == 0
