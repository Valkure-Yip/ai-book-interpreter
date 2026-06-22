"""Evaluation pipeline (see ``docs/design-docs/eval-standard.md``).

Three planes + a crosscutting line:

- **L1 流程可信度** (``trace``): replay deterministic gates over a book project.
- **L2 中间产物 + 逐章译文** (``mechanical`` + ``align``): post-hoc paragraph scoring.
- **L3 最终产物** (``judge``): EPUB compliance + LLM-as-judge comparative scoring.

Datasets (``datasets``) + ``calibration`` derive ``length_ratio`` bands and serve
as the ABI-vs-baseline comparison input. ``pipeline`` wires it together; ``report``
aggregates and renders.
"""

from __future__ import annotations

__all__ = [
    "FORMULA_VERSION",
]

# Bumping this invalidates frozen golden scores (eval-standard.md §6.4).
FORMULA_VERSION = "para-v0.2"
