"""The spend guard (SPEC.md 7.3, 7.7).

Every adapter call passes through here twice: once before, with a preflight estimate that can
refuse the call, and once after, with the cost actually incurred. Two caps apply — a hard cap
for the current run and a daily cap across everything Tokop has spent today — and both stop at
100% rather than warning past it.

The guard never raises a cap. Budget values are read from the environment once, at startup, and
nothing in the engine may write them (SPEC.md section 1, "Money").
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

WARNING_FRACTION = Decimal("0.8")


class BudgetExceeded(RuntimeError):
    """A call was refused because it would have taken spend past a cap."""

    def __init__(self, cap_name: str, cap: Decimal, spent: Decimal, projected: Decimal) -> None:
        self.cap_name = cap_name
        self.cap = cap
        self.spent = spent
        self.projected = projected
        super().__init__(
            f"{cap_name} cap of ${cap} would be exceeded: ${spent} already spent and this call "
            f"is projected to cost ${projected}. Raise {cap_name.upper().replace(' ', '_')}_USD "
            f"in .env if you mean to spend more; Tokop will not raise it for you."
        )


@dataclass(frozen=True)
class BudgetStatus:
    """What a cap looks like right now, for the Spend screen and for preflight display."""

    name: str
    cap: Decimal | None
    spent: Decimal
    warning_at: Decimal | None

    @property
    def fraction(self) -> float | None:
        if self.cap is None or self.cap == 0:
            return None
        return float(self.spent / self.cap)

    @property
    def state(self) -> str:
        fraction = self.fraction
        if fraction is None:
            return "uncapped"
        if fraction >= 1:
            return "stopped"
        if fraction >= float(WARNING_FRACTION):
            return "warning"
        return "ok"

    @property
    def remaining(self) -> Decimal | None:
        if self.cap is None:
            return None
        return max(Decimal(0), self.cap - self.spent)

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "cap_usd": None if self.cap is None else str(self.cap),
            "spent_usd": str(self.spent),
            "remaining_usd": None if self.remaining is None else str(self.remaining),
            "fraction": self.fraction,
            "state": self.state,
        }


@dataclass
class SpendGuard:
    """Enforces a per-run cap and a daily cap.

    ``run_cap`` of ``None`` means "no run cap", which is how live single calls from Compare and
    Inspect behave — they are still bounded by the daily cap. ``daily_cap`` of ``None`` means no
    daily cap, which only happens when ``DAILY_BUDGET_USD`` is unset.

    ``prior_daily_spend`` seeds today's total from the ledger so a restarted process does not
    forget what it already spent.
    """

    run_cap: Decimal | None = None
    daily_cap: Decimal | None = None
    prior_daily_spend: Decimal = Decimal(0)
    clock: Callable[[], datetime] = datetime.now
    run_spend: Decimal = field(default=Decimal(0), init=False)
    _daily: dict[date, Decimal] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        if self.run_cap is not None and self.run_cap < 0:
            raise ValueError("run cap cannot be negative")
        if self.daily_cap is not None and self.daily_cap < 0:
            raise ValueError("daily cap cannot be negative")
        self._daily[self.clock().date()] = self.prior_daily_spend

    @property
    def today(self) -> date:
        return self.clock().date()

    @property
    def daily_spend(self) -> Decimal:
        return self._daily.get(self.today, Decimal(0))

    def status(self) -> list[BudgetStatus]:
        return [
            BudgetStatus(
                "run",
                self.run_cap,
                self.run_spend,
                None if self.run_cap is None else self.run_cap * WARNING_FRACTION,
            ),
            BudgetStatus(
                "daily",
                self.daily_cap,
                self.daily_spend,
                None if self.daily_cap is None else self.daily_cap * WARNING_FRACTION,
            ),
        ]

    def preflight(self, projected_cost: Decimal) -> None:
        """Refuse the call if its projected cost would take either cap past 100%.

        Called before every adapter call. The projection is deliberately pessimistic — input
        tokens plus the full ``max_tokens``, or the historical p95 output — so the guard errs
        towards refusing a call rather than towards an overspend it cannot undo.
        """
        if projected_cost < 0:
            raise ValueError("a projected cost cannot be negative")
        if self.run_cap is not None and self.run_spend + projected_cost > self.run_cap:
            raise BudgetExceeded("run", self.run_cap, self.run_spend, projected_cost)
        if self.daily_cap is not None and self.daily_spend + projected_cost > self.daily_cap:
            raise BudgetExceeded("daily", self.daily_cap, self.daily_spend, projected_cost)

    def record(self, actual_cost: Decimal) -> None:
        """Record what a call really cost, after the fact.

        Actuals are recorded even when they overshoot the projection: the guard's job is to
        know what was spent, not to make the books agree with its own forecast.
        """
        if actual_cost < 0:
            raise ValueError("an actual cost cannot be negative")
        self.run_spend += actual_cost
        today = self.today
        self._daily[today] = self._daily.get(today, Decimal(0)) + actual_cost

    def would_exceed(self, projected_cost: Decimal) -> str | None:
        """Which cap a projected cost would break, without raising. Used by preflight displays."""
        try:
            self.preflight(projected_cost)
        except BudgetExceeded as exc:
            return exc.cap_name
        return None

    def warnings(self) -> list[str]:
        return [
            f"{s.name} budget is at {s.fraction:.0%} of ${s.cap}"
            for s in self.status()
            if s.state in ("warning", "stopped") and s.fraction is not None
        ]


def projected_cost(
    input_tokens: int,
    max_output_tokens: int,
    input_rate_per_mtok: Decimal,
    output_rate_per_mtok: Decimal,
) -> Decimal:
    """Pessimistic preflight cost: all input uncached, output at the full cap.

    Cache reads and a shorter-than-cap completion only ever make the real cost lower, so this
    is an upper bound on what the call can cost — which is what a guard needs.
    """
    if input_tokens < 0 or max_output_tokens < 0:
        raise ValueError("token counts cannot be negative")
    million = Decimal(1_000_000)
    return (
        Decimal(input_tokens) * input_rate_per_mtok
        + Decimal(max_output_tokens) * output_rate_per_mtok
    ) / million
