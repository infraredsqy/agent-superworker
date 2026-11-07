"""`migrate`: move a workspace's records from `.pf/` to `.superworker/`.

The move is generation-labelled because a move is judged against the writer that laid the records
down, not guessed from the files: `0.1.0` is the one generation this tool knows, the records 0.1.x
wrote.

The reading:

- **The refusals are one reading, shared with `doctor`.** `migration_preconditions` answers "may this
  workspace's records move", and nothing else spells that judgement: both directories here, an old
  directory nobody init-ed, a project whose git tracks it, a run in flight, a parallel-mode worktree.
  Each refusal hands the job to an agent -- every one of them is a judgement or a removal this tool
  must not make -- and lands no byte.
- **The rename is the bottom line, the rest are attachments.** The directory moves first, in one
  step; every step after works on the new location and is reported as its own reading, and a step that
  does not happen is one sentence of a warning -- the same shape `init` prints, never a rollback.
- **The attachments reuse the writers `init` uses.** The exclude line goes through the same strict
  reader and line splicer (a file this tool cannot re-encode is not rewritten), the record
  directory's own `.gitattributes` and usage note are brought to their current bytes, the driver is
  re-registered for the new attribute name, the machine layer's settings are **copied** (`~/.pf`'s
  `config.json` to `~/.superworker/config.json`, never over a file already there -- an install that
  has not moved yet keeps answering where it always did), and every `plan_path` value that names the
  old directory is rewritten -- such a value is a directory the record's owner would recreate on its
  next write.
- **The records are swept to LF.** The 0.1.x writer put the platform separator in every record, so a
  migrated tree would otherwise carry CRLF into a repository whose `.gitattributes` now says the line
  endings are ours. The sweep is a byte replacement over the record files as they are: no record is
  re-parsed and re-serialized, so nothing a person wrote is reformatted.
- **The migrated tree is a record tree like any other once it has moved**: the steps that write into
  it run under its own record lock, the protocol every writer in this tree holds.
"""

import os
import re
import sys
from pathlib import Path

try:
    import fcntl
except ImportError:      # Windows
    fcntl = None
try:
    import msvcrt
except ImportError:      # POSIX
    msvcrt = None

from .initledger import (LEDGER_AGENTS_BODY, _atomic_write_bytes, exclude_line_for, read_exclude_text,
                         register_driver, repair_layout_file, resolve_exclude_path, with_exclude_line)
from .store import CONFIG_NAME, LEGACY_DIR_NAME, LEDGER_NAME, _ledger_lock, _loader, \
    _resolve_project_root, legacy_ledger_dir_for, ledger_dir_for

__all__ = ['KNOWN_GENERATIONS', 'cmd_migrate', 'migration_preconditions', 'refusal_sentence']

#: The generations this tool moves, in the order they are printed: the id a person types, and the one
#: line about what a workspace of that generation holds. A second entry lands only with its own
#: writer's reading -- a move is judged against the generation that wrote the records.
KNOWN_GENERATIONS = (
    ('0.1.0', 'the records 0.1.x wrote'),
)

#: The usage note 0.1.x wrote into the ledger. The migration replaces it only when the file is still
#: exactly that text: a note somebody has edited is theirs, and rewriting it would throw their words
#: away. Its one use is this comparison.
LEGACY_AGENTS_BODY = """\
# .pf usage note

> This file (`.pf/AGENTS.md`) must not exceed 100 lines.
> This file is written and maintained by this tool at `init` time. **To change it, ask the user first** -- once they have said "go ahead", then touch it.

This directory is the pf pipeline's ledger: `ideas.json`, `state.json`, `instructions.json` and
`signals/` are the pipeline's records, and `nodes/<uuid>/` holds the spec and the deliverable of
each slot (DAG node).

- Records are read and written only through `pf` (`pf dag …`, `pf next`); hand-editing the JSON is forbidden, and so is reading `ideas.json` raw -- use the tool's list / plan output.
- Process artifacts belonging to one slot (probes, raw output, self-check scripts) go in `.pf/nodes/<uuid>/temp/`, which is temporary and not committed: do not submit them as deliverables.
- Everything else temporary goes in `.tmp/` at the workspace root.
- `meta/`, `worktrees/` and `nodes/<id>/temp/` are kept out of the repository by `.gitignore`; do not move anything from inside them out by hand and commit it.
"""

#: The comment 0.1.x planted above its `info/exclude` pattern. The migration takes it out: the line
#: it explained is gone, so a reader of that file would be pointed at nothing.
LEGACY_EXCLUDE_MARK = '# pf working system: this ledger stays on this machine, never in the team repo'

