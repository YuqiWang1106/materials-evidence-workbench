"""Versioned common interpretation entry points; no arbitrary expression execution."""

from .adapters import adapt_profiles
from .compiler import COMPILER_VERSION, QUALITY_VERSION, compile_plan
from .engine import extract_plan, preview_plan

__all__ = ["compile_plan", "extract_plan", "preview_plan", "COMPILER_VERSION", "QUALITY_VERSION", "adapt_profiles"]
