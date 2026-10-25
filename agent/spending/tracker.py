"""Keeps a running total of what has been spent and refuses payments that would break the policy."""

import threading
from dataclasses import dataclass

from .policy import SpendingPolicy, micros_to_usd


@dataclass(frozen=True)
class SpendDecision:
    ok: bool
    reservation_id: int = 0
    reason: str = ""


class SpendingTracker:
    """A payment is reserved *before* it is signed and only released again if it definitely
    failed, so the total can never be under-counted.

    State lives in memory: it resets when the process restarts.
    """

    def __init__(self, policy: SpendingPolicy) -> None:
        self.policy = policy
        self._spent = 0
        self._payments = 0
        self._next_id = 1
        self._reservations: dict[int, int] = {}
        self._lock = threading.Lock()

    def try_reserve(self, amount_micros: int) -> SpendDecision:
        with self._lock:
            if amount_micros <= 0:
                return SpendDecision(ok=False, reason="Payment amount must be positive")
            if amount_micros > self.policy.max_payment_micros:
                return SpendDecision(
                    ok=False,
                    reason=f"Payment of ${micros_to_usd(amount_micros)} is above the per-payment cap "
                    f"of ${micros_to_usd(self.policy.max_payment_micros)}",
                )
            if self._spent + amount_micros > self.policy.budget_micros:
                return SpendDecision(
                    ok=False,
                    reason=f"Payment of ${micros_to_usd(amount_micros)} would exceed the budget "
                    f"(${micros_to_usd(self.remaining())} left of ${micros_to_usd(self.policy.budget_micros)})",
                )
            reservation_id = self._next_id
            self._next_id += 1
            self._reservations[reservation_id] = amount_micros
            self._spent += amount_micros
            self._payments += 1
            return SpendDecision(ok=True, reservation_id=reservation_id)

    def release(self, reservation_id: int) -> None:
        """Gives the money back to the budget, e.g. when the payment was never settled."""
        with self._lock:
            amount = self._reservations.pop(reservation_id, None)
            if amount is None:
                return
            self._spent -= amount
            self._payments -= 1

    def commit(self, reservation_id: int) -> None:
        """Marks a reservation as final (settled on-chain)."""
        with self._lock:
            self._reservations.pop(reservation_id, None)

    def remaining(self) -> int:
        return self.policy.budget_micros - self._spent

    def snapshot(self) -> dict[str, object]:
        return {
            "budgetUsd": micros_to_usd(self.policy.budget_micros),
            "maxPaymentUsd": micros_to_usd(self.policy.max_payment_micros),
            "spentUsd": micros_to_usd(self._spent),
            "remainingUsd": micros_to_usd(self.remaining()),
            "payments": self._payments,
        }
