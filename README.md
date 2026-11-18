# Materials Evidence Workbench

A materials data reviewer needs to know which sample a measurement describes, where its value came from, and whether a previous review still holds after a source changes. This workbench connects document and table ingestion to source-linked evidence, review decisions, and searchable knowledge records.

I designed and developed the materials knowledge-base workflow. Other members of the project contributed to the implementation. This portfolio edition brings together the application's Python backend, browser review interface and a small, reproducible example of evidence selection. — Dave Wang

## Try an evidence review

From the repository root, run this example with Python 3.12. It uses the standard library and synthetic data.

```sh
cd examples/evidence_review
python3 demo.py
python3 -m unittest discover -s tests -v
```

A fictional paper strip has a recorded thickness of **0.12 mm**. A reviewer chooses its sample label as supporting evidence and leaves out an unrelated storage note. Run the example to see the citations, replay the decision against the same inputs, and change the source label. The changed label requires a new review. The included support-selection implementation comes from the application.

## Read the implementation

| Engineering question | Where to look |
| --- | --- |
| How does a measured value stay attached to its original source? | [Evidence selection and replay](backend/knowledge_ingestion/kb_ingest/document_evidence_context.py) and [PDF evidence locations](backend/knowledge_ingestion/kb_ingest/document_evidence_location.py) |
| What happens when a reviewer edits a number or unit? | [Quantity checks](backend/knowledge_ingestion/kb_ingest/extraction/entity_evidence_values.py) and [edge cases](backend/knowledge_ingestion/tests/test_entity_evidence_values.py) |
| How do reviewed candidates move towards publication? | [Document publication workflow](backend/knowledge_ingestion/kb_ingest/document_workflow_publication.py) and [review endpoints](backend/knowledge_ingestion/kb_ingest/http/api/publication.py) |
| How does a search combine lexical and semantic candidates? | [Reciprocal rank fusion](backend/knowledge_ingestion/kb_ingest/wiki_hybrid_search.py) |
| How does someone review a region in a spreadsheet? | [Region review interface](frontend/knowledge_ingestion/region-review.js) and [review state logic](frontend/knowledge_ingestion/region-review-logic.mjs) |

## Run the local application

Use Python 3.12 on macOS or Linux. From the repository root:

```sh
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -c backend/knowledge_ingestion/constraints-tested.txt -e backend/knowledge_ingestion
python scripts/start_demo.py
```

Open **http://127.0.0.1:8765**. The launcher prints the location of a new local workspace and leaves it available after shutdown. To reuse it, run `python scripts/website.py serve --config /absolute/path/to/instance.toml` with the configuration path from that directory. Pass `--port` to the demo launcher if needed. The browser interface is in Chinese. The example above illustrates the review flow in English.

The local site opens with an empty library. Upload [the synthetic paper-strip CSV](examples/paper-strips.csv) from the spreadsheet review screen to inspect sample dimensions as a two-row table. The review example and selected tests run offline after installation. Full PDF/OCR and model-assisted extraction require additional readers, model files and provider configuration. The public edition ships with a small synthetic catalogue for width, thickness and unit-aware comparisons. Private literature, annotations, vocabulary, model weights, credentials and previous repository history are excluded.

## Run the checks

```sh
python -m pip install -c backend/knowledge_ingestion/constraints-tested.txt pytest
python -m pytest -q backend/knowledge_ingestion/tests
node --test frontend/knowledge_ingestion/tests/*.test.mjs
```

On macOS with Python 3.12, the included suite passed 225 backend checks, 30 browser-logic checks and 3 standalone example checks. The tests exercise synthetic measurements, version-sensitive evidence replay, PDF locations, the knowledge index, and browser review state. Tests generate their own PDFs and records. Live model extraction and OCR are outside this test suite. The deterministic checks concern source binding, consistency and retrieval order. A material's physical performance and scientific validity require separate assessment.

Third-party dependencies retain their original terms. The optional reader has a [separate license notice](backend/knowledge_ingestion/runtime/mineru/LICENSE.MinerU-4.0.9.md).
