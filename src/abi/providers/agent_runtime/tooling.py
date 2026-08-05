"""Adapt ABI-owned tool bindings to LangChain at the provider boundary."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any, cast

from langchain_core.tools import BaseTool, StructuredTool

from abi.types.tools import ToolBinding

ActualStartHook = Callable[[ToolBinding, dict[str, object]], None]


def _is_async_callable(function: Callable[..., object]) -> bool:
    unwrapped = inspect.unwrap(function)
    unwrapped_call = inspect.unwrap(type(function).__call__)
    return inspect.iscoroutinefunction(unwrapped) or inspect.iscoroutinefunction(unwrapped_call)


def to_langchain_tool(
    binding: ToolBinding,
    *,
    on_actual_start: ActualStartHook | None = None,
) -> BaseTool:
    """Return the sole SDK representation of an ABI tool binding."""
    if _is_async_callable(binding.callable):

        async def coroutine(*args: Any, **kwargs: Any) -> Any:
            if on_actual_start is not None:
                on_actual_start(binding, dict(kwargs))
            result = binding.callable(*args, **kwargs)
            return await cast(Awaitable[Any], result)

        return StructuredTool.from_function(
            func=None,
            coroutine=coroutine,
            name=binding.name,
            description=binding.description,
            args_schema=binding.args_schema,
        )
    def function(*args: Any, **kwargs: Any) -> Any:
        if on_actual_start is not None:
            on_actual_start(binding, dict(kwargs))
        return binding.callable(*args, **kwargs)

    return StructuredTool.from_function(
        func=function,
        coroutine=None,
        name=binding.name,
        description=binding.description,
        args_schema=binding.args_schema,
    )
