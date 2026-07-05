from __future__ import annotations

import base64

from imp.agents.runner import ImagePart, Message, TextPart, ToolResultBlock
from imp.agents.transcript import Transcript


def _img_msg() -> Message:
    part = ImagePart(
        media_type="image/png",
        data=base64.b64encode(b"pixels").decode("ascii"),
        width=4, height=4, blob="feedface.png", nbytes=6,
    )
    return Message(
        role="user",
        content=(ToolResultBlock(
            tool_use_id="t1",
            content=[TextPart(text="a.png — 4×4 PNG"), part]),),
    )


def test_load_rebuilds_image_data_from_blob(tmp_path) -> None:
    blobs = tmp_path / "blobs"
    blobs.mkdir()
    (blobs / "feedface.png").write_bytes(b"pixels")
    t = Transcript(tmp_path / "transcript.jsonl")
    t.append(_img_msg())
    # The transcript line on disk carries no base64.
    assert '"data"' not in (tmp_path / "transcript.jsonl").read_text()

    [msg] = t.load(blobs_dir=blobs)
    img = msg.content[0].content[1]
    assert isinstance(img, ImagePart)
    assert base64.b64decode(img.data) == b"pixels"


def test_load_without_blobs_dir_degrades_to_text_note(tmp_path) -> None:
    t = Transcript(tmp_path / "transcript.jsonl")
    t.append(_img_msg())
    [msg] = t.load()  # no blobs_dir supplied
    parts = msg.content[0].content
    assert isinstance(parts[1], TextPart)
    assert parts[1].text == "[image feedface no longer available]"
