"""LocalTransport + Library integration over a temp xochitl store (no device)."""

import json

from rmk.library import Library
from rmk.transport import LocalTransport


def _store(tmp_path):
    # A folder, a notebook under it, and a two-page ordering (p1 then p2).
    (tmp_path / "fold.metadata").write_text(
        json.dumps({"visibleName": "Ideas", "type": "CollectionType", "parent": ""})
    )
    (tmp_path / "nb.metadata").write_text(
        json.dumps({"visibleName": "Roadmap", "type": "DocumentType", "parent": "fold"})
    )
    (tmp_path / "nb.content").write_text(
        json.dumps({"cPages": {"pages": [{"id": "p1"}, {"id": "p2"}]}})
    )
    d = tmp_path / "nb"
    d.mkdir()
    (d / "p1.rm").write_bytes(b"PAGE-ONE")
    (d / "p2.rm").write_bytes(b"PAGE-TWO")


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
