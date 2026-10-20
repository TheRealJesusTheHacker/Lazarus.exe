"""Restore reports: what came back, in plain language.

Every restore ends with one of these. The report never hides behind
codes or stack traces: it counts what was restored, names anything
that could not be, and points at the safety copy that makes the
restore itself undoable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

OUTCOME_RESTORED = "restored"
OUTCOME_ALREADY_CURRENT = "already_current"
OUTCOME_NOT_RESTORED = "not_restored"  # the saved copy was unusable
OUTCOME_FAILED = "failed"  # writing it back did not work


@dataclass
class RestoreItem:
    path: str
    outcome: str
    message: str = ""

    def to_dict(self) -> dict:
        return {"path": self.path, "outcome": self.outcome, "message": self.message}


@dataclass
class RestoreReport:
    snapshot_id: str
    snapshot_label: str
    snapshot_created_utc: str
    destination: str
    snapshot_complete: bool = True
    safety_snapshot_id: Optional[str] = None
    items: list[RestoreItem] = field(default_factory=list)

    # -- counts ------------------------------------------------------

    def _count(self, outcome: str) -> int:
        return sum(1 for i in self.items if i.outcome == outcome)

    @property
    def restored_count(self) -> int:
        return self._count(OUTCOME_RESTORED)

    @property
    def already_current_count(self) -> int:
        return self._count(OUTCOME_ALREADY_CURRENT)

    @property
    def not_restored_count(self) -> int:
        return self._count(OUTCOME_NOT_RESTORED)

    @property
    def failed_count(self) -> int:
        return self._count(OUTCOME_FAILED)

    @property
    def problem_count(self) -> int:
        return self.not_restored_count + self.failed_count

    @property
    def status(self) -> str:
        """complete | partial | failed — the whole restore in one word."""
        if self.problem_count == 0:
            return "complete"
        if self.restored_count + self.already_current_count > 0:
            return "partial"
        return "failed"

    # -- rendering ----------------------------------------------------

    def summary_lines(self) -> list[str]:
        lines: list[str] = []
        when = self.snapshot_created_utc[:10] if self.snapshot_created_utc else ""
        title = f"Restore from the snapshot taken {when}"
        if self.snapshot_label:
            title += f" ({self.snapshot_label})"
        lines.append(title + ".")
        lines.append(f"Restored to: {self.destination}")
        if not self.snapshot_complete:
            lines.append(
                "Note: that snapshot was itself incomplete — some things were "
                "never saved into it, so they cannot come back from it."
            )
        good = self.restored_count + self.already_current_count
        if self.status == "complete":
            lines.append(f"Everything came back: {good} item(s) in place.")
        elif self.status == "partial":
            lines.append(
                f"{good} item(s) came back. "
                f"{self.problem_count} item(s) could not be restored:"
            )
        else:
            lines.append("Nothing could be restored. The problems were:")
        for item in self.items:
            if item.outcome in (OUTCOME_NOT_RESTORED, OUTCOME_FAILED):
                reason = item.message or "It could not be restored."
                lines.append(f"  - {item.path}: {reason}")
        if self.already_current_count:
            lines.append(
                f"{self.already_current_count} item(s) were already exactly "
                "as the snapshot had them, and were left untouched."
            )
        if self.safety_snapshot_id:
            lines.append(
                "Before anything was overwritten, the way things were was "
                f"saved as snapshot {self.safety_snapshot_id}. Restoring that "
                "snapshot undoes this restore."
            )
        else:
            lines.append("Nothing was overwritten, so no safety copy was needed.")
        return lines

    def to_text(self) -> str:
        return "\n".join(self.summary_lines())

    def to_dict(self) -> dict:
        return {
            "snapshot_id": self.snapshot_id,
            "snapshot_label": self.snapshot_label,
            "snapshot_created_utc": self.snapshot_created_utc,
            "destination": self.destination,
            "snapshot_complete": self.snapshot_complete,
            "safety_snapshot_id": self.safety_snapshot_id,
            "status": self.status,
            "restored": self.restored_count,
            "already_current": self.already_current_count,
            "not_restored": self.not_restored_count,
            "failed": self.failed_count,
            "items": [i.to_dict() for i in self.items],
        }
