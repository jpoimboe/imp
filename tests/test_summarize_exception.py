"""summarize_exception: surface subprocess stderr in a message + detail tail."""

from __future__ import annotations

import subprocess

from imp.engine.errors import summarize_exception


def test_text_stderr_appends_fatal_line_and_tail() -> None:
    err = subprocess.CalledProcessError(
        returncode=128,
        cmd=["git", "commit-tree"],
        stderr="warming up\nfatal: bad object refs/imp/artifacts/x\n",
    )
    msg, detail = summarize_exception(err)
    assert msg.endswith(" — fatal: bad object refs/imp/artifacts/x")
    assert msg.splitlines()[0].startswith("Command ")
    assert "fatal: bad object refs/imp/artifacts/x" in detail["stderr_tail"]


def test_bytes_stderr_is_decoded_with_replacement() -> None:
    err = subprocess.CalledProcessError(
        returncode=1,
        cmd=["git", "hash-object"],
        stderr=b"fatal: unable to read \xff object\n",
    )
    msg, detail = summarize_exception(err)
    assert msg.endswith(" — fatal: unable to read � object")
    assert "�" in detail["stderr_tail"]


def test_empty_stderr_falls_back_to_stdout() -> None:
    err = subprocess.CalledProcessError(
        returncode=1,
        cmd=["git", "x"],
        output="stdout detail line\n",
        stderr="   \n",
    )
    msg, detail = summarize_exception(err)
    assert msg.endswith(" — stdout detail line")
    assert detail["stderr_tail"] == "stdout detail line"


def test_no_output_at_all_yields_bare_first_line() -> None:
    err = subprocess.CalledProcessError(
        returncode=1, cmd=["git", "x"], output=None, stderr=None,
    )
    msg, detail = summarize_exception(err)
    assert msg == str(err).splitlines()[0]
    assert " — " not in msg
    assert detail == {}


def test_stderr_tail_keeps_last_lines_and_caps_size() -> None:
    body = "\n".join(f"line {i}" for i in range(100))
    err = subprocess.CalledProcessError(
        returncode=1, cmd=["git"], stderr=body + "\nfatal: end\n",
    )
    msg, detail = summarize_exception(err)
    assert msg.endswith(" — fatal: end")
    assert len(detail["stderr_tail"]) <= 2048
    assert "line 0" not in detail["stderr_tail"]
    assert "fatal: end" in detail["stderr_tail"]


def test_generic_exception_returns_first_line_and_empty_detail() -> None:
    msg, detail = summarize_exception(ValueError("boom\nsecond line"))
    assert msg == "boom"
    assert detail == {}