#: A `plan_path` value may still carry the old directory's name at its start or behind a separator,
#: and nowhere else: a directory that merely ends in the old name is somebody else's, and stays.
OLD_PREFIX = re.compile(r'(^|/)' + re.escape(LEGACY_DIR_NAME) + r'/')


def _holds_worktrees(ledger: Path) -> bool:
    """Whether this record directory holds a parallel-mode worktree (its git metadata carries
    absolute paths, so it must not move under a fork's feet)."""
    worktrees = ledger / 'worktrees'
    try:
        return worktrees.is_dir() and any(worktrees.iterdir())
    except OSError:
        return False


def _lock_held(ledger: Path) -> bool:
    """Whether a 0.1.x run is in flight under this directory.

    The 0.1.0 writer took a byte-range lock on a file it never removed, so the file's **presence** is
    not the reading here: every workspace of that generation has one lying around after its first
    command. The question is whether anybody still holds a range on it, and that is asked the way
    that generation asked it -- non-blocking, then released at once. A file whose range nobody holds
    is the dead leftover this migration takes away before it starts writing.
    """
    path = ledger / 'meta' / 'ledger.lock'
    if not path.exists():
        return False
    try:
        handle = open(path, 'a+')
    except OSError:
        return True
    try:
        if os.name == 'nt' and msvcrt is not None:
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                return True
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        elif fcntl is not None:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True
            fcntl.flock(handle, fcntl.LOCK_UN)
        return False
    finally:
        handle.close()


def _clear_dead_lock(record: Path) -> bool:
    """Take away the lock file the old generation left behind, now that the move is under way.

    It sits exactly where this tree's own lock goes, and the protocol now reads an existing file as
    "someone is writing": leaving it would make every later write in the migrated workspace wait out
    the timeout. A live holder was refused by the preconditions, so what is left here is dead.
    """
    try:
        (record / 'meta' / 'ledger.lock').unlink()
    except FileNotFoundError:
        return False
    return True


def _tracked_by_project_git(project: Path) -> bool:
    """Whether the project's git tracks the old record directory (renaming it would half-remove it
    from the repository's history). A git that cannot answer is read as "not tracked" -- `init`'s
    own tracked-directory refusal then catches the mistake before a byte lands."""
    gitcmd = _loader().load_shared('gitcmd')
    try:
        listing = gitcmd.git(project, 'ls-files', '--', LEGACY_DIR_NAME)
    except OSError:
        return False
    return listing.returncode == 0 and listing.stdout.strip() != ''


def migration_preconditions(project) -> tuple:
    """The one reading of "may this workspace's records be moved": `(refusal, legacy_dir, record_dir)`.

    `refusal` is None when the move may run, and otherwise names the one fact that stops it. The order
    is the order a person needs: what is here, whether it is ours, whether git or a running process is
    holding it. `absent` is the answer a migrated workspace gets, and the one a workspace with no
    records at all gets -- it is not a refusal in the sense of the other five.
    """
    root = Path(project)
    legacy = legacy_ledger_dir_for(root)
    record = ledger_dir_for(root)
    if not legacy.is_dir():
        return 'absent', legacy, record
    markers = _loader().load_shared('ledger').INIT_MARKERS
    if not all((legacy / marker).is_file() for marker in markers):
        return 'unrecognizable', legacy, record
    if record.exists():
        return 'both-present', legacy, record
    if _tracked_by_project_git(root):
        return 'tracked-by-git', legacy, record
    if _lock_held(legacy):
        return 'locked', legacy, record
    if _holds_worktrees(legacy):
        return 'worktrees', legacy, record
    return None, legacy, record


def refusal_sentence(project, refusal: str) -> str:
    """The one sentence a refused migration prints: the fact that stopped it, and where to take it.

    Each one is a judgement or a removal this tool must not make, so the sentence hands the job to an
    agent rather than to a hand edit -- talking a person into editing a record tree by hand is how one
    gets hurt.
    """
    root = Path(project)
    old = legacy_ledger_dir_for(root)
    new = ledger_dir_for(root)
    fact = {
        'both-present': f'both {old} and {new} are here, and which records are the true ones is a '
                        f'judgement no command can make',
        'unrecognizable': f'{old} is here but does not carry the marks an init lays down, so it may '
                          f'not be a record directory of this tool at all',
        'tracked-by-git': f'{old} is tracked by this project\'s git, and renaming it would half-remove '
                          f'it from the repository\'s history',
        'locked': f'{old} holds the record lock, so a run is in flight and the records must not move '
                  f'under it',
        'worktrees': f'{old} holds a parallel-mode worktree, and a worktree\'s git metadata carries '
                     f'absolute paths',
    }.get(refusal, 'there is nothing to move here')
    return f'{fact} -- consult an agent rather than editing anything by hand; nothing was moved.'


