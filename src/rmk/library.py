"""The document library: turn the tablet's flat UUID store into named notebooks.

Each notebook on a reMarkable is a UUID with sidecar files in the xochitl root:

    <uuid>.metadata   JSON: visibleName, parent, type, deleted, lastModified
    <uuid>.content    JSON: page order and format version
    <uuid>/           directory of <page-uuid>.rm stroke files

Folders are ``CollectionType`` metadata entries; ``parent`` links form the tree.
The tree-building and name-resolution logic is pure (takes plain dicts) so it can
be unit-tested without a tablet — see tests/test_library.py.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .transport.base import Transport


@dataclass
class Doc:
    uuid: str
    name: str
    kind: str  # "document" | "collection"
    parent: str  # parent UUID, "" for root, or "trash"
    deleted: bool
    last_modified: int

    @property
    def is_folder(self) -> bool:
        return self.kind == "collection"


def parse_index(metadatas: dict[str, dict]) -> dict[str, Doc]:
    """Build a UUID -> Doc index from ``{uuid: metadata_dict}``.

    Pure function — no I/O. ``metadatas`` keys are the bare UUIDs (filename minus
    the ``.metadata`` suffix).
    """
    docs: dict[str, Doc] = {}
    for uuid, m in metadatas.items():
        kind = "collection" if m.get("type") == "CollectionType" else "document"
        docs[uuid] = Doc(
            uuid=uuid,
            name=m.get("visibleName", uuid),
            kind=kind,
            parent=m.get("parent", ""),
            deleted=bool(m.get("deleted", False)),
            last_modified=int(m.get("lastModified", 0) or 0),
        )
    return docs


def full_path(docs: dict[str, Doc], uuid: str) -> str:
    """The human-readable path of a doc, e.g. ``Ideas/Roadmap``."""
    parts: list[str] = []
    seen: set[str] = set()
    cur = uuid
    while cur in docs and cur not in seen:
        seen.add(cur)
        parts.append(docs[cur].name)
        cur = docs[cur].parent
    return "/".join(reversed(parts))


def find(docs: dict[str, Doc], query: str, include_deleted: bool = False) -> list[Doc]:
    """Resolve a user query to matching *documents* (not folders).

    Matches, in priority order: exact UUID; exact full path; exact name;
    case-insensitive substring of the name or path.
    """
    if query in docs and not docs[query].is_folder:
        return [docs[query]]

    candidates = [
        d
        for d in docs.values()
        if not d.is_folder and (include_deleted or not d.deleted)
    ]
    paths = {d.uuid: full_path(docs, d.uuid) for d in candidates}

    exact = [d for d in candidates if d.name == query or paths[d.uuid] == query]
    if exact:
        return exact

    ql = query.lower()
    return [
        d
        for d in candidates
        if ql in d.name.lower() or ql in paths[d.uuid].lower()
    ]


def page_order(content: dict) -> list[str]:
    """Extract ordered page UUIDs from a ``.content`` document.

    Handles both the modern schema (``cPages.pages[].id``) and the legacy one
    (``pages`` as a flat list of UUIDs).
    """
    cpages = content.get("cPages")
    if isinstance(cpages, dict) and isinstance(cpages.get("pages"), list):
        ids: list[str] = []
        for p in cpages["pages"]:
            pid = p.get("id") if isinstance(p, dict) else None
            # A page with a "deleted" marker was removed on-device.
            if pid and not (isinstance(p, dict) and p.get("deleted")):
                ids.append(pid)
        if ids:
            return ids
    pages = content.get("pages")
    if isinstance(pages, list):
        return [p for p in pages if isinstance(p, str)]
    return []


class Library:
    """Reads notebooks from a tablet via a :class:`Transport`."""

    def __init__(self, transport: Transport, root: str) -> None:
        self.transport = transport
        self.root = root.rstrip("/")
        self._docs: dict[str, Doc] | None = None

    def load(self) -> dict[str, Doc]:
        if self._docs is not None:
            return self._docs
        raw = self.transport.read_many(self.root, (".metadata",))
        metadatas: dict[str, dict] = {}
        for name, data in raw.items():
            uuid = name[: -len(".metadata")]
            try:
                metadatas[uuid] = json.loads(data)
            except json.JSONDecodeError:
                continue
        self._docs = parse_index(metadatas)
        return self._docs

    def documents(self, include_deleted: bool = False) -> list[Doc]:
        docs = self.load()
        return sorted(
            (
                d
                for d in docs.values()
                if not d.is_folder and (include_deleted or not d.deleted)
            ),
            key=lambda d: full_path(docs, d.uuid).lower(),
        )

    def path_of(self, uuid: str) -> str:
        return full_path(self.load(), uuid)

    def resolve(self, query: str) -> Doc:
        """Resolve a query to exactly one document, or raise with guidance."""
        matches = find(self.load(), query)
        if not matches:
            raise LookupError(f"No notebook matches {query!r}. Try `rmk ls`.")
        if len(matches) > 1:
            listing = "\n".join(f"  - {self.path_of(m.uuid)}" for m in matches[:10])
            raise LookupError(
                f"{query!r} is ambiguous ({len(matches)} matches):\n{listing}\n"
                "Use a more specific name or the full path."
            )
        return matches[0]

    def read_pages(self, uuid: str) -> list[bytes]:
        """Return the ordered list of raw ``.rm`` page files for a notebook.

        Falls back to a sorted directory listing when ``.content`` has no usable
        page order.
        """
        doc_dir = f"{self.root}/{uuid}"
        rm_files = self.transport.read_many(doc_dir, (".rm",))
        if not rm_files:
            return []

        order: list[str] = []
        try:
            content = json.loads(self.transport.read_bytes(f"{self.root}/{uuid}.content"))
            order = page_order(content)
        except Exception:  # noqa: BLE001 — degrade to filename sort
            order = []

        pages: list[bytes] = []
        used: set[str] = set()
        for pid in order:
            fname = f"{pid}.rm"
            if fname in rm_files:
                pages.append(rm_files[fname])
                used.add(fname)
        # Append any pages not referenced by the ordering, in filename order.
        for fname in sorted(rm_files):
            if fname not in used:
                pages.append(rm_files[fname])
        return pages
