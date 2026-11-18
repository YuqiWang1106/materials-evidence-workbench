"""Explicit native website composition. Import and doctor never create a Store."""

from __future__ import annotations

import argparse as argparse
import fcntl as fcntl
import importlib.util
import json as json
import os as os
import platform as platform
import signal as signal
import socket as socket
import sys as sys
from contextlib import ExitStack as _ExitStack
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from . import native_diagnostics as _diagnostics
from .core.config import NativeConfig
from .core.config import load_native_config as load_native_config
from .core.errors import ConfigurationError, NativeError
from .core.native_config import research_budget_root as _research_budget_root
from .core.native_config import research_identity as research_identity
from .core.paths import HANDWRITING_RUNTIME as LOCAL_OCR_RUNTIME
from .core.paths import MINERU_RUNTIME, WEBSITE_UI
from .core.workspace import Workspace
from .core.workspace import matches_database_workspaces as matches_database_workspaces
from .core.workspace import saved_workspace_id as saved_workspace_id
from .ingest.documents.readers.local_ocr_identity import PRIMARY_WEIGHTS
from .native_diagnostics import agent_knowledge_token as _agent_knowledge_token
from .native_diagnostics import research_config as _research_config
from .native_runtime import DataRootOwner as DataRootOwner
from .native_runtime import reader_environment as _reader_environment
from .storage import revision_check as _revisions
from .storage import sqlite as sqlite
from .storage.snapshot import readonly_snapshot as readonly_snapshot
from .website_cli import main as main


def local_reader_options(config) -> dict:
    """Task options for the local OCR readers (scanned pages, photos, handwriting). The first reader runs in the
    MinerU reader environment with MinerU's own PP-OCRv6 files, so it needs nothing beyond model_root and
    mineru_command; the second reader (PaddleOCR-VL) has its own environment and weights."""
    if config.model_root is None or not config.mineru_command:
        return {}
    python = Path(config.mineru_command[0]).parent / "python"
    options = {
        "handwriting_command": [str(python), "-I", str(LOCAL_OCR_RUNTIME / "handwriting_reader.py")],
        "handwriting_model_root": str(config.model_root / "models"),
    }
    if config.second_reader_python and config.second_reader_model:
        options.update(
            handwriting_second_command=[
                str(config.second_reader_python),
                "-I",
                str(LOCAL_OCR_RUNTIME / "second_reader.py"),
                "--backend",
                "auto",
                "--dtype",
                "auto",
            ],
            handwriting_second_model=str(config.second_reader_model),
        )
    return options


def _local_ocr_status(config) -> dict:
    """Read-only: the reader files are where the options point; nothing is started or read twice."""
    options = local_reader_options(config)
    if not options:
        return {"status": "not_configured", "second_reader": "not_configured"}
    model_root = Path(options["handwriting_model_root"])
    first = Path(options["handwriting_command"][0]).is_file() and all(
        (model_root / name).is_file() for name in PRIMARY_WEIGHTS
    )
    if config.second_reader_python is None and config.second_reader_model is None:
        second = "not_configured"
    elif (
        config.second_reader_python
        and config.second_reader_model
        and config.second_reader_python.is_file()
        and (config.second_reader_model / "model.safetensors").is_file()
    ):
        second = "configured_unverified"
    else:
        second = "not_ready"
    return {"status": "configured_unverified" if first else "not_ready", "second_reader": second}


def _model_tool():
    path = MINERU_RUNTIME / "models.py"
    spec = importlib.util.spec_from_file_location("kb_native_model_tool", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _model_settings(config):
    return _diagnostics.model_settings(config)


def _port_ready(config):
    return _diagnostics.port_ready(config)


def _health(config: NativeConfig, *, wait: bool) -> dict:
    return _diagnostics.doctor(config, _port_ready, _model_tool, _local_ocr_status, wait=wait)


def doctor(config: NativeConfig) -> dict:
    """Read-only local validation; never downloads or calls a paid provider."""
    return _health(config, wait=False)


@contextmanager
def _owned_workspace(config):
    from .core import atomic_json
    from .core.restore_gate import require_restore_complete
    from .native_identity import new_workspace_id

    with _revisions.external_database(_research_budget_root(config), config.data_root):
        pass
    with _ExitStack() as lifetime:
        with _revisions.locked_database(config.data_root, write=True) as database:
            lifetime.enter_context(DataRootOwner(config.data_root))
            require_restore_complete(config.data_root)
            config = _diagnostics.verified_workspace(config, database)
            if not config.workspace_id:
                if database is not None:
                    message = "Existing instance requires its original workspace_id in native.toml."
                    raise NativeError(message)
                config = replace(config, workspace_id=new_workspace_id())
            marker = config.data_root / ".native-workspace.json"
            if not marker.exists():
                atomic_json(marker, {"workspace_id": config.workspace_id})
        yield config


@contextmanager
def native_application(config: NativeConfig):
    """Hold ownership through construction, recovery, workers and shutdown."""
    from kb_ingest.ingest.service import IngestionService

    from .api import create_app
    from .core.config import ModelCredentials, ModelSettings
    from .knowledge_wiki import WikiService
    from .native_identity import authentication

    with _owned_workspace(config) as config, _reader_environment(config):
        try:
            tokens = authentication(config.data_root, initialize=True)
        except (OSError, ValueError, TypeError):
            raise NativeError(
                "Private authentication state is invalid; check permissions without deleting existing state."
            ) from None
        health = _health(config, wait=True)
        if health["website"]["status"] != "ready":
            raise NativeError("Native website preflight is not ready; run doctor.")
        try:
            settings = _model_settings(config)
        except ConfigurationError:
            settings = ModelSettings(ModelCredentials())
        options = {
            "document_pipeline_v2": True,
            "document_extraction_mode": "entity-graph-v1",
            "document_input_strategy": "ir_batches",
            "document_reader_tier": "standard",
            "mineru_command": list(config.mineru_command),
            "native_document_readiness": health["documents"],
            "model_config": settings.documents.as_dict(),
            "embedding_config": settings.embedding.as_dict(),
            **local_reader_options(config),
        }
        if config.entity_budget_root is not None:
            # Historical execution evidence only; no amount/path-existence gate.
            options["legacy_entity_cost_path"] = str(config.entity_budget_root / "cost.json")
            options["legacy_batch_id"] = config.entity_batch_id
        table_options = settings.table.as_dict()
        service = None
        binding = None
        try:
            service = IngestionService(
                Workspace(config.data_root, config.workspace_id),
                document_options=options,
                table_options=table_options,
                table_workflow_mode="latest-only",
                concurrency=config.workers,
            )
            wiki = WikiService(service, workspace_id=config.workspace_id)
            from .research_chat_startup import bind_research_model

            binding = bind_research_model(
                _research_config(config, tokens), engine=service.store.engine, model_settings=settings
            )
            kwargs = binding.app_kwargs()
            kwargs["model_readiness"] = health["research"]
            ui = WEBSITE_UI
            readonly_token = _agent_knowledge_token(config) if health["agent_knowledge"]["status"] == "ready" else None
            app = create_app(
                service,
                wiki_service=wiki,
                ui_dir=ui,
                agent_knowledge_token=readonly_token,
                agent_knowledge_max_response_bytes=config.agent_knowledge_max_response_bytes,
                **kwargs,
            )
            app.state.native_readiness = health
            app.state.native_workspace_id = config.workspace_id
            yield app
        finally:
            if service is not None:
                service.close()
                service.store.engine.dispose()
            if binding is not None:
                binding.close()


if __name__ == "__main__":
    raise SystemExit(main())
