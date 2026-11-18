"""Validated construction options for a bounded model client."""

from __future__ import annotations

import math
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict
from urllib.parse import urlsplit

import httpx

from .budget import ModelBudget
from .errors import ModelCallError

if TYPE_CHECKING:
    from .model_requests import ModelRequestContext


class ClientOptions(TypedDict, total=False):
    base_url: str
    budget: ModelBudget | None
    ledger_path: Path | None
    max_output_tokens: int | None
    max_attempts: int
    timeout: float
    reasoning_effort: str
    retry_delay: float
    cache_dir: Path | None
    transport: httpx.AsyncBaseTransport | None
    concurrency: int
    budget_state_path: Path | str | None
    shared_limiter: AbstractContextManager[object] | None
    cancel_check: Callable[[], object] | None
    count_input_tokens: bool
    cost_ledger: object
    batch_stage: str | None
    run_id: str | None
    incomplete_artifact_dir: Path | str | None
    run_ceiling_micro_usd: int
    project_budget_context: ModelRequestContext | None
    protocol: str
    cache_namespace: str | None
    retry_transient: bool
    require_full_timeout: bool
    workspace_id: str | None
    workspace_root: Path | None


@dataclass(kw_only=True)
class Settings:
    base_url: str = "https://api.openai.com/v1"
    budget: ModelBudget | None = None
    ledger_path: Path | None = None
    max_output_tokens: int | None = 12000
    max_attempts: int = 2
    timeout: float = 240
    reasoning_effort: str = "medium"
    retry_delay: float = 1
    cache_dir: Path | None = None
    transport: httpx.AsyncBaseTransport | None = None
    concurrency: int = 2
    budget_state_path: Path | str | None = None
    shared_limiter: AbstractContextManager[object] | None = None
    cancel_check: Callable[[], object] | None = None
    count_input_tokens: bool = False
    cost_ledger: object = None
    batch_stage: str | None = None
    run_id: str | None = None
    incomplete_artifact_dir: Path | str | None = None
    run_ceiling_micro_usd: int = 1_000_000
    project_budget_context: ModelRequestContext | None = None
    protocol: str = "responses"
    cache_namespace: str | None = None
    retry_transient: bool = False
    require_full_timeout: bool = False
    workspace_id: str | None = None
    workspace_root: Path | None = None

    def validate(self, api_key: str, model: str) -> None:
        parsed = urlsplit(self.base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            message = "invalid model endpoint"
            raise ValueError(message)
        if not api_key or not model:
            message = "model credential and model name are required"
            raise ValueError(message)
        if (
            self.max_attempts < 1
            or self.max_attempts > 3
            or not 1 <= self.concurrency <= 2
            or type(self.timeout) not in (int, float)
            or not math.isfinite(self.timeout)
            or self.timeout <= 0
        ):
            message = "invalid model execution limits"
            raise ValueError(message)
        if self.protocol not in {"responses", "chat_completions"}:
            message = "unsupported_protocol"
            raise ValueError(message)
        if self.budget is not None and self.budget_state_path is not None:
            message = "pass budget_state_path on either the budget or client, not both"
            raise ValueError(message)

    def scoped_budget(self) -> ModelBudget:
        owner = self.budget.workspace_id if self.budget is not None else "local"
        if self.workspace_id is not None:
            owner = self.workspace_id
        if self.project_budget_context is not None and self.project_budget_context.service.workspace_id != owner:
            raise ModelCallError("model_workspace_identity_mismatch")
        if self.budget is None:
            return ModelBudget(
                budget_state_path=self.budget_state_path,
                workspace_id=owner,
                workspace_root=self.workspace_root,
            )
        if (
            self.workspace_id is not None
            and self.workspace_id != self.budget.workspace_id
            or (self.workspace_root is not None and self.workspace_root.absolute() != self.budget.workspace_root)
        ):
            message = "model workspace identity mismatch"
            raise ModelCallError(message)
        return self.budget
