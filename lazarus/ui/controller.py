"""The window's hands: every engine call, and no Qt anywhere.

The controller holds the one open store, turns engine records into
plain view data (dicts of strings and numbers the window can show
as-is), and wraps each user action in exactly the engine call — or
the CLI's composition of calls — that the command line makes. It
reimplements no engine logic: no hashing, no odds, no retention
maths live here. Engine refusals travel as :class:`LazarusError`,
whose ``str()`` *is* the display text; the window shows it as-is.
"""

from __future__ import annotations

import os
import stat
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from .._version import __version__
from ..assess import assess_options
from ..backup import (
    _acquire_run_lock,
    _release_run_lock,
    backup_snapshot_ids,
    backup_status,
    default_config_path,
    history_path_for,
    load_config,
    load_history,
    run_backup,
    save_config,
    validate_config,
)
from ..backup_model import (
    RETENTION_COUNT,
    RETENTION_DAYS,
    TRIGGER_MANUAL,
    BackupConfig,
    RetentionSpec,
    ScheduleSpec,
    human_bytes,
)
from ..decrypt_run import SubprocessRunner, prepare_work_area, run_decryptor
from ..decryptors import find_entry, identify_family, load_registry
from ..errors import (
    BackupConfigError,
    DecryptorError,
    LazarusError,
    SourceNotFoundError,
    StoreError,
)
from ..model import KIND_PRE_RESTORE
from ..rescue import RegionMap, rescue_to_image
from ..restore import restore
from ..retention import apply_retention, plan_retention
from ..schedule import next_scheduled_instant, register_schedule, unregister_schedule
from ..snapshot import create_snapshot
from ..store import SnapshotStore, format_utc, new_snapshot_id, utc_now
from ..undelete import recover_deleted, scan_for_deleted
from ..unlock import default_platform, unlock_volume
from ..verify import verify_snapshot, verify_store

#: How long a decryptor run may take — the CLI's own limit.
DECRYPT_TIMEOUT_SECONDS = 4 * 60 * 60

#: The odds words the engine itself uses for each verdict.
VERDICT_WORDS = {
    "strong": "likely whole",
    "partial": "partly there",
    "gone": "gone",
}

#: Backup history words, exactly as the CLI prints them.
_TRIGGER_WORDS = {"manual": "by hand", "scheduled": "scheduled", "catch-up": "catch-up"}
_OUTCOME_WORDS = {
    "complete": "complete",
    "partial": "partial",
    "failed": "failed",
    "refused": "refused",
}


def display_error(error: BaseException) -> str:
    """The one line a failure may show: the engine's own words."""
    if isinstance(error, LazarusError):
        return str(error)
    return (
        "Something went wrong inside Lazarus. Nothing was changed "
        "by this step. Try again, and if it keeps happening, note "
        "what you were doing and restart Lazarus."
    )


def _refuse_pipe_or_socket(path, *, destination: bool = False, follow_link: bool = False) -> None:
    """Refuse a pipe or a socket before any engine call can block on it.

    Opening a pipe with no writer on the other end waits forever,
    and a socket is no more readable as a drive or a file — so the
    window's layer names what the thing *is* and what to pick
    instead, in plain words, and never hands it to the engine.
    Directories, regular files, and everything else pass through
    untouched: the engine refuses what it cannot use, in its own
    words.

    With ``follow_link``, a shortcut (a symlink) is judged by what
    it points at: the engine follows the link when it opens the
    path, so a shortcut to a pipe would block it exactly as a
    direct pipe would. A broken shortcut — one whose target is
    not there — passes through untouched: the engine refuses it
    in its own words. Callers whose engine never opens the target
    (a snapshot stores a shortcut as a shortcut) leave
    ``follow_link`` off and keep the direct check only.
    """
    try:
        mode = os.lstat(path).st_mode
    except (OSError, ValueError):
        return  # missing or unnameable: the engine says so, plainly
    if follow_link and stat.S_ISLNK(mode):
        try:
            mode = os.stat(path).st_mode
        except OSError:
            return  # broken shortcut: the engine says so, plainly
    if stat.S_ISFIFO(mode):
        kind = "a pipe"
    elif stat.S_ISSOCK(mode):
        kind = "a socket"
    else:
        return
    if destination:
        raise LazarusError(
            f"'{path}' is {kind}, not a place Lazarus can write "
            "the rescue image to. Pick a new image file name "
            "instead — a file Lazarus can write from start to finish."
        )
    raise LazarusError(
        f"'{path}' is {kind}, not a drive, an image, a file, or "
        "a folder Lazarus can read. Pick a drive, an image file, "
        "a file, or a folder instead."
    )


