"""Nonsecret identities shared by document checkpoints and model request receipts."""

from hashlib import sha256


def model_cache_identity(client: object) -> dict[str, object]:
    """Use the resolved client; offline test doubles may have no configured endpoint."""
    return {
        "model": getattr(client, "model", type(client).__qualname__),
        "base_url": getattr(client, "base_url", None),
        "protocol": getattr(client, "protocol", None),
        "reasoning_effort": getattr(client, "reasoning_effort", None),
        "max_output_tokens": getattr(client, "max_output_tokens", None),
    }


def stage_context(stage_version: str, prompt_version: str, instructions: str) -> dict[str, str]:
    """Carry semantic changes through both checkpoint and request-ledger lookup."""
    return {
        "stage_version": stage_version,
        "prompt_version": prompt_version,
        "prompt_sha256": sha256(instructions.encode("utf-8")).hexdigest(),
    }
