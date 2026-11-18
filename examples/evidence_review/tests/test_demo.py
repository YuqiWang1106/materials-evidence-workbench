"""Offline review scenarios using only the synthetic paper-strip fixture."""

from copy import deepcopy
import unittest

from demo import TARGET, paper_record
from materials_evidence.support import (
    bind_review_support,
    build_support_context,
    replay_review_support,
)


class PaperReviewTests(unittest.TestCase):
    def setUp(self):
        self.record, self.sources = paper_record()
        self.contexts = build_support_context(self.record, self.sources)
        label = next(
            option for option in self.contexts[TARGET]
            if option["evidence"][0]["block_id"] == "sample-label"
        )
        self.selected = {TARGET: [label["id"]]}

    def bind_label(self):
        bound, trace, errors = bind_review_support(
            self.record, self.contexts, self.selected, self.sources
        )
        self.assertEqual(errors, {})
        return bound, trace

    def test_selection_preserves_the_measurement_and_skips_the_other_option(self):
        before = deepcopy(self.record)
        bound, trace = self.bind_label()
        self.assertEqual(self.record, before)
        self.assertEqual(
            [ref["block_id"] for ref in bound["facts"][0]["evidence"]],
            ["measurement", "sample-label"],
        )
        self.assertEqual(trace[0]["selected_ids"], self.selected[TARGET])
        restored, errors = replay_review_support(self.record, trace, self.sources)
        self.assertEqual(errors, {})
        self.assertEqual(restored, bound)

    def test_sample_note_cannot_replace_missing_measurement_evidence(self):
        self.record["facts"][0]["evidence"] = []
        bound, _, errors = bind_review_support(
            self.record, self.contexts, self.selected, self.sources
        )
        self.assertIn(TARGET, errors)
        self.assertEqual(bound["facts"][0]["evidence"], [])

    def test_replay_requires_review_when_the_selected_label_changes(self):
        _, trace = self.bind_label()
        sources = deepcopy(self.sources)
        label = next(block for block in sources if block["block_id"] == "sample-label")
        label["text"] = "The blue paper strip was labeled Wren."
        restored, errors = replay_review_support(self.record, trace, sources)
        self.assertIsNone(restored)
        self.assertIn(TARGET, errors)


if __name__ == "__main__":
    unittest.main()
