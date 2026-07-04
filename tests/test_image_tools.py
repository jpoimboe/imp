from imp.agents.runner import ContentPart, ImagePart, TextPart


def test_text_part_defaults_type_and_holds_text():
    part = TextPart(text="login.png — 1200×800 PNG")
    assert part.type == "text"
    assert part.text == "login.png — 1200×800 PNG"


def test_image_part_carries_wire_and_persistence_fields():
    part = ImagePart(
        media_type="image/png",
        data="aGVsbG8=",
        width=1200,
        height=800,
        blob="deadbeef.png",
        nbytes=340_000,
    )
    assert part.type == "image"
    assert part.media_type == "image/png"
    assert part.data == "aGVsbG8="
    assert (part.width, part.height) == (1200, 800)
    assert part.blob == "deadbeef.png"
    assert part.nbytes == 340_000


def test_image_part_is_frozen():
    import dataclasses
    import pytest

    part = ImagePart(
        media_type="image/jpeg",
        data="Zm9v",
        width=10,
        height=10,
        blob="cafe.jpg",
        nbytes=3,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        part.data = "changed"  # type: ignore[misc]


def test_text_part_is_frozen():
    import dataclasses
    import pytest

    part = TextPart(text="hi")
    with pytest.raises(dataclasses.FrozenInstanceError):
        part.text = "changed"  # type: ignore[misc]


def test_content_part_union_admits_both():
    parts: list[ContentPart] = [TextPart(text="hi"), ImagePart(
        media_type="image/png", data="Zm9v", width=1, height=1,
        blob="a.png", nbytes=3,
    )]
    assert [p.type for p in parts] == ["text", "image"]
