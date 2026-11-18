"""Shared source-value instructions for document extraction and source review.

Source evidence keeps the entire original cell. A fact's value/unit are a
lossless projection of that source, not a replacement transcription.
"""

from .llm.prompts import load_prompt

VERSION = "source-value-contract-v1"

VALUE_CONTRACT_INSTRUCTIONS = load_prompt("document-source-value-v1")
