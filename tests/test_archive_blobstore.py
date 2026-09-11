from __future__ import annotations

from pathlib import Path

from imp.engine import durable
from imp.engine.archive.blobstore import LocalBlobStore, hash_content


def test_hash_content_is_deterministic_and_content_addressed() -> None:
    a = hash_content(b'{"type":"quest_created"}\n')
    b = hash_content(b'{"type":"quest_created"}\n')
    c = hash_content(b'{"type":"quest_merged"}\n')
    assert a == b
    assert a != c
    assert len(a) == 64  # sha256 hexdigest


def test_put_returns_hash_and_get_roundtrips(tmp_path: Path) -> None:
    store = LocalBlobStore(tmp_path / "blobs")
    content = b'{"type":"x"}\n{"type":"y"}\n'
    digest = store.put(content)
    assert digest == hash_content(content)
    assert store.has(digest)
    assert store.get(digest) == content


def test_put_durably_publishes_new_blob_in_order(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "archive" / "blobs"
    store = LocalBlobStore(root)
    content = b"new durable blob\n"
    digest = hash_content(content)
    dest = root / digest[:2] / digest[2:]
    calls: list[tuple[str, Path]] = []
    real_fsync_file = durable._fsync_file
    real_replace = durable.os.replace
    real_fsync_directory = durable.fsync_directory

    def fsync_file(stream, path: Path) -> None:
        calls.append(("file", path.parent))
        real_fsync_file(stream, path)

    def replace(src, target) -> None:
        calls.append(("replace", Path(target)))
        real_replace(src, target)

    def fsync_directory(path: Path) -> None:
        calls.append(("directory", path))
        real_fsync_directory(path)

    monkeypatch.setattr(durable, "_fsync_file", fsync_file)
    monkeypatch.setattr(durable.os, "replace", replace)
    monkeypatch.setattr(durable, "fsync_directory", fsync_directory)

    actual = store.put(content)

    assert (actual, calls, store.get(digest)) == (
        digest,
        [
            ("file", dest.parent),
            ("replace", dest),
            ("directory", dest.parent),
            ("directory", root),
            ("directory", root.parent),
        ],
        content,
    )


def test_put_durably_confirms_existing_blob_in_order(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "archive" / "blobs"
    store = LocalBlobStore(root)
    content = b"existing durable blob\n"
    digest = store.put(content)
    dest = root / digest[:2] / digest[2:]
    calls: list[tuple[str, Path]] = []
    real_fsync_file = durable.fsync_file
    real_fsync_directory = durable.fsync_directory

    def fsync_file(path: Path) -> None:
        calls.append(("file", path))
        real_fsync_file(path)

    def fsync_directory(path: Path) -> None:
        calls.append(("directory", path))
        real_fsync_directory(path)

    monkeypatch.setattr(durable, "fsync_file", fsync_file)
    monkeypatch.setattr(durable, "fsync_directory", fsync_directory)

    actual = store.put(content)

    assert (actual, calls, store.get(digest)) == (
        digest,
        [
            ("file", dest),
            ("directory", dest.parent),
            ("directory", root),
            ("directory", root.parent),
        ],
        content,
    )


def test_put_is_deduping_single_blob_for_same_content(tmp_path: Path) -> None:
    root = tmp_path / "blobs"
    store = LocalBlobStore(root)
    content = b"same bytes\n"
    first = store.put(content)
    second = store.put(content)
    assert first == second
    files = [p for p in root.rglob("*") if p.is_file()]
    assert len(files) == 1
    assert (root / first[:2] / first[2:]).exists()


def test_get_and_has_absent_blob(tmp_path: Path) -> None:
    store = LocalBlobStore(tmp_path / "blobs")
    missing = hash_content(b"never stored")
    assert store.has(missing) is False
    assert store.get(missing) is None
