"""Post-EPUB QA: stratified random spot-check sampler + excellence validator."""

from __future__ import annotations

from abi.qa.sampler import select_random_review_passages
from abi.qa.units import AuditUnit, extract_units, stratify
from abi.qa.validator import validate_random_spotcheck

__all__ = [
    "AuditUnit",
    "extract_units",
    "select_random_review_passages",
    "stratify",
    "validate_random_spotcheck",
]
