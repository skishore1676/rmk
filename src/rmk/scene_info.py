"""Bounded decoding of firmware 3.27 SceneInfo metadata left by rmscene 0.6.

Wire shapes: upstream rmscene PR53, head 0f1edc5e19ae45f2d6d0074e875a5270314424ce.
Tag 6 is viewport state, not an ink/PDF coordinate transform. Tags 5, 7 and 8
describe paper dimensions and must agree with the already calibrated grid.
Tag 9 has no established general semantics. Only the observed value 1 with
the complete standard-grid metadata profile has been qualified against an
independent rmapi annotated export. Other values fail closed. Unknown
tags, malformed envelopes and unconsumed bytes are never silently discarded.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import struct

MAX_SCENE_INFO_BYTES = 1024
GRID = (1404, 1872)


class SceneInfoError(ValueError):
    pass


class _Reader:
    def __init__(self, data: bytes):
        self.data, self.pos = data, 0

    def take(self, size: int) -> bytes:
        if size < 0 or size > len(self.data) - self.pos:
            raise SceneInfoError("SceneInfo metadata is truncated.")
        result = self.data[self.pos:self.pos + size]
        self.pos += size
        return result

    def varuint(self) -> int:
        value = 0
        for index in range(10):
            byte = self.take(1)[0]
            if index == 9 and byte > 1:
                raise SceneInfoError("SceneInfo metadata has an invalid integer.")
            value |= (byte & 127) << (7 * index)
            if byte < 128:
                if index and byte == 0:
                    raise SceneInfoError("SceneInfo metadata has a noncanonical integer.")
                return value
        raise SceneInfoError("SceneInfo metadata has an invalid integer.")

    def payload(self) -> bytes:
        return self.take(struct.unpack("<I", self.take(4))[0])

    def done(self):
        if self.pos != len(self.data):
            raise SceneInfoError("SceneInfo metadata has unconsumed bytes.")


def _lww_value(data: bytes) -> bytes:
    reader = _Reader(data)
    if reader.varuint() != 0x1F:
        raise SceneInfoError("SceneInfo metadata has an invalid LWW timestamp.")
    reader.take(1)  # CRDT actor byte; timestamp does not transform drawing data.
    reader.varuint()
    if reader.varuint() != 0x2C:
        raise SceneInfoError("SceneInfo metadata has an invalid LWW value.")
    value = reader.payload()
    reader.done()
    return value


def _numbers(data: bytes, count: int) -> tuple[float, ...]:
    if len(data) != count * 8:
        raise SceneInfoError("SceneInfo metadata has an invalid numeric length.")
    values = struct.unpack("<" + "d" * count, data)
    if not all(math.isfinite(value) for value in values):
        raise SceneInfoError("SceneInfo metadata has nonfinite dimensions.")
    return values


@dataclass(frozen=True)
class SceneInfoMetadata:
    fields: tuple[int, ...]
    paper_size: tuple[int, int] | None
    viewport: tuple[float, ...] | None
    paper_size_raw: tuple[float, ...] | None
    paper_size_lww: tuple[float, ...] | None
    layout_byte: int | None

    def describe(self):
        return {"fields": list(self.fields), "paper_size": self.paper_size,
                "viewport": self.viewport, "viewport_applied_to_ink": False,
                "paper_size_raw": self.paper_size_raw, "paper_size_lww": self.paper_size_lww,
                "layout_byte": self.layout_byte}


def parse_scene_info(extra: bytes, parsed_paper=None) -> SceneInfoMetadata:
    if len(extra) > MAX_SCENE_INFO_BYTES:
        raise SceneInfoError("SceneInfo metadata exceeds its supported size.")
    reader = _Reader(extra)
    values = {}
    while reader.pos < len(extra):
        tag = reader.varuint()
        field, kind = tag >> 4, tag & 15
        if kind != 12 or field not in (5, 6, 7, 8, 9):
            raise SceneInfoError("SceneInfo metadata contains an unknown field or wire type.")
        if field in values or (values and field <= max(values)):
            raise SceneInfoError("SceneInfo metadata repeats or reorders a field.")
        payload = reader.payload()
        if field == 5:
            if parsed_paper is not None or len(payload) != 8:
                raise SceneInfoError("SceneInfo metadata has an ambiguous paper size.")
            values[field] = struct.unpack("<II", payload)
        elif field == 6:
            values[field] = _numbers(_lww_value(payload), 4)
            if values[field][2] <= 0 or values[field][3] <= 0:
                raise SceneInfoError("SceneInfo metadata has invalid viewport dimensions.")
        elif field == 7:
            values[field] = _numbers(payload, 2)
        elif field == 8:
            values[field] = _numbers(_lww_value(payload), 2)
        else:
            flag = _lww_value(payload)
            if len(flag) != 1:
                raise SceneInfoError("SceneInfo field 9 has an invalid byte envelope.")
            values[field] = flag[0]
    reader.done()
    sizes = [tuple(value) for value in (parsed_paper, values.get(5), values.get(7), values.get(8)) if value is not None]
    if any(size != GRID for size in sizes):
        raise SceneInfoError("SceneInfo paper dimensions disagree or require device calibration.")
    if values and not sizes:
        raise SceneInfoError("SceneInfo extensions do not establish a calibrated paper grid.")
    if 9 in values and (values[9] != 1 or any(field not in values for field in (6, 7, 8))):
        # Do not infer bool semantics: upstream observed both 1 and 2. Only
        # the complete value-1 standard-grid profile has alignment evidence.
        raise SceneInfoError("SceneInfo field 9 has an unqualified value or layout profile; alignment review is required.")
    return SceneInfoMetadata(tuple(values), tuple(parsed_paper) if parsed_paper is not None else values.get(5),
                             values.get(6), values.get(7), values.get(8), values.get(9))
