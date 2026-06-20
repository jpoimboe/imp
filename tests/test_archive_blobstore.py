from __future__ import annotations

from pathlib import Path

from imp.engine.archive.blobstore import LocalBlobStore, hash_content


def test_hash_content_is_deterministic_and_content_addressed() -> None:
    a = hash_content(b'{"type":"change_created"}\n')
    b = hash_content(b'{"type":"change_created"}\n')
    c = hash_content(b'{"type":"change_merged"}\n')
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
