"""Hashed atomic summary jobs with independent namespace and worker ownership."""

from contextlib import contextmanager
from copy import deepcopy

from .core import StateConflict, fingerprint
from .research_chat_store import ResearchChatStore, checked_id


class ResearchSummaryFiles(ResearchChatStore):
    def __init__(self, store, workspace, collection):
        super().__init__(store)
        self.root = store.root / "research_summaries" / fingerprint([workspace, collection])

    def path(self, jid):
        return self.safe(self.root / checked_id("summary", jid))

    def save(self, job):
        self.write(self.path(job["job_id"]) / "job.json", job)

    def job(self, cid, jid):
        checked_id("chat", cid)
        job = self.read(self.path(jid) / "job.json")
        snapshot = self.read(self.path(jid) / "input.json")
        if job["chat_id"] != cid or job["job_id"] != jid or job["input_hash"] != fingerprint(snapshot):
            raise StateConflict("SUMMARY_IDENTITY_CONFLICT")
        if jid != "summary-" + fingerprint([cid, job["request"]["request_id"]]):
            raise StateConflict("SUMMARY_IDENTITY_CONFLICT")
        return deepcopy(job), snapshot

    @contextmanager
    def job_lock(self, jid):
        checked_id("summary", jid)
        with self.store.lock("research-summary:" + jid):
            yield

    def lease(self, jid):
        return self.acquire_worker("turn-" + checked_id("summary", jid).split("-", 1)[1])

    def ids(self):
        self.safe(self.root)
        return (
            sorted(
                p.name
                for p in self.root.iterdir()
                if p.is_dir() and not p.is_symlink() and p.name.startswith("summary-") and (p / "job.json").is_file()
            )
            if self.root.exists()
            else []
        )
