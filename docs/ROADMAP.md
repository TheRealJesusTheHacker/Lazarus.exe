# LAZARUS.EXE — Build Roadmap

The resurrection chart. Phases run in order, and the order is the
point: **restoration is designed first** — the snapshot store exists
to serve the restore engine, never the other way around.

**The three jobs (fixed priority):**
Restoration → Decrypt & unlock → Backup automation.

**Safety law:** recovery never writes to the drive it is recovering
from, odds are stated before work begins, and Lazarus unlocks only
with a key the owner holds. No exceptions, no settings that weaken
them.

---

## Phase 1 — Versioned snapshot store + restore engine (THE FLAGSHIP)

The key phase. Lazarus's reason to exist.

- **Snapshot store design, restore-first:** dated, versioned copies
  of files and folders on a target drive the user owns. Every design
  question is answered from the restore side: "can we get Tuesday's
  version of this folder back, exactly as it was?"
- **Point-in-time restore:** restore a single file, a folder, or
  everything, rolled back to a chosen date/version.
- **Restore is itself reversible:** before a restore overwrites
  anything, the current state is snapshotted — an undo for the undo.
- **Integrity verification:** restored data is verified against the
  snapshot (hashes), and the restore report says plainly what came
  back whole, what came back partial, and what could not come back.
- **Plain-language restore reports** — designed failure states, no
  raw OS errors ever surfaced.

**Status: COMPLETE — shipped in v0.2.0.**

As built: a content-addressed snapshot store (file contents kept
once, named by SHA-256; per-snapshot manifests written atomically,
so an interrupted save reads as unfinished, never as trustworthy),
and a restore engine that picks a snapshot by name or by date,
re-hashes every saved copy it needs *before* writing anything,
snapshots the current state of whatever it is about to overwrite
(the undo — and if that safety copy cannot be completed, the
restore stops before changing a byte), writes each file to a
temporary name and hash-checks it before it lands, and finishes
with the plain-language report. The safety rules live in the
engine, not the interface: a restore never writes outside its
destination (doctored records, backslash paths, duplicate entries,
and shortcut write-throughs are all refused), the store never
snapshots itself, and restores never delete newer work. Proven by
a 63-test suite and an independent audit round — 17 findings, all
fixed and re-probed. A command-line interface ships now; the
animated HUD is Phase 5. Verified on Linux; the first
Windows-hardware run is still to come.

## Phase 2 — Undelete + drive rescue

- **Undelete:** scan a drive for deleted files and recover them to a
  *different* drive. Per-file **honest recovery odds** computed from
  the evidence — filesystem, overwrite state, time since deletion —
  shown before recovery starts. An overwritten file is reported as
  gone, plainly. Odds are never dressed up.
- **Failing-drive rescue:** image-first workflow — clone/salvage the
  drive to an image with gentle, error-tolerant reads (skip bad
  areas, come back for them last), then run recovery against the
  image, not the dying hardware.
- Read-only against the source, always — enforced by the same kind
  of test-pinned safety assertions the rest of the medic kit uses.

**Status: COMPLETE — shipped in v0.3.0.**

As built: two filesystem parsers behind one odds engine. The FAT
parser walks every folder of a FAT12/16/32 drive and gathers the
entries deletion marked, saying openly that the stretch of pieces
it reports is the usual unbroken-run assumption — deletion erased
the map that would prove it — with each piece judged by whether
the table now counts it as back in use. The NTFS parser walks the
Master File Table record by record, applying each record's own
fixup before trusting it; small files come back with the contents
that live inside their record, and larger files are reported as
the stretches they sat in, split by the drive's own in-use map
($BITMAP). The odds engine weighs only that evidence — likely
whole, partly there (damage stated in bytes), or gone — and counts
anything the drive cannot testify about as at risk, never as safe.
Recovery writes only to a folder on a different device than the
source's — the same-device refusal fires before a byte is written
— flattens names from drive records into safe single file names,
and hashes everything it writes. The rescue engine clones through
a progress map of regions and holes: first pass one read per
region, later passes in smaller pieces, the map saved before the
first read and after every region, resumable across runs, and
refusing — in plain language — corrupt maps and any placement of
the progress file on the image or the source. Every source is
opened read-only through a single seam with no write path.
Proven by a 79-test Phase 2 suite (142 in all) including a
hostile-input audit battery kept as permanent tests.

