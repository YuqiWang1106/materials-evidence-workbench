"""Success response fields owned by the website, shared by HTTP and OpenAPI."""

from .website_time import instant, mapping, nullable, object_fields, sequence

created = object_fields(created_at=instant)
updated = object_fields(updated_at=instant)
timestamps = object_fields(created_at=instant, updated_at=instant)
asset_evidence = object_fields(asset=created)
source_management = object_fields(operation=nullable(updated))
task_list = sequence(object_fields(source_operation=updated))
_stage_run = object_fields(started_at=nullable(instant), ended_at=nullable(instant))
document_observation = object_fields(
    observed_at=instant,
    progress=object_fields(updated_at=instant),
    events=sequence(object_fields(at=instant)),
    stages=sequence(_stage_run),
    stage_history=sequence(_stage_run),
    model_attempts=sequence(object_fields(started_at=nullable(instant))),
)

drafts = sequence(timestamps)
reused_draft = object_fields(draft=timestamps)
standard_preview = object_fields(child=timestamps)
recipes = sequence(created)
confirmation = object_fields(confirmed_at=instant)
standard_review = object_fields(updated_at=instant, confirmed_at=instant, meaning_confirmations=mapping(confirmation))
suggestion = object_fields(
    created_at=instant,
    updated_at=instant,
    started_at=nullable(instant),
    completed_at=nullable(instant),
    attempts=sequence(object_fields(started_at=instant, finished_at=instant)),
)
suggestions = object_fields(jobs=sequence(suggestion))
batches = object_fields(batches=sequence(created))
table_versions = object_fields(tables=sequence(created))
_published_artifact = object_fields(created_at=instant, publication=updated)
source_tables = object_fields(tables=sequence(_published_artifact))
raw_table = object_fields(publication=updated, analyses=sequence(_published_artifact))
analysis = object_fields(publication=updated, analysis=created)
chats = object_fields(chats=sequence(updated))
chat = object_fields(created_at=instant, updated_at=instant, turns=sequence(timestamps))
summaries = object_fields(jobs=sequence(timestamps))

candidate_reviews = object_fields(reviews=sequence(updated))
candidates = object_fields(groups=sequence(candidate_reviews))
source_reviews = object_fields(reviews=sequence(created))
_review = object_fields(at=instant)
_reviewed = object_fields(review=nullable(_review))
entities = object_fields(
    entities=sequence(
        object_fields(review=nullable(_review), attributes=sequence(_reviewed), related=sequence(_reviewed))
    )
)
entity_reviews = object_fields(reviews=mapping(_review))
runs = object_fields(runs=sequence(timestamps))

_budget_receipt = nullable(object_fields(created_at=instant, updated_at=instant, sent_at=nullable(instant)))
_legacy_job = object_fields(
    created_at=instant,
    updated_at=instant,
    completed_at=nullable(instant),
    attempts=sequence(object_fields(finished_at=instant)),
)
report_revision = object_fields(
    created_at=instant,
    updated_at=instant,
    completed_at=nullable(instant),
    legacy_job=_legacy_job,
    budget_receipt=_budget_receipt,
    report=nullable(object_fields(generated_at=instant, budget_receipt=_budget_receipt)),
)
report_history = object_fields(revisions=sequence(report_revision))

wiki_page = object_fields(
    created_at=instant, updated_at=instant, revision=created, review=nullable(created), history=sequence(created)
)
wiki_pages = object_fields(pages=sequence(wiki_page))
study_job = object_fields(
    created_at=instant, updated_at=instant, provider_receipt=nullable(object_fields(sent_at=nullable(instant)))
)
study_proposal = object_fields(proposal=nullable(study_job))
study_unit = object_fields(created_at=instant, updated_at=instant, snapshot=created, wiki_jobs=sequence(study_job))
study_units = object_fields(units=sequence(study_unit))
