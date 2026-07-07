"""Unit tests for the pure library logic (no tablet required)."""

from rmk.library import find, full_path, page_order, parse_index


def _index():
    metas = {
        "root-ideas": {"visibleName": "Ideas", "type": "CollectionType", "parent": ""},
        "nb-roadmap": {
            "visibleName": "Roadmap",
            "type": "DocumentType",
            "parent": "root-ideas",
        },
        "nb-groceries": {
            "visibleName": "Groceries",
            "type": "DocumentType",
            "parent": "",
        },
        "nb-old": {
            "visibleName": "Old thing",
            "type": "DocumentType",
            "parent": "",
            "deleted": True,
        },
    }
    return parse_index(metas)


def test_parse_index_kinds():
    docs = _index()
    assert docs["root-ideas"].is_folder
    assert not docs["nb-roadmap"].is_folder
    assert docs["nb-old"].deleted


def test_full_path_nested():
    docs = _index()
    assert full_path(docs, "nb-roadmap") == "Ideas/Roadmap"
    assert full_path(docs, "nb-groceries") == "Groceries"


def test_find_by_name_and_path():
    docs = _index()
    assert [d.uuid for d in find(docs, "Roadmap")] == ["nb-roadmap"]
    assert [d.uuid for d in find(docs, "Ideas/Roadmap")] == ["nb-roadmap"]


def test_find_by_uuid():
    docs = _index()
    assert [d.uuid for d in find(docs, "nb-groceries")] == ["nb-groceries"]


def test_find_substring_case_insensitive():
    docs = _index()
    assert [d.uuid for d in find(docs, "road")] == ["nb-roadmap"]


def test_find_never_returns_the_folder_itself():
    docs = _index()
    # Querying a folder name resolves to the document(s) under it (via path
    # substring), never the CollectionType entry itself.
    matches = find(docs, "Ideas")
    assert [d.uuid for d in matches] == ["nb-roadmap"]
    assert all(not d.is_folder for d in matches)


def test_find_excludes_deleted_by_default():
    docs = _index()
    assert find(docs, "Old thing") == []  # deleted by default
    assert [d.uuid for d in find(docs, "Old thing", include_deleted=True)] == ["nb-old"]


def test_page_order_modern_schema():
    content = {"cPages": {"pages": [{"id": "p1"}, {"id": "p2", "deleted": True}, {"id": "p3"}]}}
    assert page_order(content) == ["p1", "p3"]


def test_page_order_legacy_schema():
    assert page_order({"pages": ["a", "b", "c"]}) == ["a", "b", "c"]


def test_page_order_empty():
    assert page_order({}) == []
