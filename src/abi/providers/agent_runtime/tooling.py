"""Adapt ABI-owned tool bindings to LangChain at the provider boundary."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any, cast

from langchain_core.tools import BaseTool, StructuredTool, ToolException

from abi.types.tools import ToolBinding

ActualStartHook = Callable[[ToolBinding, dict[str, object]], None]


def _permission_denial_output(error: ToolException) -> str:
    return f"ERROR: tool request rejected: {error}"


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
            try:
                result = binding.callable(*args, **kwargs)
                return await cast(Awaitable[Any], result)
            except (FileExistsError, PermissionError, ValueError) as exc:
                raise ToolException(str(exc)) from exc

        return StructuredTool.from_function(
            func=None,
            coroutine=coroutine,
            name=binding.name,
            description=binding.description,
            args_schema=binding.args_schema,
            handle_tool_error=_permission_denial_output,
        )
    def function(*args: Any, **kwargs: Any) -> Any:
        if on_actual_start is not None:
            on_actual_start(binding, dict(kwargs))
        try:
            return binding.callable(*args, **kwargs)
        except (FileExistsError, PermissionError, ValueError) as exc:
            raise ToolException(str(exc)) from exc

    return StructuredTool.from_function(
        func=function,
        coroutine=None,
        name=binding.name,
        description=binding.description,
        args_schema=binding.args_schema,
        handle_tool_error=_permission_denial_output,
    )
