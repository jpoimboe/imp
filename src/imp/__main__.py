import signal
import sys


def _exit_130_on_sigint(_signum: int, _frame: object) -> None:
    raise SystemExit(130)


# Module-level (before the heavy imp.cli.main import) so a SIGINT during the
# startup window exits 130 instead of being reaped killed-by-signal (-2). Runs
# for both `python -m imp` (module executed as __main__) and the installed `imp`
# console script (entry point imp.__main__:main), covering the pre-main() import
# window that main() itself cannot.
signal.signal(signal.SIGINT, _exit_130_on_sigint)


def main() -> int:
    try:
        from imp.cli.main import main as _cli_main

        return _cli_main()
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
