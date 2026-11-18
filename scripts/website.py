"""Thin native website entry; Python 3.12, no runtime installation or downloads."""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    # Preserve venv executable paths: resolving their symlink loses environment identity.
    candidates = [ROOT / ".venv/bin/python", ROOT / "backend/knowledge_ingestion/.venv/bin/python"]
    selected = None
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            probe = subprocess.run(
                [str(candidate), "-I", "-c", "import sys; raise SystemExit(sys.version_info[:2] != (3,12))"],
                capture_output=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if probe.returncode == 0:
            selected = str(candidate)
            break
    if selected is not None and os.path.abspath(sys.executable) != os.path.abspath(selected):
        os.execv(selected, [selected, str(Path(__file__).resolve()), *sys.argv[1:]])
    if sys.version_info[:2] != (3, 12):
        print("网站需要 Python 3.12；请先创建项目 .venv，再运行此入口。", file=sys.stderr)
        return 1
    sys.path.insert(0, str(ROOT / "backend/knowledge_ingestion"))
    from kb_ingest.website_cli import main as native_main

    return native_main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
