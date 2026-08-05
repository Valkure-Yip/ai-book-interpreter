#!/usr/bin/env python3
"""Enforce SDK import boundaries with executable AST analysis."""

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass
from pathlib import Path

_PROVIDER_SDKS = ("langchain", "langgraph", "langfuse")


@dataclass(frozen=True, slots=True)
class Violation:
    rule: str
    path: str
    line: int
    module: str


def _is_provider_module(relative_path: Path) -> bool:
    return bool(relative_path.parts) and relative_path.parts[0] == "providers"


def _is_forbidden_sdk(module: str) -> bool:
    top_level = module.split(".", 1)[0]
    return top_level.startswith(_PROVIDER_SDKS)


def scan_tree(root: Path) -> tuple[Violation, ...]:
    """Return stable violations for SDK imports outside ``providers``."""
    violations: list[Violation] = []
    for source_path in sorted(root.rglob("*.py")):
        relative_path = source_path.relative_to(root)
        if _is_provider_module(relative_path):
            continue
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        for node in ast.walk(tree):
            modules: tuple[str, ...]
            if isinstance(node, ast.Import):
                modules = tuple(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                modules = (node.module,)
            else:
                continue
            for module in modules:
                if _is_forbidden_sdk(module):
                    violations.append(
                        Violation(
                            rule="sdk-import-outside-providers",
                            path=relative_path.as_posix(),
                            line=node.lineno,
                            module=module,
                        )
                    )
    return tuple(sorted(violations, key=lambda item: (item.path, item.line, item.module)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="Path to the src/abi package root")
    args = parser.parse_args(argv)
    violations = scan_tree(args.root)
    for item in violations:
        print(
            f"{item.path}:{item.line}: {item.rule}: import {item.module}. "
            "Move the SDK import and adaptation into src/abi/providers/.",
            file=sys.stderr,
        )
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
