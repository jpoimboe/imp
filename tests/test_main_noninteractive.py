import anyio

from imp.cli.main import make_stdin_reply_fn


def test_stdin_reply_reads_lines_then_defaults_on_eof(monkeypatch) -> None:
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO("first line\n"))
    reply = make_stdin_reply_fn("Proceed using your best judgment.")
    assert anyio.run(reply) == "first line"
    assert anyio.run(reply) == "Proceed using your best judgment."   # EOF → default
    assert anyio.run(reply) == "Proceed using your best judgment."   # stays default
