"""Local task-level accounting for interchangeable paid workers.

This records CLI/provider estimates; it cannot impose a hard provider billing cap.
reserve() returns min(requested_cap, remaining budget) as Decimal. Reservations
created by this object may run concurrently. Pending entries loaded by another
object are unproven interrupted calls: reconcile them using actual cost evidence
before starting another paid invocation. A process-level runner lock is assumed;
all objects for the same path share an additional in-process thread lock.

The JSON contains identifiers and monetary values only. Never put credentials,
prompts, provider responses, or account tokens into identifiers.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import re
import tempfile
import threading
from typing import Any

_LOCKS: dict[str, Any] = {}
_LOCKS_GUARD = threading.Lock()
_STATES = {"pending", "reconciled", "unknown"}


def _amount(value: Any, label: str) -> Decimal:
    if isinstance(value, (float, bool)) or not isinstance(value, (Decimal, int, str)):
        raise RuntimeError(f"{label} must be Decimal, integer, or decimal text; not float")
    try:
        amount = Decimal(value)
    except (InvalidOperation, ValueError, TypeError):
        raise RuntimeError(f"Invalid {label}") from None
    if not amount.is_finite() or amount < 0:
        raise RuntimeError(f"{label} must be finite and nonnegative")
    return amount


def _identifier(value: Any, label: str) -> str:
    if (not isinstance(value, str) or not value or value != value.strip()
            or len(value) > 1024 or re.search(r"[\x00-\x1f\x7f?@=]", value)):
        raise RuntimeError(f"Invalid {label}; use a non-secret invocation/path identifier")
    return value


class BudgetLedger:
    """Persistent USD ledger with conservative interrupted-call handling."""

    def __init__(self, path: str | Path, total_cap: Decimal | int | str):
        self.path = Path(path).resolve()
        self.total_cap = _amount(total_cap, "total cap")
        if self.total_cap <= 0:
            raise RuntimeError("Total cap must be positive")
        key = os.path.normcase(str(self.path))
        with _LOCKS_GUARD:
            self._lock = _LOCKS.setdefault(key, threading.RLock())
        self._active: set[str] = set()
        with self._lock:
            if self.path.exists():
                self._read()
            else:
                self._write({"schema_version": 1, "total_cap_usd": str(self.total_cap), "invocations": {}})

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise RuntimeError("Budget ledger cannot be read; do not reset missing/corrupt accounting") from None
        if (not isinstance(data, dict) or set(data) != {"schema_version", "total_cap_usd", "invocations"}
                or data["schema_version"] != 1 or not isinstance(data["invocations"], dict)):
            raise RuntimeError("Unsupported or corrupt budget ledger")
        if _amount(data["total_cap_usd"], "saved total cap") != self.total_cap:
            raise RuntimeError("Saved task cap differs; changing an adapter must not reset its budget")
        for invocation, entry in data["invocations"].items():
            _identifier(invocation, "saved invocation")
            if (not isinstance(entry, dict)
                    or set(entry) != {"provider", "state", "reserved_cap_usd", "cost_usd", "source"}
                    or entry["state"] not in _STATES or entry["source"] not in {"reservation", "legacy"}):
                raise RuntimeError("Corrupt invocation accounting")
            self._provider(entry["provider"])
            _amount(entry["reserved_cap_usd"], "saved reservation")
            if entry["state"] == "reconciled":
                _amount(entry["cost_usd"], "saved actual cost")
            elif entry["cost_usd"] is not None:
                raise RuntimeError("Pending/unknown invocation has an invalid actual cost")
        return data

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix="." + self.path.name + ".",
                                             suffix=".tmp", dir=self.path.parent, delete=False) as handle:
                temporary = Path(handle.name)
                json.dump(data, handle, ensure_ascii=False, indent=2, allow_nan=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except OSError:
            raise RuntimeError("Atomic budget write failed; no new reservation was approved") from None
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()

    @staticmethod
    def _provider(value: Any) -> str:
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value):
            raise RuntimeError("Provider must be a short non-secret adapter identifier, not a URL/key")
        return value

    def _snapshot(self, data: dict) -> dict:
        spent = Decimal("0")
        reserved = Decimal("0")
        unknown = []
        interrupted = []
        exceeded = []
        for invocation, entry in data["invocations"].items():
            reservation = _amount(entry["reserved_cap_usd"], "reservation")
            if entry["state"] == "reconciled":
                cost = _amount(entry["cost_usd"], "actual cost")
                spent += cost
                if entry["source"] == "reservation" and cost > reservation:
                    exceeded.append(invocation)
            elif entry["state"] == "pending":
                reserved += reservation
                if invocation not in self._active:
                    interrupted.append(invocation)
            else:
                unknown.append(invocation)
        remaining = self.total_cap - spent - reserved
        reasons = []
        if unknown:
            reasons.append("Unknown costs require actual cost evidence before further paid calls")
        if interrupted:
            reasons.append("Interrupted/unproven pending reservations require reconciliation; never reset automatically")
        if remaining <= 0:
            reasons.append("Task budget is fully spent or reserved")
        blocked = bool(reasons)
        return {
            "schema_version": 1,
            "total_cap_usd": str(self.total_cap),
            "spent_usd": str(spent), # Sum of KNOWN costs; costs_complete indicates any unknown total.
            "reserved_usd": str(reserved),
            "remaining_usd": str(remaining),
            "available_usd": str(max(Decimal("0"), remaining)) if not blocked else "0",
            "costs_complete": not unknown and not interrupted and reserved == 0,
            "blocked": blocked,
            "block_reasons": reasons,
            "unknown_invocations": unknown,
            "interrupted_invocations": interrupted,
            "reservation_exceeded_invocations": exceeded,
            "invocations": data["invocations"],
            "billing_limit_note": "CLI/provider estimates only; actual charges can exceed reservations or the task cap",
        }

    def snapshot(self) -> dict:
        """Return a fresh JSON-safe snapshot; decimal USD fields are strings."""
        with self._lock:
            return self._snapshot(self._read())

    def import_cost(self, invocation: str, amount: Decimal | int | str | None) -> None:
        """Import prior actual usage once; a conflicting duplicate is rejected.

        Matching settled reservations are also idempotent, allowing the caller
        to import the same real usage log on subsequent task starts. Unknown
        legacy cost remains blocking until reconcile() receives actual evidence.
        """
        invocation = _identifier(invocation, "invocation")
        cost = None if amount is None else _amount(amount, "imported actual cost")
        with self._lock:
            data = self._read()
            existing = data["invocations"].get(invocation)
            if existing is not None:
                if existing["state"] == "pending":
                    raise RuntimeError("Invocation is still pending; reconcile it instead of importing over it")
                old_cost = None if existing["cost_usd"] is None else _amount(existing["cost_usd"], "saved actual cost")
                if old_cost != cost:
                    raise RuntimeError("Duplicate usage has a conflicting actual cost; prior accounting was retained")
                return
            data["invocations"][invocation] = {
                "provider": "legacy", "state": "unknown" if cost is None else "reconciled",
                "reserved_cap_usd": "0", "cost_usd": None if cost is None else str(cost), "source": "legacy",
            }
            self._write(data)

    def reserve(self, invocation: str, provider: str, requested_cap: Decimal | int | str) -> Decimal:
        """Reserve min(requested cap, unspent/unreserved task budget).

        Unknown costs or unproven disk reservations fail closed. Active
        reservations from this object reduce remaining budget and may coexist.
        """
        invocation = _identifier(invocation, "invocation")
        provider = self._provider(provider)
        requested = _amount(requested_cap, "requested cap")
        if requested <= 0:
            raise RuntimeError("Requested cap must be positive")
        with self._lock:
            data = self._read()
            if invocation in data["invocations"]:
                raise RuntimeError("Invocation already has accounting; use a new approved invocation identity")
            snapshot = self._snapshot(data)
            if snapshot["blocked"]:
                raise RuntimeError("Cannot reserve paid invocation: " + "; ".join(snapshot["block_reasons"]))
            granted = min(requested, _amount(snapshot["available_usd"], "available budget"))
            data["invocations"][invocation] = {
                "provider": provider, "state": "pending", "reserved_cap_usd": str(granted),
                "cost_usd": None, "source": "reservation",
            }
            self._write(data)
            self._active.add(invocation)
            return granted

    def reconcile(self, invocation: str, amount: Decimal | int | str | None) -> None:
        """Record actual cost, including failed/over-budget calls; never clip it.

        This is also the explicit recovery path for a persisted pending/unknown
        call. The caller must provide real usage evidence, not a guessed zero.
        A matching repeated reconciliation is safe; changing a known cost fails.
        """
        invocation = _identifier(invocation, "invocation")
        cost = None if amount is None else _amount(amount, "actual cost")
        with self._lock:
            data = self._read()
            entry = data["invocations"].get(invocation)
            if entry is None:
                raise RuntimeError("Cannot reconcile an invocation without an existing accounting record")
            if entry["state"] == "reconciled":
                if cost != _amount(entry["cost_usd"], "saved actual cost"):
                    raise RuntimeError("Invocation already reconciled with a different actual cost")
                return
            # The call has ended. If persistence fails, the old disk pending
            # entry must become unproven and block another paid invocation.
            self._active.discard(invocation)
            entry["state"] = "unknown" if cost is None else "reconciled"
            entry["cost_usd"] = None if cost is None else str(cost)
            self._write(data)
