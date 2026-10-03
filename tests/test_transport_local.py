"""LocalTransport + Library integration over a temp xochitl store (no device)."""

import json

from rmk.library import Library
from rmk.transport import LocalTransport


NB = "00000000-0000-0000-0000-000000000010"
P1 = "00000000-0000-0000-0000-000000000001"
P2 = "00000000-0000-0000-0000-000000000002"


def _store(tmp_path):
    # A folder, a notebook under it, and a two-page ordering (p1 then p2).
    (tmp_path / "fold.metadata").write_text(
        json.dumps({"visibleName": "Ideas", "type": "CollectionType", "parent": ""})
    )
    (tmp_path / f"{NB}.metadata").write_text(
        json.dumps({"visibleName": "Roadmap", "type": "DocumentType", "parent": "fold"})
    )
    (tmp_path / f"{NB}.content").write_text(
        json.dumps({"cPages": {"pages": [{"id": P1}, {"id": P2}]}})
    )
    d = tmp_path / NB
    d.mkdir()
    (d / f"{P1}.rm").write_bytes(b"PAGE-ONE")
    (d / f"{P2}.rm").write_bytes(b"PAGE-TWO")


def test_local_transport_lists_and_reads(tmp_path):
    _store(tmp_path)
    with LocalTransport() as t:
        lib = Library(t, str(tmp_path))
        docs = lib.documents()
        assert [d.name for d in docs] == ["Roadmap"]
        assert lib.path_of(docs[0].uuid) == "Ideas/Roadmap"


def test_local_transport_reads_pages_in_order(tmp_path):
    _store(tmp_path)
    with LocalTransport() as t:
        lib = Library(t, str(tmp_path))
        doc = lib.resolve("Roadmap")
        assert lib.read_pages(doc.uuid) == [b"PAGE-ONE", b"PAGE-TWO"]


def test_local_read_never_appends_orphan_and_missing_page_fails(tmp_path):
    import pytest
    _store(tmp_path)
    orphan = "00000000-0000-0000-0000-000000000003"
    (tmp_path / NB / f"{orphan}.rm").write_bytes(b"ORPHAN")
    lib = Library(LocalTransport(), str(tmp_path))
    assert lib.read_pages(NB) == [b"PAGE-ONE", b"PAGE-TWO"]
    (tmp_path / NB / f"{P2}.rm").unlink()
    with pytest.raises(FileNotFoundError):
        lib.read_pages(NB)
