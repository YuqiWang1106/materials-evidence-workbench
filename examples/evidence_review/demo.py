"""Review a citation for an invented paper sample. Run with python demo.py."""

from materials_evidence.support import (
    bind_review_support,
    build_support_context,
    replay_review_support,
)


TARGET = "f:sheet-thickness"


def paper_record():
    """A value in a measurement log and two notes describing its sample."""
    sources = [
        {"block_id": "measurement", "text": "Measured thickness: 0.12 mm."},
        {"block_id": "sample-label", "text": "The blue paper strip was labeled Kite."},
        {"block_id": "storage-note", "text": "The strip was kept in a cardboard sleeve."},
    ]
    measurement, label, storage = [
        {"block_id": block["block_id"], "quote": block["text"]} for block in sources
    ]
    record = {
        "objects": [
            {"key": "strip", "kind": "sample", "evidence": [label, storage]},
        ],
        "facts": [
            {
                "key": "sheet-thickness",
                "object_key": "strip",
                "field_id": "sample.thickness",
                "raw_value": "0.12",
                "evidence": [measurement],
            },
        ],
        "relations": [],
    }
    return record, sources


def main():
    record, sources = paper_record()
    contexts = build_support_context(record, sources)
    print("Paper thickness review (synthetic data)")
    print(f"Measurement: {sources[0]['text']}")
    print("Available supporting citations:")
    for option in contexts[TARGET]:
        ref = option["evidence"][0]
        print(f"  {ref['block_id']}: {ref['quote']}")

    # In a review UI, the reader would choose the label showing which strip was tested.
    label = next(
        option for option in contexts[TARGET]
        if option["evidence"][0]["block_id"] == "sample-label"
    )
    bound, trace, errors = bind_review_support(
        record, contexts, {TARGET: [label["id"]]}, sources
    )
    if errors:
        raise ValueError(errors)
    print("After the reviewer selects the sample label, the fact cites:")
    for ref in bound["facts"][0]["evidence"]:
        print(f"  [{ref['block_id']}] {ref['quote']}")

    restored, errors = replay_review_support(record, trace, sources)
    if errors or restored != bound:
        raise ValueError(f"Unchanged review did not reproduce: {errors}")
    print("Replay with the same sources: same citations.")

    revised = [
        {**block, "text": "The blue paper strip was labeled Wren."}
        if block["block_id"] == "sample-label" else block
        for block in sources
    ]
    result, errors = replay_review_support(record, trace, revised)
    if result is not None or TARGET not in errors:
        raise ValueError("Expected the edited source to require a new review.")
    print("Replay after the label changes from Kite to Wren: new review required.")


if __name__ == "__main__":
    main()
