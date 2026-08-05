"""Adapt ABI-owned tool bindings to LangChain at the provider boundary."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any, cast

from langchain_core.tools import BaseTool, StructuredTool

from abi.types.tools import ToolBinding


def to_langchain_tool(binding: ToolBinding) -> BaseTool:
    """Return the sole SDK representation of an ABI tool binding."""
    if inspect.iscoroutinefunction(binding.callable):
        coroutine = cast(Callable[..., Awaitable[Any]], binding.callable)
        return StructuredTool.from_function(
            func=None,
            coroutine=coroutine,
            name=binding.name,
            description=binding.description,
            args_schema=binding.args_schema,
        )
    function = cast(Callable[..., Any], binding.callable)
    return StructuredTool.from_function(
        func=function,
        coroutine=None,
        name=binding.name,
        description=binding.description,
        args_schema=binding.args_schema,
    )
