import signal


def _exit_130_on_sigint(_signum: int, _frame: object) -> None:
    raise SystemExit(130)


# Module-level (before the heavy imp.cli.main import) so a SIGINT during the
# startup window exits 130 instead of being reaped killed-by-signal (-2).
signal.signal(signal.SIGINT, _exit_130_on_sigint)

if __name__ == "__main__":
    try:
        from imp.cli.main import main

        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130) from None
