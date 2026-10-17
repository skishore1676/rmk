"""Synthetic metadata envelopes only; no private .rm files or handwriting."""
import struct

import pytest

from rmk.scene_info import GRID, SceneInfoError, parse_scene_info


def varuint(value):
    result = bytearray()
    while value >= 128:
        result.append((value & 127) | 128)
        value >>= 7
    return bytes(result) + bytes([value])


def field(index, payload):
    return varuint((index << 4) | 12) + struct.pack("<I", len(payload)) + payload


def lww(payload):
    # Synthetic CRDT timestamp (actor=0, counter=0), then the wrapped value.
    return b"\x1f\x00\x00" + field(2, payload)


def modern_metadata(*, viewport=(-700.0, -300.0, 2200.0, 2500.0),
                    raw=GRID, current=GRID, include_flag=False, flag=1):
    result = (field(5, struct.pack("<II", *GRID))
              + field(6, lww(struct.pack("<dddd", *viewport)))
              + field(7, struct.pack("<dd", *raw))
              + field(8, lww(struct.pack("<dd", *current))))
    return result + field(9, lww(bytes([flag]))) if include_flag else result


def test_current_device_envelope_is_reproduced_without_personal_data():
    # Same tagged shapes/124-byte length as the observed device, but all CRDT
    # timestamps and the viewport are independently synthesized. No pen data.
    data = modern_metadata(include_flag=True)
    assert len(data) == 124
    result = parse_scene_info(data)
    assert result.fields == (5, 6, 7, 8, 9)
    assert result.layout_byte == 1


def test_known_metadata_is_fully_decoded_without_transforming_the_ink():
    result = parse_scene_info(modern_metadata())
    assert result.fields == (5, 6, 7, 8)
    assert result.paper_size == GRID
    assert result.viewport == (-700.0, -300.0, 2200.0, 2500.0)
    assert result.paper_size_raw == result.paper_size_lww == GRID
    assert result.describe()["viewport_applied_to_ink"] is False


def test_newer_rmscene_can_already_have_consumed_the_paper_size():
    data = modern_metadata()[13:]
    assert parse_scene_info(data, parsed_paper=GRID).fields == (6, 7, 8)


def test_optional_paper_size_can_be_omitted_when_other_dimensions_establish_grid():
    assert parse_scene_info(modern_metadata()[13:]).paper_size_raw == GRID


@pytest.mark.parametrize("data", [
    field(10, b""), b"\x51\x00", field(5, b"short"),
    field(5, struct.pack("<II", *GRID)) * 2,
    field(7, struct.pack("<dd", *GRID)) + field(5, struct.pack("<II", *GRID)),
    b"\xdc\x00\x00\x00\x00\x00",  # noncanonical tag 5
    b"\xff" * 11,
    field(6, b"\x1f\x00\x00" + field(2, struct.pack("<dddd", 0, 0, 1, 1)) + b"trailing"),
    field(6, b"\x1f\x00\x00" + field(2, b"short")),
    field(6, b"\x2f\x00\x00" + field(2, struct.pack("<dddd", 0, 0, 1, 1))),
    field(9, lww(b"\x01\x00")), b"\x00" * 1025,
])
def test_unknown_malformed_duplicate_or_unconsumed_metadata_fails(data):
    with pytest.raises(SceneInfoError):
        parse_scene_info(data)


@pytest.mark.parametrize("cut", [1, 4, 6, 12, 14, 17, 22, 57, 59, 78, 80, 108, 110, 123])
def test_truncated_metadata_fails(cut):
    with pytest.raises(SceneInfoError):
        parse_scene_info(modern_metadata(include_flag=True)[:cut])


@pytest.mark.parametrize("patch", [
    {"raw": (1620, 2160)}, {"current": (1404, 1800)}, {"raw": (float("nan"), 1872)},
    {"viewport": (0, 0, float("inf"), 1)}, {"viewport": (0, 0, -1, 1)},
    {"viewport": (0, 0, 1, 0)},
])
def test_inconsistent_uncalibrated_or_invalid_dimensions_fail(patch):
    with pytest.raises(SceneInfoError):
        parse_scene_info(modern_metadata(**patch))


@pytest.mark.parametrize("flag", [0, 2, 255])
def test_unknown_field9_semantics_are_not_assumed_from_valid_encoding(flag):
    with pytest.raises(SceneInfoError, match="field 9.*unqualified"):
        parse_scene_info(modern_metadata(include_flag=True, flag=flag))


def test_value_one_without_the_qualified_layout_profile_fails():
    with pytest.raises(SceneInfoError, match="field 9.*unqualified"):
        parse_scene_info(field(5, struct.pack("<II", *GRID)) + field(9, lww(b"\x01")))
