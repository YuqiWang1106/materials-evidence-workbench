"""Start the website with empty local storage and no model credentials."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765, choices=range(1, 65536), metavar="PORT")
    args = parser.parse_args()
    instance = Path(tempfile.mkdtemp(prefix="materials-evidence-")).resolve()
    config = instance / "instance.toml"
    models = instance / "models.toml"
    models.write_text('[api]\napi_key = ""\n', encoding="utf-8")
    config.write_text(
        f'data_root = {json.dumps(str(instance / "data"))}\n'
        f'model_config = {json.dumps(str(models))}\n'
        f'host = "127.0.0.1"\nport = {args.port}\nworkers = 1\n',
        encoding="utf-8",
    )
    entry = Path(__file__).with_name("website.py")
    print(f"Local workspace: {instance}\nOpen http://127.0.0.1:{args.port}", flush=True)
    os.execv(sys.executable, [sys.executable, str(entry), "serve", "--config", str(config)])


if __name__ == "__main__":
    main()
