"""What are my options for this drive or folder, in plain language.

An assessment is the honest menu, not a sales pitch. It looks at two
kinds of evidence: what the platform itself says about a volume (is
it locked, and with what), and what signs a folder carries (the
marks a known ransomware family leaves behind). For each it lays out
every legitimate way back Lazarus knows — the owner's own key, a
published decryptor run on copies, a snapshot, an earlier copy of
the file — and it says the hard part out loud too: Lazarus cannot
break encryption, and neither can any honest tool.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .decrypt_model import DecryptorEntry
from .decryptors import identify_family, load_registry
from .errors import DecryptorError, UnlockError

_HONESTY_LINE = (
    "Lazarus cannot break encryption, and neither can any honest "
    "tool: without the key, locked files stay locked. Its job is "
    "every legitimate way back — your own key, a published decryptor, "
    "a backup, or an earlier copy of the file."
)


@dataclass
class Assessment:
    """One target, its findings, and the honest options for it."""

    target: str
    findings: list[str] = field(default_factory=list)
    options: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_text(self) -> str:
        lines: list[str] = [f"What Lazarus can do for {self.target}"]
        if self.findings:
            lines.append("")
            lines.append("What Lazarus found:")
            for finding in self.findings:
                lines.append(f"  - {finding}")
        if self.options:
            lines.append("")
            lines.append("Your options:")
            for option in self.options:
                lines.append(f"  - {option}")
        if self.notes:
            lines.append("")
            lines.append("Worth knowing:")
            for note in self.notes:
                lines.append(f"  - {note}")
        return "\n".join(lines)


def assess_options(
    target: Path,
    *,
    platform=None,
    registry: Optional[list[DecryptorEntry]] = None,
) -> Assessment:
    """Assess one volume or folder and return the honest options.

    ``platform`` is the volume seam (see :mod:`lazarus.unlock`); when
    it is given, the target is first asked about as a volume. A seam
    that cannot answer here leaves a note, never a crash — the folder
    evidence is still weighed. ``registry`` defaults to the maintained
    decryptor list.
    """
    target = Path(target)
    if registry is None:
        registry = load_registry()

    findings: list[str] = []
    options: list[str] = []
    notes: list[str] = []

    volume_found = False
    if platform is not None:
        volume = None
        try:
            volume = platform.describe_volume(str(target))
        except UnlockError as error:
            # A designed seam message, in plain language — it becomes
            # a note, and the rest of the assessment carries on.
            notes.append(str(error))
        if volume is not None:
            volume_found = True
            kind = (volume.kind or "").strip().lower()
            if kind == "bitlocker":
                if volume.locked is True:
                    findings.append("This volume is locked with BitLocker.")
                    options.append(
                        "Open it with your own recovery key — "
                        f"lazarus unlock {volume.ref} — then recover "
                        "its files like any other drive's. Your drives, "
                        "your keys: Lazarus opens a lock only with a "
                        "key you hold."
                    )
                elif volume.locked is False:
                    findings.append("This volume is open.")
                    options.append(
                        "Recover from it normally: lazarus scan / "
                        "lazarus recover for deleted files, or restore "
                        "from a snapshot."
                    )
                else:
                    findings.append(
                        "This volume uses BitLocker, but Lazarus could "
                        "not tell whether it is locked, so it will not "
                        "guess. If it asks for a recovery key when you "
                        "open it, lazarus unlock can open it with your "
                        "own key."
                    )
            else:
                findings.append(
                    "Lazarus could not name the lock on this volume, "
                    "and it does not guess at locks. It can open a "
                    "BitLocker volume with your own key; other locks "
                    "are outside what it does."
                )

    is_folder = target.is_dir()
    if is_folder:
        identification = identify_family(target, registry)
        notes.extend(identification.notes)
        for match in identification.matches:
            entry = match.entry
            lead = (
                "This looks like"
                if match.confidence == "looks like"
                else "This may be"
            )
            evidence = " ".join(match.evidence)
            if evidence:
                findings.append(f"{lead} {entry.family} — {evidence}")
            else:
                findings.append(f"{lead} {entry.family}.")
            options.append(
                f"A free decryptor for {entry.family} is published by "
                f"{entry.publisher}: {entry.decryptor_name}. Lazarus "
                "can run it on copies of these files — never on the "
                "only copies (lazarus decrypt). Its honest limits: "
                f"{entry.conditions}"
            )
        if not identification.matches:
            findings.append(
                "Nothing here carries signs Lazarus recognizes as a "
                "known ransomware family's."
            )
            options.append(
                "If these files were locked by ransomware Lazarus "
                "does not know, there is no published decryptor "
                "Lazarus can offer for them, and no honest tool can "
                "break that encryption."
            )

    if not volume_found and not is_folder:
        raise DecryptorError(
            f"Lazarus could not find {target} as a volume or a "
            "folder. Check the name and try again."
        )

    # The standing alternatives, each once: a way already named above
    # is not repeated just to make the list look longer.
    if not any("lazarus restore" in option for option in options):
        options.append(
            "If a snapshot of these files exists, bring them back "
            "from it: lazarus list, then lazarus restore."
        )
    if not any("lazarus scan" in option for option in options):
        options.append(
            "Look for deleted earlier copies of the files: lazarus "
            "scan, then lazarus recover onto a different drive."
        )
    if is_folder and not any("lazarus rescue" in option for option in options):
        options.append(
            "If the drive itself is failing, rescue it before "
            "anything else: lazarus rescue."
        )

    notes.append(_HONESTY_LINE)
    return Assessment(
        target=str(target), findings=findings, options=options, notes=notes
    )
