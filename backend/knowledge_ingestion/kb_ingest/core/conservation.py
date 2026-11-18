"""A processing step accounts for every record it was given.

Each record either stays in the step's result or is set aside with a recorded reason. A step whose counts
do not add up has a defect: the run fails instead of continuing with records missing.
"""


class ConservationError(RuntimeError):
    """A step lost or invented records. This is a defect in the step, never a property of the document."""

    code = "records_unaccounted"

    def __init__(self, step: str, entered: int, kept: int, set_aside: int) -> None:
        super().__init__(f"{self.code}: step={step} entered={entered} kept={kept} set_aside={set_aside}")
        self.step = step
        self.entered = entered
        self.kept = kept
        self.set_aside = set_aside


def require_conserved(step: str, *, entered: int, kept: int, set_aside: int) -> None:
    """Raise when the records that entered a step are not exactly those kept plus those set aside."""
    if entered != kept + set_aside:
        raise ConservationError(step, entered, kept, set_aside)