Filesystem coverage, proven vs pending — proven means the parser
is exercised end-to-end against ground-truth images built
independently of it:

| Filesystem | State | How it is proven |
|---|---|---|
| FAT12 / FAT16 / FAT32 | Proven | Independent spec-built images: intact, partly reused, and fully reused deletions; long names; subfolders; erased start records |
| NTFS (MFT-record scan) | Proven | Independent spec-built images: resident and non-resident files, $BITMAP split verdicts, fixup-damaged records skipped and counted |
| NTFS folder-index / journal recovery | Pending | Records the MFT scan cannot see are out of reach until a later phase walks indexes and the journal |
| ext2 / ext4 and others | Pending | Not yet read; such a drive is refused in plain language, never guessed at |

Verified on Linux against synthetic images and synthetic failing
drives. The first run against real Windows hardware — a real
failing drive, real deleted NTFS system volumes — is still to
come, and nothing above claims otherwise.

## Phase 3 — Decrypt & unlock files off drives

- **Owner-key volume unlock:** open encrypted volumes — BitLocker
  first — using the owner's own recovery key or password, through
  Windows' own unlock mechanisms, then recover files normally.
  Owner-only framing throughout: your drives, your keys.
- **Published ransomware decryptors:** for families where security
  researchers have published working decryptors, integrate them from
  a maintained, sourced list (each decryptor credited to its
  publisher), and run them against copies of the encrypted files —
  never the only copy.
- **Honest odds up front:** family identified → decryptor exists or
  it doesn't. If no published decryptor exists, Lazarus says so
  plainly and offers what *is* possible (restore from backup,
  undelete, salvage). It never implies it can break encryption —
  because it can't, and neither can anyone else honestly selling
  that promise.

**Status: COMPLETE (v0.4.0, 2026-10-20).**

**As built.** `lazarus volumes` and `lazarus unlock E:` drive
BitLocker through Windows' own `manage-bde`, behind a platform
seam. Commands are built with a placeholder where the key
belongs; the key is substituted only into the executed copy, in
memory, on Windows. The key is asked for once — typed at a
prompt that does not echo, never taken as a command argument —
tried exactly once, and never stored, shown, logged, or
retried. A volume that cannot be positively identified as
BitLocker from Windows' own status fields is refused plainly.
On any other system the same commands refuse plainly rather
than pretending. What is proven where: command construction,
status-output reading, key handling, the one-attempt rule, and
every refusal are proven by the test suite here; **the unlock
itself — `manage-bde` accepting a key on a real Windows volume —
is seam-only until a Windows-hardware run**, and after a
successful unlock the volume is handed to the Phase 1/2 engines
like any other drive. The decryptor list
(`lazarus/decryptors_registry.json`) ships six entries, each
verified against its publisher's own page on 2026-10-20:
STOP/Djvu (Emsisoft), GandCrab (Bitdefender), Avaddon
(Emsisoft), REvil/Sodinokibi (Bitdefender; attacks before
2021-07-13 only), Babuk (Avast), Maze/Sekhmet/Egregor
(Emsisoft). The runner (`lazarus decrypt`) runs a registered
decryptor only on copies inside a Lazarus work area that it
re-verifies at run time — folder layout, symbolic and hard
links, the fingerprint record, every copy's hash, an empty
recovered-files folder — with the originals hashed before and
after, and the report separating changes that predate the run
from changes during it. `lazarus identify` answers *looks
like* / *may be* and never "confirmed", and the decrypt
command acts on its own reading only for a single *looks
like*. `lazarus assess` gives the one-screen honest answer.
Proven by a 290-test suite (1 skipped) and three independent
audit rounds (18 findings, then 11, then 4 of low severity —
all fixed and re-probed). One limit is recorded rather than
hidden: the work area is verified immediately before the run,
and no check can freeze a disk — a change made in the moment
between verification and running is caught by the report's
before/after check on the originals, not prevented.

## Phase 4 — Backup automation

- **One-click backup** of the folders that matter, to an external or
  second drive, into the Phase 1 versioned store.