def _date_led(created_utc: str) -> str:
    """'2026-10-20 07:30 UTC' from a stored timestamp, or 'date unknown'."""
    if not created_utc:
        return "date unknown"
    return created_utc.replace("T", " ")[:16] + " UTC"


def snapshot_view(info) -> dict:
    kind_words = "safety copy" if info.kind == KIND_PRE_RESTORE else "snapshot"
    return {
        "id": info.id,
        "kind": info.kind,
        "kind_words": kind_words,
        "label": info.label,
        "created_utc": info.created_utc,
        "date_text": _date_led(info.created_utc),
        "complete": info.complete,
        "state_words": "complete" if info.complete else "INCOMPLETE",
        "has_manifest": info.has_manifest,
        "file_count": info.file_count,
        "total_bytes": info.total_bytes,
        "size_text": human_bytes(info.total_bytes),
        "row_text": (
            f"{_date_led(info.created_utc)} — {kind_words}, "
            f"{'complete' if info.complete else 'INCOMPLETE'}, "
            f"{info.file_count} file(s), {human_bytes(info.total_bytes)}"
            + (f" — “{info.label}”" if info.label else "")
            if info.has_manifest
            else f"{info.id} — unfinished snapshot (never fully saved; cannot be used)"
        ),
    }


class Controller:
    """State and actions for one Lazarus window."""

    def __init__(
        self,
        config_path: Optional[Path] = None,
        platform=None,
        program: str = "lazarus",
    ) -> None:
        self.store: Optional[SnapshotStore] = None
        self.config_path = Path(config_path) if config_path else default_config_path()
        self._platform = platform  # unlock seam; None = this computer's own
        self.program = program
        self._registry = None
        # The prune plan the owner was last shown, as the exact
        # (config, store, plan) the apply must use — a plan is
        # single-use, and saving backup settings or running a
        # backup puts it out of date (see prune_plan_view).
        self._stored_prune_plan = None

    # -- the unlock seam ------------------------------------------------

    @property
    def platform(self):
        if self._platform is None:
            self._platform = default_platform()
        return self._platform

    @platform.setter
    def platform(self, value) -> None:
        self._platform = value

    # -- store -----------------------------------------------------------

    @property
    def store_path(self) -> Optional[str]:
        return str(self.store.root) if self.store is not None else None

    def open_store(self, path) -> dict:
        store = SnapshotStore.open(Path(path))
        self.store = store
        return {"store_path": str(store.root), "message": f"Store open: {store.root}"}

    def create_store(self, path) -> dict:
        store = SnapshotStore.create(Path(path))
        self.store = store
        return {
            "store_path": str(store.root),
            "message": (
                f"Store created at {store.root}. Snapshots you take "
                "will live here, dated and ready to restore."
            ),
        }

    def require_store(self) -> SnapshotStore:
        if self.store is None:
            raise StoreError(
                "No store is open yet. Open a store, or make a new "
                "one, and Lazarus can show what it holds."
            )
        return self.store

    def list_snapshots(self) -> list[dict]:
        return [snapshot_view(info) for info in self.require_store().list_snapshots()]

    def browse_snapshot(self, snapshot_id: str) -> dict:
        store = self.require_store()
        manifest = store.load_manifest(snapshot_id)
        return {
            "id": manifest.id,
            "kind": manifest.kind,
            "label": manifest.label,
            "created_utc": manifest.created_utc,
            "date_text": _date_led(manifest.created_utc),
            "complete": manifest.complete,
            "source_root": manifest.source_root or "",
            "root_name": manifest.root_name,
            "file_count": manifest.file_count,
            "total_bytes": manifest.total_bytes,
            "size_text": human_bytes(manifest.total_bytes),
            "entries": [
                {
                    "path": entry.path,
                    "type": entry.type,
                    "size": entry.size,
                    "size_text": human_bytes(entry.size) if entry.type == "file" else "",
                }
                for entry in manifest.entries
            ],
            "problems": [
                {"path": p.path, "message": p.message} for p in manifest.problems
            ],
        }

    # -- snapshots ---------------------------------------------------------

    def take_snapshot(self, source, label: str = "") -> dict:
        store = self.require_store()
        _refuse_pipe_or_socket(source)
        manifest = create_snapshot(store, Path(source), label=label or "")
        return {
            "id": manifest.id,
            "created_utc": manifest.created_utc,
            "date_text": _date_led(manifest.created_utc),
            "label": manifest.label,
            "complete": manifest.complete,
            "file_count": manifest.file_count,
            "total_bytes": manifest.total_bytes,
            "size_text": human_bytes(manifest.total_bytes),
            "problems": [
                {"path": p.path, "message": p.message} for p in manifest.problems
            ],
            "lines": [
                f"Snapshot saved: {manifest.id}",
                f"Taken: {_date_led(manifest.created_utc)}",
                f"Holds: {manifest.file_count} file(s), "
                f"{human_bytes(manifest.total_bytes)}",
            ]
            + ([f"Label: {manifest.label}"] if manifest.label else [])
            + (
                ["This snapshot is INCOMPLETE. These could not be saved:"]
                + [
                    f"  - {p.path}: {p.message}" if p.path else f"  - {p.message}"
                    for p in manifest.problems
                ]
                if not manifest.complete
                else []
            ),
        }

    # -- restore -----------------------------------------------------------

    @staticmethod
    def normalise_selection(manifest_entries: list[dict], selected) -> Optional[list[str]]:
        """Collapse a checkbox selection to the paths restore needs.

        ``None`` (or a selection covering every entry) means
        "everything". A chosen folder already covers everything
        under it, so covered paths drop out. This is selection
        bookkeeping only — the engine still chooses what each path
        restores.
        """
        if selected is None:
            return None
        chosen = {str(p).replace("\\", "/").strip("/") for p in selected if str(p).strip()}
        if not chosen:
            return None
        all_paths = {entry["path"] for entry in manifest_entries}
        if all_paths and all_paths <= chosen:
            return None
        kept = [
            path
            for path in sorted(chosen)
            if not any(path != other and path.startswith(other + "/") for other in chosen)
        ]
        return kept or None

    def restore_selection(
        self,
        snapshot_id: str,
        destination,
        selected=None,
    ) -> dict:
        store = self.require_store()
        manifest = store.load_manifest(snapshot_id)
        entries = [
            {"path": e.path, "type": e.type} for e in manifest.entries
        ]
        paths = self.normalise_selection(entries, selected)
        dest = Path(destination) if destination else None
        if paths is None:
            reports = [
                restore(store, destination=dest, snapshot_id=snapshot_id, path=None)
            ]
            lines: list[str] = []
            problems: list[dict] = []
            safety_ids: list[str] = []
            for report in reports:
                lines.extend(report.summary_lines())
                if report.safety_snapshot_id:
                    safety_ids.append(report.safety_snapshot_id)
                for item in report.items:
                    if item.outcome in ("not_restored", "failed"):
                        problems.append({"path": item.path, "message": item.message})
            return {
                "status": reports[0].status,
                "lines": lines,
                "problems": problems,
                "safety_snapshot_ids": safety_ids,
                "reports": reports,
            }
        # Several roots, one call each. A root the engine refuses
        # (for example, its safety copy could not be saved in full)
        # must not swallow the roots that already came back: its
        # refusal is named here, root by root, beside the reports
        # of the roots that did restore.
        reports = []
        refusals: list[dict] = []
        lines = []
        problems = []
        safety_ids = []
        came_back = 0
        problem_count = 0
        for path in paths:
            try:
                report = restore(
                    store, destination=dest, snapshot_id=snapshot_id, path=path
                )
            except LazarusError as error:
                if len(paths) == 1:
                    # One root only: nothing else has run, so the
                    # engine's refusal stands on its own, as before.
                    raise
                refusals.append({"path": path, "message": str(error)})
                lines.append(f"{path}: {error}")
                problems.append({"path": path, "message": str(error)})
                problem_count += 1
                continue
            reports.append(report)
            lines.extend(report.summary_lines())
            if report.safety_snapshot_id:
                safety_ids.append(report.safety_snapshot_id)
            came_back += report.restored_count + report.already_current_count
            problem_count += report.problem_count
            for item in report.items:
                if item.outcome in ("not_restored", "failed"):
                    problems.append({"path": item.path, "message": item.message})
        # The merged status is the engine's own semantics, widened
        # across roots: "complete" only when every root completed
        # clean; "failed" only when nothing at all came back
        # anywhere and there were problems or refusals; anything
        # in between is "partial".
        if problem_count == 0:
            status = "complete"
        elif came_back == 0:
            status = "failed"
        else:
            status = "partial"
        return {
            "status": status,
            "lines": lines,
            "problems": problems,
            "safety_snapshot_ids": safety_ids,
            "reports": reports,
            "refusals": refusals,
        }

    # -- verify --------------------------------------------------------------

    def verify_store_view(self) -> dict:
        result = verify_store(self.require_store())
        return {
            "ok": result.ok,
            "snapshots_checked": result.snapshots_checked,
            "problems": [
                {"path": p.path, "message": p.message} for p in result.problems
            ],
            "lines": (
                [
                    f"The whole store checked out: {result.snapshots_checked} "
                    "snapshot(s) verified, byte for byte."
                ]
                if result.ok
                else ["The store has problems:"]
                + [f"  - {p.path}: {p.message}" for p in result.problems]
            ),
        }

    def verify_snapshot_view(self, snapshot_id: str) -> dict:
        result = verify_snapshot(self.require_store(), snapshot_id)
        return {
            "ok": result.ok,
            "summary": result.summary(),
            "problems": [
                {"path": p.path, "message": p.message} for p in result.problems
            ],
        }

    # -- deleted files ---------------------------------------------------------

    def scan_deleted(self, source) -> dict:
        _refuse_pipe_or_socket(source, follow_link=True)
        report = scan_for_deleted(Path(source))
        return {
            "source_description": report.source_description,
            "fs_type": report.fs_type,
            "notes": list(report.notes),
            "lines": report.summary_lines(),
            "strong_count": report.strong_count,
            "partial_count": report.partial_count,
            "gone_count": report.gone_count,
            "files": [
                {
                    "name": a.name,
                    "size": a.size,
                    "size_text": human_bytes(a.size),
                    "verdict": a.verdict,
                    "verdict_words": VERDICT_WORDS.get(a.verdict, a.verdict),
                    "surviving_bytes": a.surviving_bytes,
                    "missing_bytes": a.missing_bytes,
                    "summary": a.summary,
                    "evidence": list(a.evidence),
                }
                for a in report.assessments
            ],
        }

    def recover(self, source, destination, names=None) -> dict:
        _refuse_pipe_or_socket(source, follow_link=True)
        report = recover_deleted(
            Path(source), Path(destination), names=list(names) if names else None
        )
        return {
            "status": report.status,
            "lines": report.summary_lines(),
            "destination": report.destination,
            "report": report,
        }

    # -- drive rescue ------------------------------------------------------------

    @staticmethod
    def default_map_path(destination) -> str:
        return str(destination) + ".map.json"

    def rescue(self, source, destination, map_path=None) -> dict:
        _refuse_pipe_or_socket(source, follow_link=True)
        _refuse_pipe_or_socket(destination, destination=True, follow_link=True)
        report = rescue_to_image(
            Path(source),
            Path(destination),
            map_path=Path(map_path) if map_path else None,
        )
        return {
            "status": report.status,
            "lines": report.summary_lines(),
            "regions_total": report.regions_total,
            "regions_recovered": report.regions_recovered,
            "bytes_recovered": report.bytes_recovered,
            "bytes_missing": report.bytes_missing,
            "total_bytes": report.total_bytes,
            "report": report,
        }

    @staticmethod
    def read_rescue_progress(map_path) -> Optional[dict]:
        """Live rescue numbers, read from the progress file.

        Returns ``None`` when there is no readable map yet (the
        rescue has not written its first one). Numbers come straight
        from the map's own records: a region counts as recovered
        when nothing of it is still missing, and bytes are the
        source size less every hole the map still lists — the same
        arithmetic the engine's own report uses.
        """
        try:
            region_map = RegionMap.load(Path(map_path))
        except LazarusError:
            return None
        bytes_missing = 0
        recovered = 0
        partial = 0
        unrecovered = 0
        untested = 0
        for index, region in enumerate(region_map.regions):
            hole_bytes = sum(length for _start, length in region["holes"])
            bytes_missing += hole_bytes
            if not region["holes"]:
                recovered += 1
            elif region["state"] == "untested":
                untested += 1
            elif hole_bytes >= region_map.region_length(index):
                unrecovered += 1
            else:
                partial += 1
        total = region_map.source_size
        return {
            "regions_total": region_map.region_count,
            "regions_recovered": recovered,
            "regions_partial": partial,
            "regions_unrecovered": unrecovered,
            "regions_untested": untested,
            "total_bytes": total,
            "bytes_recovered": total - bytes_missing,
            "bytes_missing": bytes_missing,
        }

    # -- locked volumes ------------------------------------------------------------

    def list_volumes(self) -> dict:
        try:
            volumes = self.platform.list_volumes()
        except LazarusError as error:
            return {"available": False, "message": str(error), "volumes": []}
        rows = []
        for volume in volumes:
            if (volume.kind or "").strip().lower() == "bitlocker":
                if volume.locked is True:
                    state = "locked"
                elif volume.locked is False:
                    state = "open"
                else:
                    state = "lock state unknown"
                kind_words = "BitLocker"
            else:
                state = "lock state unknown" if volume.locked is None else (
                    "locked" if volume.locked else "open"
                )
                kind_words = volume.kind
            rows.append(
                {
                    "ref": volume.ref,
                    "kind": volume.kind,
                    "kind_words": kind_words,
                    "label": volume.label,
                    "locked": volume.locked,
                    "state_words": state,
                    "evidence": list(volume.evidence),
                }
            )
        return {"available": True, "message": "", "volumes": rows}

    def unlock(self, ref: str, key_provider: Callable[[], str]) -> dict:
        outcome = unlock_volume(self.platform, ref, key_provider)
        return {
            "status": outcome.status,
            "message": outcome.message,
            "next_step": outcome.next_step,
            "text": outcome.to_text(),
            "ref": outcome.ref,
        }

    # -- ransomware families and decryptors -----------------------------------------

    def registry(self) -> list:
        if self._registry is None:
            self._registry = load_registry()
        return self._registry

    def registry_view(self) -> list[dict]:
        return [
            {
                "family": entry.family,
                "decryptor_name": entry.decryptor_name,
                "publisher": entry.publisher,
                "conditions": entry.conditions,
                "source_url": entry.source_url,
                "verified_utc": entry.verified_utc,
                "row_text": (
                    f"{entry.family} — {entry.decryptor_name}, free from "
                    f"{entry.publisher}. Checked {entry.verified_utc}."
                ),
            }
            for entry in self.registry()
        ]

    def identify(self, folder) -> dict:
        identification = identify_family(Path(folder), self.registry())
        return {
            "files_seen": identification.files_seen,
            "notes": list(identification.notes),
            "matches": [
                {
                    "family": match.entry.family,
                    "confidence": match.confidence,
                    "evidence": list(match.evidence),
                    "decryptor_name": match.entry.decryptor_name,
                    "publisher": match.entry.publisher,
                    "conditions": match.entry.conditions,
                }
                for match in identification.matches
            ],
            "no_match_text": (
                f"Lazarus does not recognize the signs in {folder} as "
                "any family on its list."
            ),
        }

    def resolve_decrypt_family(self, folder, family_name: Optional[str] = None) -> dict:
        """The CLI's family-choice rule, word for word.

        Returns ``{"status": "ready", "entry": entry}``,
        ``{"status": "choose", "choices": [...], "message": ...}``
        when the owner must pick, or ``{"status": "refused",
        "message": ...}`` carrying the CLI's plain wording.

        Naming a family settles it from the list alone — the
        folder is never read. With no name, the folder's signs are
        read here, once, and the plan is settled from that
        identification by :meth:`plan_from_identification`.
        """
        if family_name:
            return self.plan_from_identification(None, family_name)
        identification = identify_family(Path(folder), self.registry())
        return self.plan_from_identification(
            {
                "matches": [
                    {"family": match.entry.family, "confidence": match.confidence}
                    for match in identification.matches
                ]
            }
        )

    def plan_from_identification(self, identification, family_name: Optional[str] = None) -> dict:
        """Settle a family plan from an identification already made.

        The same rule, and the same words, as
        :meth:`resolve_decrypt_family` — but nothing is read from
        any folder: the plan comes only from ``identification``
        (the view data :meth:`identify` returned) and, when the
        owner has picked a family, from the decryptor list. The
        window uses this so that running a decryptor never sets
        off a fresh read of the folder on the window thread.
        """
        registry = self.registry()
        if family_name:
            entry = find_entry(registry, family_name)
            if entry is None:
                return {
                    "status": "refused",
                    "message": (
                        f"Lazarus has no decryptor listed for "
                        f"'{family_name}'. See the decryptor list on "
                        "this page for the families it knows, and use "
                        "one of those names."
                    ),
                }
            return {"status": "ready", "entry": entry}
        matches = list((identification or {}).get("matches") or [])
        looks_like = [m for m in matches if m["confidence"] == "looks like"]
        if len(looks_like) > 1:
            names = " and ".join(m["family"] for m in looks_like)
            return {
                "status": "choose",
                "choices": [m["family"] for m in matches],
                "message": (
                    f"The signs in this folder point plainly at more "
                    f"than one family — {names} — and Lazarus will "
                    f"not choose between families by itself. Pick the "
                    f"one to use."
                ),
            }
        if looks_like:
            return {"status": "ready", "entry": find_entry(registry, looks_like[0]["family"])}
        if matches:
            return {
                "status": "choose",
                "choices": [m["family"] for m in matches],
                "message": (
                    "The signs in this folder only suggest which "
                    "family locked these files — they do not point "
                    "to one plainly, so Lazarus will not choose a "
                    "decryptor by itself. If you know the family, "
                    "pick it here."
                ),
            }
        return {
            "status": "refused",
            "message": (
                "Lazarus could not tell which family locked these "
                "files, so it will not run a decryptor blind. Look "
                "at what Lazarus found first, or name the family."
            ),
        }

    def run_decrypt(self, folder, work_root, decryptor_path, family_name=None) -> dict:
        plan = self.resolve_decrypt_family(folder, family_name)
        if plan["status"] != "ready":
            raise DecryptorError(plan["message"])
        return self.run_decrypt_with_entry(
            plan["entry"], folder, work_root, decryptor_path
        )

    def run_decrypt_with_entry(self, entry, folder, work_root, decryptor_path) -> dict:
        work = prepare_work_area(Path(folder), Path(work_root))
        report = run_decryptor(
            entry,
            work,
            Path(decryptor_path),
            SubprocessRunner(timeout_seconds=DECRYPT_TIMEOUT_SECONDS),
        )
        return {
            "status": report.status,
            "text": report.to_text(),
            "lines": report.to_text().splitlines(),
            "files_copied": len(work.files),
            "work_problems": list(work.problems),
            "report": report,
        }

    # -- assess -----------------------------------------------------------------------

    def assess(self, path) -> dict:
        assessment = assess_options(Path(path), platform=self.platform)
        return {
            "target": assessment.target,
            "findings": list(assessment.findings),
            "options": list(assessment.options),
            "notes": list(assessment.notes),
            "text": assessment.to_text(),
        }

    # -- backup -------------------------------------------------------------------------

    def backup_configured(self) -> bool:
        return Path(self.config_path).exists()

    def load_backup_config(self):
        return load_config(self.config_path)

    def backup_status_view(self) -> dict:
        config = load_config(self.config_path)
        status = backup_status(config, self.config_path)
        status.next_due_local = next_scheduled_instant(
            config.schedule, datetime.now().astimezone()
        ).strftime("%A %Y-%m-%d at %H:%M")
        return {
            "state": status.state,
            "headline": status.headline,
            "message": status.message,
            "next_action": status.next_action,
            "store": status.store,
            "sources": status.sources,
            "snapshots_in_store": status.snapshots_in_store,
            "last_run_utc": status.last_run_utc,
            "last_outcome": status.last_outcome,
            "next_due_local": status.next_due_local,
            "free_bytes": status.free_bytes,
            "free_text": (
                human_bytes(status.free_bytes) if status.free_bytes is not None else ""
            ),
            "estimate_bytes": status.estimate_bytes,
            "estimate_text": (
                f"about {human_bytes(status.estimate_bytes)} (an estimate)"
                if status.estimate_bytes is not None
                else ""
            ),
            "history_damaged_lines": status.history_damaged_lines,
            "text": status.to_text(),
            "status": status,
        }

    def backup_setup(
        self,
        store_path,
        sources,
        *,
        every: str = "daily",
        weekday: str = "monday",
        at: str = "02:00",
        keep_mode: str = RETENTION_COUNT,
        keep_count: int = 10,
        keep_days: int = 30,
        verify_every_runs: int = 7,
        register: bool = False,
    ) -> dict:
        """Set a backup up, step for step as the CLI does."""
        config_path = self.config_path
        store_dir = Path(store_path)
        resolved_sources: list[str] = []
        for source in sources or []:
            source_path = Path(source)
            if not source_path.exists() and not source_path.is_symlink():
                raise SourceNotFoundError(
                    f"'{source}' is not there, so it cannot be backed "
                    "up. Check the name, and that the drive it lives on "
                    "is plugged in, then set the backup up again."
                )
            resolved_sources.append(str(source_path.resolve()))
        if keep_mode == RETENTION_DAYS:
            retention = RetentionSpec(mode=RETENTION_DAYS, count=10, days=keep_days)
        else:
            retention = RetentionSpec(mode=RETENTION_COUNT, count=keep_count, days=30)
        schedule = ScheduleSpec(every=every, at=at, weekday=weekday)
        config = BackupConfig(
            store=str(store_dir.resolve()),
            sources=resolved_sources,
            schedule=schedule,
            retention=retention,
            verify_every_runs=verify_every_runs,
            created_utc=format_utc(utc_now()),
            tool_version=__version__,
        )
        validate_config(config)
        if not store_dir.exists():
            store = SnapshotStore.create(store_dir)
        elif (store_dir / "store.json").exists():
            store = SnapshotStore.open(store_dir)
        elif store_dir.is_dir():
            try:
                is_empty = not any(store_dir.iterdir())
            except OSError:
                is_empty = False
            if is_empty:
                store = SnapshotStore.create(store_dir)
            else:
                raise StoreError(
                    f"'{store_dir}' is not a Lazarus store, and it "
                    "already holds other files. Pick an empty folder "
                    "or a new folder for the backup store, so backups "
                    "never mix with other files."
                )
        else:
            raise StoreError(
                f"'{store_dir}' is a file, not a folder. "
                "Pick a folder for the backup store."
            )
        config.store = str(store.root)
        save_config(config, config_path)
        # New settings put any tidying plan the owner was shown
        # out of date: it was worked out under the old rule.
        self._stored_prune_plan = None
        lines = [
            "Backup is set up.",
            f"Backup store: {config.store}",
            "What will be backed up:",
            *[f"  - {source}" for source in config.sources],
            f"Settings saved to: {config_path}",
            "Next step: run the first backup now.",
        ]
        if register:
            lines.append(
                register_schedule(config, config_path, self.program)
            )
        return {"config": config, "lines": lines, "store_path": config.store}

    def run_backup_now(self) -> dict:
        config = load_config(self.config_path)
        result = run_backup(config, self.config_path, trigger=TRIGGER_MANUAL)
        # A finished run changes what is in the store, so any
        # tidying plan the owner was shown no longer matches it.
        self._stored_prune_plan = None
        return {
            "outcome": result.outcome,
            "state": result.state,
            "message": result.message,
            "text": result.to_text(),
            "lines": result.to_text().splitlines(),
            "result": result,
        }

    def backup_history_view(self, limit: int = 10) -> dict:
        history = load_history(history_path_for(self.config_path))
        entries = []
        for entry in list(reversed(history.entries))[:limit]:
            entries.append(
                {
                    "started_utc": entry.started_utc or "unknown time",
                    "outcome": entry.outcome,
                    "outcome_words": _OUTCOME_WORDS.get(entry.outcome, entry.outcome),
                    "trigger": entry.trigger,
                    "trigger_words": _TRIGGER_WORDS.get(entry.trigger, entry.trigger),
                    "files_saved": entry.files_saved,
                    "bytes_saved": entry.bytes_saved,
                    "size_text": human_bytes(entry.bytes_saved),
                    "snapshots": list(entry.snapshots),
                    "pruned": list(entry.pruned),
                    "verified": entry.verified,
                    "verify_problems": entry.verify_problems,
                    "reason": entry.reason,
                    "notes": list(entry.notes),
                }
            )
        return {"entries": entries, "damaged_lines": history.damaged_lines}

    def _prune_plan(self):
        config = load_config(self.config_path)
        store_path = Path(config.store)
        if not store_path.is_dir():
            raise BackupConfigError(
                "The backup drive is not connected: there is no "
                f"folder at '{config.store}'. Plug the drive in and "
                "try again."
            )
        store = SnapshotStore.open(store_path)
        history = load_history(history_path_for(self.config_path))
        plan = plan_retention(
            store, backup_snapshot_ids(history), config.retention, utc_now()
        )
        return config, store, plan

    def prune_plan_view(self) -> dict:
        config, store, plan = self._prune_plan()
        # Keep the exact plan that was shown: prune_apply applies
        # this plan, and no other, so what the owner confirmed is
        # exactly what gets removed.
        self._stored_prune_plan = (config, store, plan)
        return {
            "remove": [
                {"id": item.id, "created_utc": item.created_utc, "reason": item.reason}
                for item in plan.remove
            ],
            "held": [
                {"id": held_id, "reason": reason} for held_id, reason in plan.held
            ],
            "keep_ids": list(plan.keep_ids),
            "plan": plan,
        }

    def prune_apply(self) -> dict:
        stored = self._stored_prune_plan
        if stored is None:
            raise LazarusError(
                "Look at what would be tidied up first, so you can "
                "see exactly what will be removed."
            )
        # A plan is single-use: it is spent by this apply, whether
        # or not it had anything in it.
        self._stored_prune_plan = None
        config, store, plan = stored
        if not plan.remove:
            return {
                "removed_ids": [],
                "objects_removed": 0,
                "bytes_freed": 0,
                "size_text": human_bytes(0),
                "gc_note": "",
                "lines": [
                    "Nothing to tidy up: every backup in the store is "
                    "inside the keeping rule you chose."
                ],
            }
        lock_path, stale_note = _acquire_run_lock(
            store.root, new_snapshot_id(utc_now())
        )
        try:
            result = apply_retention(store, plan)
        finally:
            _release_run_lock(lock_path)
        lines = []
        if stale_note:
            lines.append(stale_note)
        lines.append(f"Removed {len(result.removed_ids)} old backup(s).")
        lines.append(f"Saved copies removed: {result.objects_removed}")
        lines.append(f"Space freed: {human_bytes(result.bytes_freed)}")
        if result.gc_note:
            lines.append(result.gc_note)
        return {
            "removed_ids": list(result.removed_ids),
            "objects_removed": result.objects_removed,
            "bytes_freed": result.bytes_freed,
            "size_text": human_bytes(result.bytes_freed),
            "gc_note": result.gc_note,
            "lines": lines,
        }

    def register(self) -> str:
        config = load_config(self.config_path)
        return register_schedule(config, self.config_path, self.program)

    def unregister(self) -> str:
        return unregister_schedule()