# ── the attachments ─────────────────────────────────────────────────────────

def _move_exclude_line(project: Path, record: Path) -> str:
    """Switch the project repository's `info/exclude` to the new directory: the old line out, the new
    line in. The reader, the splicer and the write are `init`'s own."""
    exclude_path = resolve_exclude_path(project)
    if exclude_path is None:
        return 'skipped-not-a-checkout'
    new_line = exclude_line_for(project)
    if new_line is None:
        return 'skipped-not-a-checkout'
    old_line = exclude_line_for(project, LEGACY_DIR_NAME)
    try:
        current = read_exclude_text(exclude_path) or ''
    except (OSError, UnicodeDecodeError) as failure:
        print(f'  Warning: {exclude_path} could not be read ({failure}), so it was not rewritten; '
              f'the old ignore line is still there.', file=sys.stderr)
        return 'failed'
    text, _ = with_exclude_line(current, f'/{LEGACY_DIR_NAME}/', False)
    if old_line is not None:
        text, _ = with_exclude_line(text, old_line, False)
    # 0.1.x planted a comment line above the pattern; it is this tool's own, and the line it explains
    # is gone now -- leaving it would point a reader at a directory no longer kept out.
    text, _ = with_exclude_line(text, LEGACY_EXCLUDE_MARK, False)
    next_text, _ = with_exclude_line(text, new_line, True)
    if next_text == current:
        return 'written'
    try:
        exclude_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_bytes(exclude_path, next_text.encode('utf-8'))
    except OSError as failure:
        print(f'  Warning: {exclude_path} could not be written ({failure}); the old ignore line may '
              f'still be there and the new one may be missing.', file=sys.stderr)
        return 'failed'
    return 'written'


def _repair_usage_note(record: Path) -> str:
    """Bring the ledger's usage note to its current bytes, when the file is still the one 0.1.x wrote.

    A note somebody has edited is theirs: it is compared against that exact text first, and anything
    else is left alone -- the tool owns the note it wrote, not every note that ever sat at that path.
    The comparison ignores the endings, because 0.1.x wrote the platform's: the same note is the same
    note whichever separator landed in it.
    """
    note = record / 'AGENTS.md'
    try:
        current = note.read_bytes()
    except OSError:
        return 'skipped'
    if current.replace(b'\r\n', b'\n') != LEGACY_AGENTS_BODY.encode('utf-8'):
        return 'skipped-edited'
    _atomic_write_bytes(note, LEDGER_AGENTS_BODY.encode('utf-8'))
    return 'rewritten'


def _copy_machine_layer(home) -> str:
    """Copy the machine layer's settings to the new home: copied, never moved, never over a file that
    is already there. The landing is an exclusive create, so two runs racing on one target cannot both
    write it."""
    home_dir = Path(home).expanduser() if home else Path(
        os.environ.get('SUPERWORKER_HOME_DIR') or Path.home()).expanduser()
    source = home_dir / LEGACY_DIR_NAME / CONFIG_NAME
    target = home_dir / LEDGER_NAME / CONFIG_NAME
    if target.is_file():
        return 'skipped-target-present'
    if not source.is_file():
        return 'skipped-no-source'
    try:
        payload = source.read_bytes()
    except OSError as failure:
        print(f'  Warning: the machine layer\'s settings were not copied: {source} could not be read '
              f'({failure}). The old machine layer is untouched.', file=sys.stderr)
        return 'failed'
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, 'O_BINARY', 0)
        handle = os.fdopen(os.open(str(target), flags), 'wb')
        with handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        return 'skipped-target-present'
    except OSError as failure:
        print(f'  Warning: the machine layer\'s settings were not copied: {target} could not be '
              f'written ({failure}). The old machine layer is untouched.', file=sys.stderr)
        return 'failed'
    return 'copied'


