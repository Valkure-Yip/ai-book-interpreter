"""Per-run budget gate. Checked before every LLM call."""

from __future__ import annotations


class BudgetExceeded(RuntimeError):
    """Raised when the next LLM call would push total cost past the hard cap."""

    def __init__(self, spent: float, cap: float, attempted: float):
        super().__init__(
            f"budget hard cap reached: spent=${spent:.4f}, cap=${cap:.4f}, "
            f"attempted=${attempted:.4f}. "
            "Increase --max-cost-usd or run `abi resume` after raising the cap."
        )
        self.spent = spent
        self.cap = cap
        self.attempted = attempted


class BudgetGate:
    """Pre-flight check against a hard USD cap.

    Caller passes the *estimated* cost of an upcoming call. If admitting it
    would exceed the cap, raises ``BudgetExceeded`` before the network round trip.
    """

    def __init__(self, hard_cap_usd: float | None) -> None:
        self._cap = hard_cap_usd
        self._spent = 0.0

    def admit(self, estimated_cost: float) -> None:
        if self._cap is None:
            return
        if self._spent + estimated_cost > self._cap:
            raise BudgetExceeded(self._spent, self._cap, estimated_cost)

    def record(self, actual_cost: float) -> None:
        self._spent += actual_cost

    @property
    def spent(self) -> float:
        return self._spent
