"""Foreground serve and read-only doctor commands for the native website."""

import argparse
import json
import signal
import sys
from collections.abc import Sequence
from pathlib import Path

from .core.native_config import parse_native_config as _parse_config
from .storage import revision_check as _revisions

_STARTUP_FAILURE = "Native startup/configuration failed; private configuration values are not shown."


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Native materials knowledge website (Python 3.12)")
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("serve", "doctor"):
        command = sub.add_parser(action)
        command.add_argument("--config", type=Path, required=True, help="Absolute native instance TOML file")
        if action == "doctor":
            command.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv=None):
    # type: (Sequence[str] | None) -> int
    args = _parse_args(argv)
    # Resolve the public factory and diagnostic hooks after the CLI has been imported.
    from . import native

    try:
        config = _parse_config(args.config)
        if args.action == "doctor":
            result = native.doctor(config)
            # print also preserves the readiness exit code when stdout is closed.
            print(json.dumps(result, ensure_ascii=False))  # noqa: T201
            return 0 if result["status"] == "ready" else 1
        import uvicorn

        # Uvicorn replays captured signals after its lifespan has stopped. Convert
        # SIGTERM to a normal Python exit so the outer ownership/binding cleanup
        # also runs after its worker drain, before the process exits.
        prior_term = signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        try:
            with native.native_application(config) as app:
                uvicorn.run(app, host=config.host, port=config.port, reload=False, workers=1)
        finally:
            signal.signal(signal.SIGTERM, prior_term)
    except _revisions.IncompatibleRevisionError:
        sys.stderr.write(_revisions.REFUSAL_MESSAGE + "\n")
        return 1
    except Exception:  # noqa: BLE001
        print(_STARTUP_FAILURE, file=sys.stderr)  # noqa: T201
        return 1
    return 0
