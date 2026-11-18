"""Poll for recoverable ingestion work, renew active attempt leases, and record the loop's health."""

from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import uuid4

from .core import StateConflict, clock
from .storage import task_repository as tasks

if TYPE_CHECKING:
    from datetime import datetime

    from kb_ingest.ingest.service import IngestionService

logger = logging.getLogger(__name__)
_IDLE_SECONDS = 2.0
_HEARTBEAT_SECONDS = 15.0
_LEASE_SECONDS = 900.0
_RETRY_SECONDS = 2.0
_MAX_RETRY_SECONDS = 60.0
_TASK = "task:"
_WORKING = ("reading", "extracting", "validating")


@dataclass
class _Retry:
    failures: int
    not_before: float


@dataclass
class _LiveAttempt:
    token: str
    renewed: float


class SchedulerHealth:
    """What the recovery loop last did. Failed steps and tasks wait longer before each retry.

    Only the step name, exception type, error ID and time are kept. Messages stay in the log.
    """

    def __init__(self, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._retries: dict[str, _Retry] = {}
        self._last_error: dict[str, object] | None = None
        self._last_pass_at: datetime | None = None
        self._tasks: Counter[str] = Counter()
        self._live_attempts: dict[str, _LiveAttempt] = {}

    def attempt_renewed(self, task_id: str, token: str) -> None:
        """Called when the attempt starts and after each lease renewal that reached the database."""
        with self._lock:
            self._live_attempts[task_id] = _LiveAttempt(token, self._monotonic())

    def attempt_finished(self, task_id: str) -> None:
        with self._lock:
            self._live_attempts.pop(task_id, None)

    def own_lease_expired(self, task_id: str, token: str | None) -> bool | None:
        """None unless this process runs the attempt. The data root has a single owning process.

        The owner counts its lease on the monotonic clock, which stands still while the machine
        sleeps. The stored wall-clock lease remains the rule for recovery after a restart.
        """
        with self._lock:
            live = self._live_attempts.get(task_id)
            if not token or live is None or live.token != token:
                return None
            return self._monotonic() - live.renewed >= _LEASE_SECONDS

    def due(self, key: str) -> bool:
        with self._lock:
            retry = self._retries.get(key)
            return retry is None or self._monotonic() >= retry.not_before

    def succeeded(self, key: str) -> None:
        with self._lock:
            self._retries.pop(key, None)

    def _record(self, step: str, error: BaseException) -> str:
        error_id = "err-" + uuid4().hex[:12]
        self._last_error = {"step": step, "error": type(error).__name__, "error_id": error_id, "at": clock.now()}
        return error_id

    def failed(self, key: str, error: BaseException) -> tuple[str, float]:
        """Return the error ID for the log and the delay before the next attempt."""
        with self._lock:
            failures = self._retries[key].failures + 1 if key in self._retries else 1
            delay = min(_RETRY_SECONDS * 2 ** (failures - 1), _MAX_RETRY_SECONDS)
            self._retries[key] = _Retry(failures, self._monotonic() + delay)
            return self._record("task" if key.startswith(_TASK) else key, error), delay

    def reported(self, step: str, error: BaseException) -> str:
        """Record a failure outside the loop's own steps, such as a worker or a lease renewal."""
        with self._lock:
            return self._record(step, error)

    def tasks_listed(self, tasks: list[dict]) -> None:
        present = {_TASK + task["task_id"] for task in tasks}
        with self._lock:
            self._tasks = Counter(task["state"] for task in tasks)
            for key in [key for key in self._retries if key.startswith(_TASK) and key not in present]:
                del self._retries[key]

    def pass_finished(self) -> None:
        with self._lock:
            self._last_pass_at = clock.now()

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            failing = sorted(self._retries)
            return {
                "last_pass_at": self._last_pass_at,
                "last_error": dict(self._last_error) if self._last_error else None,
                "failing_steps": [key for key in failing if not key.startswith(_TASK)],
                "failing_tasks": sum(key.startswith(_TASK) for key in failing),
                "queued_tasks": self._tasks["queued"],
                "working_tasks": sum(self._tasks[state] for state in _WORKING),
            }


def health_report(service: IngestionService) -> dict[str, object]:
    """Ready needs a live loop with nothing failing; a stopped or dead loop is not ready."""
    thread = service._scheduler
    alive = thread is not None and thread.is_alive()
    state = service.scheduler_health.snapshot()
    with service._guard:
        running = len(service._running)
    failing = bool(state["failing_steps"] or state["failing_tasks"])
    status = "not_ready" if not alive else "degraded" if failing else "ready"
    return {"status": status, "scheduler": {"alive": alive, **state}, "running_tasks": running}


def _interrupt_expired(service: IngestionService, task: dict) -> None:
    # An expired lease does not prove that the provider rejected the previous request.
    change = tasks.Transition(
        task["attempt_token"],
        "failed",
        "上次处理已中断，请核对费用与结果后明确重试",
        "interrupted_attempt: explicit_retry_required; reservations_preserved",
    )
    with suppress(StateConflict), service.store._write() as session:
        tasks.interrupt_expired(session, service.store.workspace_id, task["task_id"], change)


def _recover_task(service: IngestionService, task: dict) -> None:
    task_id = task["task_id"]
    if task["source_type"] == "prepared" and not service.store.pending_path(task_id, "prepared_input.json").is_file():
        return
    if service._retired_table(task):
        return
    expired = task["state"] in _WORKING and (task["lease_until"] or 0) < time.time()
    if expired and service.scheduler_health.own_lease_expired(task_id, task["attempt_token"]) is False:
        # The wall clock passed the lease while this process was suspended. The attempt still runs here.
        with suppress(StateConflict):
            service.store.heartbeat(task_id, task["attempt_token"])
            service.scheduler_health.attempt_renewed(task_id, task["attempt_token"])
    elif expired and task["source_type"] in ("paper", "patent"):
        _interrupt_expired(service, task)
    elif task["state"] == "queued" or expired:
        service.start(task_id)


def _recover_tasks(service: IngestionService) -> None:
    health = service.scheduler_health
    tasks = service.store.list_tasks()
    health.tasks_listed(tasks)
    for task in tasks:
        key = _TASK + task["task_id"]
        if service._stop.is_set():
            break
        if not health.due(key):
            continue
        try:
            _recover_task(service, task)
        except Exception as error:
            error_id, delay = health.failed(key, error)
            logger.exception(
                "task recovery failed task_id=%s error_id=%s retry_in=%ss", task["task_id"], error_id, delay
            )
        else:
            health.succeeded(key)


def _steps(service: IngestionService) -> list[tuple[str, Callable[[], object]]]:
    steps: list[tuple[str, Callable[[], object]]] = [
        ("source_operations", service.recover_source_operations),
        ("standard_model_batches", service.recover_standard_model_batches),
        ("standard_model_jobs", service.recover_standard_model_jobs),
        ("document_report_jobs", service.recover_document_report_jobs),
        ("table_chat_turns", service.recover_table_chat_turns),
    ]
    study_jobs = getattr(service, "study_wiki_jobs", None)
    if study_jobs is not None:
        steps.append(("study_wiki_jobs", study_jobs.recover))
    steps.append(("tasks", lambda: _recover_tasks(service)))
    return steps


def run_pass(service: IngestionService) -> None:
    """Run every due step. One step's failure is logged and never reaches the next step or the loop."""
    health = service.scheduler_health
    for name, step in _steps(service):
        if service._stop.is_set():
            return
        if not health.due(name):
            continue
        try:
            step()
        except Exception as error:
            error_id, delay = health.failed(name, error)
            logger.exception("scheduler step failed step=%s error_id=%s retry_in=%ss", name, error_id, delay)
        else:
            health.succeeded(name)
    health.pass_finished()


def poll(service: IngestionService) -> None:
    while not service._stop.is_set():
        run_pass(service)
        service._stop.wait(_IDLE_SECONDS)


def start_worker(service: IngestionService, task_id: str) -> None:
    with service._guard:
        if service._stop.is_set() or task_id in service._running:
            return
        service._running.add(task_id)

    def run() -> None:
        try:
            service.process(task_id)
        except (StateConflict, KeyError):
            pass  # A withdrawn source may have been fenced before this queued worker starts.
        except Exception as error:
            # Nobody reads this future. The lease expires and the scheduler reports the interruption.
            error_id = service.scheduler_health.reported("worker", error)
            logger.exception("ingestion worker failed task_id=%s error_id=%s", task_id, error_id)
        finally:
            with service._guard:
                service._running.discard(task_id)

    service.pool.submit(run)


@contextmanager
def heartbeat(service: IngestionService, task: dict) -> Iterator[None]:
    """Renew the lease while the attempt runs, and let the scheduler see that it runs in this process."""
    task_id, token = task["task_id"], task["attempt_token"]
    done = threading.Event()

    def beat() -> None:
        while not done.wait(_HEARTBEAT_SECONDS):
            try:
                service.store.heartbeat(task_id, token)
                service.scheduler_health.attempt_renewed(task_id, token)
            except StateConflict:
                break
            except Exception as error:
                # One failed write must not end renewal: the lease would lapse under a running attempt.
                error_id = service.scheduler_health.reported("heartbeat", error)
                logger.exception("lease renewal failed task_id=%s error_id=%s", task_id, error_id)

    service.scheduler_health.attempt_renewed(task_id, token)
    thread = threading.Thread(target=beat, daemon=True)
    thread.start()
    try:
        yield
    finally:
        done.set()
        thread.join(timeout=1)
        service.scheduler_health.attempt_finished(task_id)
