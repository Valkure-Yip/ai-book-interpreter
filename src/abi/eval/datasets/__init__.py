"""Evaluation-dataset adapters.

Importing this package side-effects every adapter into the registry. Callers
use the :func:`load_eval_dataset` entry point with a spec string:

    from abi.eval.datasets import load_eval_dataset
    ds = load_eval_dataset("wmt24pp:en-zh_CN:literary:limit_docs=1")
"""

from abi.eval.datasets import (
    news_commentary,  # noqa: F401  # side-effect: register
    wmt24pp,  # noqa: F401  # side-effect: register adapter
)
from abi.eval.datasets._base import (
    DatasetParagraph,
    DatasetSpec,
    EvalDataset,
    compute_book_id,
    load_eval_dataset,
    materialize_to_book_file,
    parse_spec,
    register_adapter,
)

__all__ = [
    "DatasetParagraph",
    "DatasetSpec",
    "EvalDataset",
    "compute_book_id",
    "load_eval_dataset",
    "materialize_to_book_file",
    "parse_spec",
    "register_adapter",
]
