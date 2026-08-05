"""LangGraph persistence for a provider-generic bounded callback loop."""

from __future__ import annotations

import stat
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Literal, cast

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

TickCallback = Callable[[str], Awaitable[bool]]
ExhaustedCallback = Callable[[str, int], Awaitable[None]]


class LoopState(TypedDict):
    """Checkpointed runtime cursor; it intentionally contains no business facts."""

    run_id: str
    cycle: int
    continue_run: bool


class DurableLoopRuntime:
    """Run a bounded business callback loop on one ABI-owned local checkpointer."""

    def __init__(
        self,
        *,
        checkpoint_path: Path,
        max_cycles: int,
        on_exhausted: ExhaustedCallback | None = None,
    ) -> None:
        if max_cycles < 1:
            raise ValueError(
                "max_cycles must be at least 1; configure a bounded positive control loop"
            )
        self._checkpoint_path = checkpoint_path
        self._max_cycles = max_cycles
        self._on_exhausted = on_exhausted

    async def run(self, *, run_id: str, tick: TickCallback) -> None:
        """Invoke the callback until it stops or the configured cycle limit is exhausted."""
        if not run_id:
            raise ValueError(
                "run_id must be stable and non-empty; use the durable ledger run identity"
            )
        checkpoint_path = _validated_checkpoint_path(self._checkpoint_path)
        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        async with AsyncSqliteSaver.from_conn_string(str(checkpoint_path)) as checkpointer:
            await checkpointer.setup()

            async def cycle_node(state: LoopState) -> LoopState:
                keep_running = await tick(state["run_id"])
                return {
                    "run_id": state["run_id"],
                    "cycle": state["cycle"] + 1,
                    "continue_run": keep_running,
                }

            def route_cycle(state: LoopState) -> Literal["cycle", "__end__"]:
                if state["continue_run"] and state["cycle"] < self._max_cycles:
                    return "cycle"
                return "__end__"

            builder = StateGraph(LoopState)
            builder.add_node("cycle", cycle_node)
            builder.add_edge(START, "cycle")
            builder.add_conditional_edges("cycle", route_cycle, ["cycle", END])
            graph = builder.compile(checkpointer=checkpointer)
            raw_result = await graph.ainvoke(
                {"run_id": run_id, "cycle": 0, "continue_run": True},
                {"configurable": {"thread_id": run_id}},
            )
            result = cast(LoopState, raw_result)
        if result["continue_run"] and result["cycle"] >= self._max_cycles:
            if self._on_exhausted is None:
                raise RuntimeError(
                    f"durable loop exhausted {self._max_cycles} cycles; provide an on_exhausted "
                    "callback that records a controller incident and blocks the run"
                )
            await self._on_exhausted(run_id, self._max_cycles)

def _validated_checkpoint_path(path: Path) -> Path:
    """Reject aliases and non-file path components before opening local SQLite state."""
    if ".." in path.parts:
        raise RuntimeError(
            "durable loop checkpoint path may not contain '..'; configure a stable local path"
        )
    absolute = path if path.is_absolute() else Path.cwd() / path
    current = Path(absolute.anchor)
    for index, part in enumerate(absolute.parts[1:]):
        current /= part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            break
        if stat.S_ISLNK(mode):
            raise RuntimeError(
                "durable loop checkpoint path may not contain symlinks; choose a trusted path"
            )
        is_final = index == len(absolute.parts[1:]) - 1
        if (is_final and not stat.S_ISREG(mode)) or (
            not is_final and not stat.S_ISDIR(mode)
        ):
            raise RuntimeError(
                "durable loop checkpoint path has an invalid component; choose a regular SQLite file"
            )
    return absolute
