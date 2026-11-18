"""Resource locations anchored to the installed distribution or repository checkout."""

from pathlib import Path


def package_root() -> Path:
    return Path(__file__).resolve().parents[1]


PACKAGE_ROOT = package_root()
DISTRIBUTION_ROOT = PACKAGE_ROOT.parent
HANDWRITING_RUNTIME = DISTRIBUTION_ROOT / "runtime" / "handwriting"
MINERU_RUNTIME = DISTRIBUTION_ROOT / "runtime" / "mineru"
WEBSITE_UI = DISTRIBUTION_ROOT.parents[1] / "frontend" / "knowledge_ingestion"