- **Scheduled backups** (daily/weekly, user's choice) with the same
  dated versioning — a backup run *is* a new restore point.
- **Local-first:** no cloud account required, ever. (A user pointing
  the target at a synced folder is their business; Lazarus itself
  phones nowhere.)
- **Loud, honest drive status:** target drive unplugged, full, or
  failing is surfaced *before* it matters — clear status in the app,
  not a silent log line after the disaster.

**Status: COMPLETE — shipped in v0.5.0.**

**As built.** Backup automation sits on the Phase 1 store
unchanged in spirit: a backup run is one `create_snapshot` per
configured folder, so every restore point a backup makes is an
ordinary snapshot the Phase 1 restore engine already knows how
to bring back. Three new modules carry it. `lazarus/backup.py`
(with `lazarus/backup_model.py`) is the engine: configuration,
the run itself, pre-flight, status, and the run history.
`lazarus/retention.py` plans and applies keep-N / keep-days
tidy-ups. `lazarus/schedule.py` (with the pure command builders
in `lazarus/schedule_commands.py`) is the scheduling layer,
following the same pure-builder + platform-seam pattern Phase 3
used for `manage-bde`. The CLI grows one command group:
`lazarus backup setup / run / due / status / history / prune /
register / unregister`, with honest exit codes (0 complete or
ready, 1 partial/failed/overdue, 2 refused or a setup error).

Configuration lives at `~/.lazarus/backup.json` and the history
beside it at `~/.lazarus/backup-history.jsonl` — append-only,
one JSON line per attempt, fsynced, with damaged lines counted
and skipped rather than trusted or fatal. Config is validated
strictly and semantically before anything is saved: paths must
be full paths (a relative path is refused in plain language,
and setup resolves every folder to its full path before
saving — a scheduled task runs from an unrelated working
folder, and a relative path there would silently back up a
same-named stranger; this exact failure was found in audit and
is pinned by an end-to-end test that sets up in one folder,
runs from another holding a decoy, and byte-compares the
restore against the original). Retention is capped at
1–10,000 snapshots or 1–36,500 days so a hand-edited config
cannot crash the arithmetic.

A run takes a lock (`<store>/backup-run.lock`, holding pid,
start time, and run id; released in a `finally`), then
pre-flights in a fixed order: target present → store opens
(a store that will not open is reported damaged, with what to
do next) → no other backup running → store writable → free
space against the estimate → integrity spot-check when due →
one snapshot per source → retention, only after a fully
complete run. Every attempt — including pre-flight refusals —
appends exactly one history entry. A source that is missing or
fails makes the run partial, not dead, unless nothing could be
saved. Bytes copied are measured by the store's object-area
diff, so the history's numbers are the store's own. The size
estimate is honest about its basis: the last run's copied bytes
(plus headroom), labelled an estimate, and when the last run
copied nothing it carries the caveat that changed files may
need more room than the estimate says.

Status and runs share **one** definition of "a spot-check is
due": runs since the last *passed* check, plus the run about to
happen, against the configured cadence. A failed check
therefore leaves the check due — the store is re-checked on
every later run and status query until one passes, so status
can never settle back to READY over known damage. History
entries dated in the future (a forged line, or one honest run
made while the system clock was wrong) are ignored by the
cadence, the last-run derivation, and the scheduler's due math,
with a five-minute tolerance for ordinary clock wobble. Status
states: READY / TARGET MISSING / TARGET FULL (with both
numbers) / STORE DAMAGED (with the next action) / NEVER RUN /
OVERDUE — plus UNFINISHED BACKUP, added by the audit: when the
only thing wrong is the leftover of a backup cut off mid-save,
the headline says so and names the remedy (`lazarus backup
prune`, which sweeps unfinished leftovers once they are a day
old, whoever started them) instead of calling the store
damaged and offering no way out.

Scheduling: the registration target is Windows Task Scheduler,
task name `LAZARUS.EXE Backup`. The `schtasks.exe` commands
(daily, and weekly with the weekday mapped to `MON`…`SUN`;
create/delete/query/run) are pure builders, unit-tested, and
executed only through the `SchedulerPlatform` seam — on this
machine the platform is honestly unavailable and `register`
refuses in plain language; nothing was registered here. The due
engine is fully real: given a moment, it finds the last
scheduled instant at or before it, asks the history for the
last run that actually produced snapshots at or after that
instant, and runs at most one backup — trigger *scheduled*
within fifteen minutes of the instant, *catch-up* after that.
One run satisfies one missed instant; a machine off for a week
gets one catch-up, not seven. Schedule times are read in the
local timezone of the moment supplied and converted to UTC at
the backup boundary.

Retention safety, as audited: the newest finished backup
snapshot is never a candidate; pre-restore safety copies are
never candidates; a finished snapshot whose date cannot be
parsed is held back and reported ("left alone"), in both modes;
pruning never runs during or after a failed run; a dry run
leaves the store byte-identical and its plan matches apply
exactly. Object tidy-up never follows a symlinked fan-out
folder out of the store, skips anything that is not a real
directory, fails closed on unreadable manifests, keeps objects
referenced by *any* snapshot kind, and gives unreferenced
objects a 24-hour grace before sweeping — an object written by
a snapshot still in progress is never swept from under it.
Hand-built plans with bad snapshot ids are skipped and
reported, never raised and never acted on outside the store.

**Audit.** One independent adversarial round: 16 findings
(F-01…F-16). Fixed in code and pinned by permanent tests
(`tests/test_phase4_audit.py`, 24 tests): F-01 (object sweep
followed a symlinked fan-out folder outside the store),
F-02 (relative config paths — the scheduled-task wrong-folder
backup), F-03 (a manifest whose inner id did not match its
folder could crash runs, blind verification, or become
immortal to retention), F-04 (a lock file naming an enormous
pid crashed every run with no history entry), F-05 (unbounded
retention days overflowed date arithmetic, after copying),
F-06 (an undateable snapshot sorted as oldest and could be
pruned first), F-07 (one future-dated history line suppressed
checks, overdue, and due runs), F-08 (`backup setup` validated
nothing), F-09 (an unfinished snapshot read as store damage
with no product escape — the deadlock above), F-10 (object
sweep had no in-flight grace), F-15 (retention apply trusted a
hand-built plan), and the F-14 items worth fixing
(`history --limit 0` silently blank; the zero-copied estimate
caveat). Documented as accepted residuals: F-11 (lock
staleness trusts pid liveness alone — a recycled pid can block
backups; it fails safe, and the refusal now names the lock file
and the delete-it remedy), F-12 (refused runs count toward the
verify cadence — checks arrive earlier, never later; accepted
as conservative), F-13 (the `schtasks /TR` string does not
escape embedded quotes in owner-supplied paths — quotes are
illegal in Windows filenames and registration is idempotent),
the remaining F-14 cosmetics (prune announces its plan before
taking the lock; a store path that is a regular file is
reported as a missing target rather than not-a-folder;
duplicate folders in one config make duplicate snapshots
without a note), and F-16 (benign hostilities: duplicate JSON
keys are last-wins, a UTF-8 BOM config is accepted, a history
entry with `seconds: NaN` loads). The test suite also caught a
real cadence bug before the audit — the spot-check counter was
off by one and a failed check reset it — fixed so runs and
status share the one due rule above; and one retention test's
premise (a just-written unreferenced object is swept
immediately) was deliberately retired because F-10's grace is
the safer behavior — the test now backdates the object and
keeps every original assertion. Suite at ship: **411 tests
passing, 2 skipped** — 408 from Phase 4's work plus 3 from a
fleet-wide rescue fix (binary-mode writes) that landed on main
while this phase was in build and is included in this tree
(one pre-existing skip; one writability test that cannot bite
when the suite runs as root).

**Seam-only until a Windows-hardware run** (nothing here claims
otherwise): `schtasks` accepting the built registration
commands on a real machine; the registered task firing
`lazarus backup due` from the Task Scheduler working folder and
hitting the configured folders; a missed run catching up after
the machine was off; local-time/DST behavior around a schedule
boundary; setup/run/status/history/prune/register/unregister
as a standard user; the stored command line with spaces in the
program and config paths; status spot-check timing against a
large real store; and the full suite re-run on Windows.

## Phase 5 — The animated UI + packaging

- Futuristic animated HUD interface (PySide6), green on black, on the
  real pipeline — every control functional, no decorative buttons.
  Restore flows lead the design; recovery odds and drive status are
  first-class screens, not fine print.
- Packaging: the single self-installing exe pattern the medic kit
  standardised on (one file, one admin prompt, verified install).
- **Portable edition (named deliverable):** a portable no-install
  edition of Lazarus — runs from a folder or USB drive with no
  installation, following the Argus Portable pattern
  (ArgusPortable.zip) — ships alongside the installer at Phase 5.
  It is a committed Phase 5 deliverable, not a backlog option, and
  is part of the Technician's Toolkit USB set.
- Every failure state a designed screen with one clear next action,
  in plain language.

**Status: COMPLETE — v1.0.0, the first public release.**

As built: the window is a PySide6 interface in Sentinel green on
black — five pages (Restore, Snapshots, Deleted files, Locked
files, Backup) over a Qt-free controller, so every control calls
the real Phase 1–4 engine and the core suite runs with no Qt
loaded. Restore leads, as planned; the odds and the backup status
are first-class screens. Long jobs run on one guarded worker with
real progress (byte counters and the rescue progress map polled
live — never invented percentages); quitting mid-job waits for
the job to finish rather than tearing it down. Twenty-one
interface states were rendered and eyeballed, and every
text/background pairing was computed and verified on the renders
at 4.5:1 or better. The render review itself caught a real defect
— dialog text squeezed past its label under offscreen size hints —
fixed by sizing dialogs to their contents and pinned with a test.
Packaging is the medic-kit pattern: one self-installing
Lazarus.exe (Program Files copy + Start Menu entry, one approval
prompt, every step verified and reported; removing it is one
click in a portable copy), LazarusPortable.zip alongside it, and
a tag-gated release workflow that runs the whole suite on
Windows, launches the staged program's self-check, and publishes
with SHA256SUMS.txt. An independent audit of the new surface (56
attack probes) found 16 items, five of them serious; all were
fixed and regression-pinned, and the re-audit caught one more
serious item the first pass missed — a shortcut (symlink)
pointing at a pipe slipped past the pipe check — which was
fixed and pinned the same way. A final re-audit verified every
fix and found no regressions; the release proof build then
caught one last interaction — the packaged program's start-up
self-check waiting on the mid-job close dialog — which was
fixed, pinned, and proven on a rebuilt packaged program before
release.

Three honest residuals, recorded rather than hidden: (1) pointing
the window at a pipe or socket as a scan/rescue/snapshot source
— including through a shortcut that points at one — is refused
by the window up front in plain language, but the
command line can still be made to wait on one — the engine-level
refusal is future engine work, not claimed here. (2) The backup
status's "what to do next" line for a damaged store still names
the command-line check, because the window shows the engine's
own words verbatim; wording parity there is future engine-copy
work. (3) A rescue destination that is a shortcut loop (a
symlink pointing at itself) reaches the engine's general apology
rather than a specific message — a pre-existing engine
disposition, recorded for a future engine pass; nothing hangs
and nothing is written.

**Seam-only until a Windows-hardware run** (nothing here claims
otherwise — the release workflow's Windows run of the suite
proves the suite on Windows, not these): the first interactive
run of the window on a real display, including display scaling,
with each flow started for real; self-install end to end — the
offer, the approval prompt, the Program Files copy and Start
Menu entry verified on disk, the installed copy launched, and
removal from a portable copy; the portable edition run from a
real USB drive; manage-bde accepting an owner's recovery key on
a real locked volume (Phase 3); `schtasks` accepting the built
registration commands on a real machine, the registered task
firing `lazarus backup due` from the Task Scheduler working
folder and hitting the configured folders, a missed run catching
up after the machine was off, local-time/DST behavior around a
schedule boundary, the backup commands as a standard user, the
stored command line with spaces in the program and config
paths, and status spot-check timing against a large real store
(Phase 4); and, from Phases 1–2, locked-file restore wording on
a real restore, paths longer than 260 characters, multi-GB and
disk-full restores, cross-volume restore, a power cut
mid-restore on NTFS, Unicode filenames, and real deleted files
on real NTFS volumes and a real failing drive.

## Later / backlog

- Technician's Toolkit USB bundling — the Phase 5 portable edition
  (see above) packaged into the Technician's Toolkit USB set,
  alongside Argus Portable and Charon Portable, for technician
  field work.
- Whole-disk/partition restore (bare-metal recovery) — after the
  file-level engine has proven itself on hardware.
- Additional published-decryptor coverage as researchers release
  new ones.

## Standing rules

- **Honest odds, always.** Recovery and decryption chances are
  computed from evidence and stated before work begins — never
  faked, never rounded up for morale.
- **Owner keys only.** Lazarus unlocks with keys the owner holds.
  It does not defeat encryption, and its copy never implies it can.
- **Never write to the source.** Recovery, undelete, and decryption
  attempts work on copies or read from the source and write
  elsewhere.
- **Plain language.** Every user-visible word is written for the
  non-technical people Sentinel serves. No raw OS errors, ever.
- **Audit everything.** Build → audit → fix → re-audit, per
  Sentinel's standing rule across the fleet.
- Brand: green and black. No purple anywhere.
