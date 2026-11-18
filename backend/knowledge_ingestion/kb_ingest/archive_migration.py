"""Compatibility exports for the offline migration API."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import tempfile
import tomllib
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path, PurePosixPath

from sqlalchemy import column, literal_column, select, table

from .core.workspace import workspace_files
from .offline import (
    LocalArchiveExportReader,
    MigrationError,
    export_bundle,
    import_bundle,
    inventory,
    verify_bundle,
    verify_target,
)
from .offline.files import BRANCH, FORMAT
from .storage import sqlite
from .storage.engine import file_connection

__all__ = [
    "BRANCH",
    "FORMAT",
    "LocalArchiveExportReader",
    "MigrationError",
    "Path",
    "PurePosixPath",
    "annotations",
    "column",
    "contextmanager",
    "deepcopy",
    "export_bundle",
    "fcntl",
    "file_connection",
    "hashlib",
    "import_bundle",
    "inventory",
    "json",
    "literal_column",
    "os",
    "re",
    "select",
    "shutil",
    "sqlite",
    "table",
    "tempfile",
    "tomllib",
    "verify_bundle",
    "verify_target",
    "workspace_files",
]
