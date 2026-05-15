"""Evaluation pipeline.

Compares the ABI multi-pass translation against a naive single-prompt
"stuff the source into the model" baseline across mechanical metrics
(glossary, length, anchors, completeness) and LLM-as-judge dimensions
(adequacy, fluency, coherence, style, pairwise preference).

Public entry points:
- :func:`abi.eval.pipeline.run_eval` — orchestrate one eval run
- :class:`abi.types.eval.EvalReport` — persisted output
"""

from abi.eval.pipeline import run_eval

__all__ = ["run_eval"]