def _rewrite_plan_paths(record: Path) -> int:
    """Rewrite every `plan_path` value under the record directory that names the old directory, and
    nothing else in the file it sits in: every other field, free text included, travels untouched.

    The field is the whole of this step's knowledge -- which record carries it, and which tool owns
    that record, is not the core's business. A file whose values all already name the new directory is
    not written at all; one that changes goes back through its own writer's byte contract (sorted
    keys, LF), so the file on disk stays a file its writer wrote.
    """
    jsonio = _loader().load_shared('jsonio')
    rewritten = 0
    for path in sorted(record.rglob('*.json')):
        if not path.is_file():
            continue
        try:
            data = jsonio.read_json_strict(path, str(path.relative_to(record)))
        except jsonio.JsonReadError as failure:
            print(f'  Warning: {failure}. A `plan_path` still naming the old directory makes its '
                  f'owner recreate it -- consult an agent.', file=sys.stderr)
            continue
        changed = _plan_paths_in(data)
        if not changed:
            continue
        jsonio.atomic_write_json(path, data, sort_keys=True)
        rewritten += changed
    return rewritten


def _plan_paths_in(value) -> int:
    """Walk one record's value, rewrite the `plan_path` strings in it, and count what changed."""
    changed = 0
    if isinstance(value, dict):
        for key, item in value.items():
            if key == 'plan_path' and isinstance(item, str):
                replaced = OLD_PREFIX.sub(lambda match: f'{match.group(1)}{LEDGER_NAME}/', item)
                if replaced != item:
                    value[key] = replaced
                    changed += 1
            else:
                changed += _plan_paths_in(item)
    elif isinstance(value, list):
        for item in value:
            changed += _plan_paths_in(item)
    return changed


def _sweep_records_to_lf(record: Path) -> tuple:
    """Put every record file on LF, and hand back `(files changed, files seen)`.

    The bytes are replaced, not re-serialized: a record is written by this tree's one writer, so
    rewriting it through that writer would also reformat whatever a person had edited by hand. CRLF is
    the only ending the 0.1.x writer produced beyond LF, and a JSON text carries no bare CR of its own.
    """
    changed = seen = 0
    for path in sorted(record.rglob('*.json')):
        if not path.is_file():
            continue
        seen += 1
        payload = path.read_bytes()
        if b'\r\n' not in payload:
            continue
        _atomic_write_bytes(path, payload.replace(b'\r\n', b'\n'))
        changed += 1
    return changed, seen


# ── the verb ───────────────────────────────────────────────────────────────

def cmd_migrate(generation: str | None = None, project: str | None = None,
                home: str | None = None) -> None:
    """`migrate [0.1.0]`: move this workspace's records, or say which generations may be named."""
    if not generation:
        print('migrate: name a generation to move this workspace\'s records.')
        for name, label in KNOWN_GENERATIONS:
            print(f'  {name}  {label}')
        print('Run `sw migrate 0.1.0`.')
        return
    known = [name for name, _label in KNOWN_GENERATIONS]
    if generation not in known:
        print(f'Error: unknown generation "{generation}": this tool moves {", ".join(known)} -- '
              f'name one of those, or run `sw migrate` with no generation to see the list. Nothing '
              f'was moved.', file=sys.stderr)
        sys.exit(1)
    root = Path(project).expanduser().resolve() if project else _resolve_project_root(Path.cwd())
    refusal, legacy, record = migration_preconditions(root)
    if refusal is None:
        legacy.rename(record)
        dead_lock = _clear_dead_lock(record)
        with _ledger_lock(str(root)):
            exclude = _move_exclude_line(root, record)
            attributes = 'rewritten' if repair_layout_file(record, '.gitattributes') else 'unchanged'
            usage = _repair_usage_note(record)
            command = register_driver(root)
            machine = _copy_machine_layer(home)
            plan_paths = _rewrite_plan_paths(record)
            changed, seen = _sweep_records_to_lf(record)
        print(f'Moved: {legacy} -> {record}')
        print(f'Ignore line: {exclude}')
        print(f'Record .gitattributes: {attributes}')
        print(f'Usage note: {usage}')
        print(f'Merge driver: registered (merge.superworker.driver = {command})')
        print(f'Machine layer: {machine}')
        print(f'Plan paths rewritten: {plan_paths}')
        print(f'Records on LF: {changed} of {seen} rewritten' if changed
              else f'Records on LF: all {seen} already were')
        if dead_lock:
            print('Lock file: removed the one the old generation left behind')
        print('Re-run `sw doctor` to read the health of the migrated project.')
        return
    if refusal == 'absent':
        print(f'migrate: nothing to move -- {ledger_dir_for(root)} is the record directory, and no '
              f'{legacy.name} is here.')
        return
    print(f'Error: {refusal_sentence(root, refusal)}', file=sys.stderr)
    sys.exit(1)