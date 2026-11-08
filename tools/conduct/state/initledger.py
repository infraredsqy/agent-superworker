"""`init` and `register`: the one setup act, the four layout files, and the merge driver's two keys.

A project either commits its `.superworker/` or keeps that directory to itself. Which one it is gets
decided here, once, and every other command just reads what is on disk -- a `.superworker/` the
project's git tracks is the committed kind, anything else is the local kind. Nothing else records the
choice, so there is no second place for it to drift out of step with reality.

What init writes -- one table (`LAYOUT_FILES`), and everything below derives from it:

    .superworker/.gitignore       `meta/ worktrees/ temp/` -- the tool's own machinery. Both modes want
                                  that ignored: the committed kind needs it out of the project's
                                  history, the own-repo kind needs it out of the ledger's own.
    .superworker/.gitattributes   no EOL conversion on the record files, and the entry-wise merge for
                                  the four that must not be merged line by line
    .superworker/AGENTS.md        how this directory is to be used. The tool owns it, not the host:
                                  every project that runs sw gets the same discipline, on any harness
    .superworker/config.json      the empty object, and only when the file is absent: a person edits
                                  this one, so a re-run must not wipe out what they wrote

The first two are the init markers. Every file is written with LF on every platform, and is rewritten
only when its bytes differ from the constant -- so re-running `init` repairs a hand-broken file and
leaves an intact one alone.

and, for `--local`, one line in the repository's `info/exclude`: the project's record directory,
anchored at the repository root (a project may be a subdirectory of it). That file belongs to the
clone and never travels, so no teammate ever sees a line about a directory that does not exist for
them. The line keeps the ending the file already uses, and another project's lines are left exactly
as they are. `--commit` takes this project's line back out.

`--own-repo` gives `.superworker/` a repository of its own, which buys local history and -- once it is
given a remote -- the trip between machines. That `git init` is the only git command this file runs,
and it is a setup act by construction: no runtime command ever touches a repository.

The merge driver is two keys in the project's git config, `merge.superworker.name` and
`merge.superworker.driver`; `register` is the door for a project that already has a ledger, and
`init` walks through it too. `migrate` re-registers the same pair through the same writer, because
the attribute the registration answers moved with the directory's name.
"""

import os
import subprocess
import sys
from pathlib import Path

from .store import CONFIG_NAME, LEDGER_NAME, _loader, _resolve_project_root

#: The three pieces of machinery under `.superworker/`. In the committed kind it keeps them out of the
#: project repository; with a repository of its own it keeps them out of that one.
GITIGNORE_BODY = """\
# This tool's own machinery: where dispatch lands, worktrees, the temp files a node produces.
meta/
worktrees/
temp/
"""

#: The patterns are relative to `.superworker/`, so the same file is right in both modes: the committed
#: kind governs `<project>/.superworker/…`, and the own-repo kind governs that repository's root.
GITATTRIBUTES_BODY = """\
# The line endings of these files are ours: every record is written with LF, and no machine's
# core.autocrlf has any business rewriting that.
** -text

# Records merge entry-wise, not line by line; the driver lives in tools/conduct/ledger-merge.py,
# and the command is registered in each machine's git config (sw register).
ideas.json merge=superworker
state.json merge=superworker
instructions.json merge=superworker
signals/** merge=superworker
"""

#: `.superworker/`'s own usage note, written by `init` and carried with the project: everyone who runs
#: sw sees the same discipline, with no hook hung on every tool call for it. To change it, ask the
#: user first.
LEDGER_AGENTS_BODY = """\
# .superworker usage note

> This file (`.superworker/AGENTS.md`) must not exceed 100 lines.
> This file is written and maintained by this tool at `init` time. **To change it, ask the user first** -- once they have said "go ahead", then touch it.

This directory is the superworker pipeline's ledger: `ideas.json`, `state.json`, `instructions.json` and
`signals/` are the pipeline's records, and `nodes/<uuid>/` holds the spec and the deliverable of
each slot (DAG node).

- Records are read and written only through `sw` (`sw dag …`, `sw plan`); hand-editing the JSON is forbidden, and so is reading `ideas.json` raw -- use the tool's `dag list` / `plan` output.
- Process artifacts belonging to one slot (probes, raw output, self-check scripts) go in `.superworker/nodes/<uuid>/temp/`, which is temporary and not committed: do not submit them as deliverables.
- Everything else temporary goes in `.tmp/` at the workspace root.
- `meta/`, `worktrees/` and `nodes/<id>/temp/` are kept out of the repository by `.gitignore`; do not move anything from inside them out by hand and commit it.
"""

#: `<project>/.superworker/config.json` as init hands it out: an empty object, LF, no trailing newline --
#: the record writer's own byte rule, so the file `init` lays down and the one `config set` rewrites are
#: the same bytes. A trailing newline here would give this one record two byte readings.
EMPTY_CONFIG_BODY = '{}'

#: The layout files, and the one table they come from: the name, the bytes, and whether the file lands
#: only when it is absent. `.superworker/config.json` is the one file with a rule of its own -- a person
#: edits it, so a re-run leaves it alone; the three constant files are repaired whenever their bytes
#: differ. The writing, the init gate and the migration's one-file repair all derive from here.
LAYOUT_FILES = (
    ('.gitignore', GITIGNORE_BODY, False),
    ('.gitattributes', GITATTRIBUTES_BODY, False),
    ('AGENTS.md', LEDGER_AGENTS_BODY, False),
    (CONFIG_NAME, EMPTY_CONFIG_BODY, True),
)

#: Where `sw` sits in this tool tree: three levels up from `<tools>/conduct/state/` is `<tools>`.
TOOLS = Path(__file__).resolve().parent.parent.parent

#: The merge driver's args, in git's own order, spelled once: the command string and the reader that
#: judges it are built from it, so a change of order cannot leave the two describing different lines.
MERGE_DRIVER_ARGS = '%O %A %B %P'

#: The attribute value the record files carry, and the two config keys that name its driver. One home
#: each: the layout template above, the registration below and `doctor` all read them from here.
MERGE_ATTRIBUTE = 'superworker'
MERGE_NAME_KEY = f'merge.{MERGE_ATTRIBUTE}.name'
MERGE_DRIVER_KEY = f'merge.{MERGE_ATTRIBUTE}.driver'
MERGE_NAME_TEXT = f'{MERGE_ATTRIBUTE} ledger three-way merge'

#: The command registered into `merge.superworker.driver` on a machine where `sw` is on PATH.
DRIVER_ON_PATH = f'sw merge-ledger {MERGE_DRIVER_ARGS}'


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    """Land one layout file whole: the write is `tools/shared/jsonio.py`'s, the same landing a record
    gets (temp file in the target's directory, fsync, a replace that tolerates a live reader)."""
    _loader().load_shared('jsonio').atomic_write_bytes(path, payload)


def write_layout_files(ledger, *, force: bool = False) -> list:
    """Land the layout files from the one table, and hand back the names this run wrote.

    The three constant files are written whenever their bytes differ, so a re-run repairs what was
    broken and leaves what is intact alone; `config.json` lands only when it is absent (or `force` was
    asked for), because it is a file humans and agents write into. Every file is LF: the body constants
    carry LF, and the landing is binary, so no platform translates it.
    """
    ledger = Path(ledger)
    written = []
    for name, body, only_when_absent in LAYOUT_FILES:
        path = ledger / name
        payload = body.encode('utf-8')
        if only_when_absent:
            if not force and path.is_file():
                continue
        elif path.is_file() and path.read_bytes() == payload:
            continue
        _atomic_write_bytes(path, payload)
        written.append(name)
    return written


def repair_layout_file(ledger, name: str) -> bool:
    """Bring one constant layout file's bytes current: True when this run wrote it.

    The same rule `write_layout_files` applies to the whole table, for the one file `migrate` needs:
    the record directory's `.gitattributes`, whose driver attribute moves with the rename. The body
    constant is the only spelling of the new attribute there is, and the file `config.json` is not
    repairable (`only_when_absent`) — asking for it answers False.
    """
    for entry_name, body, only_when_absent in LAYOUT_FILES:
        if entry_name != name:
            continue
        if only_when_absent:
            return False
        path = Path(ledger) / name
        payload = body.encode('utf-8')
        if path.is_file() and path.read_bytes() == payload:
            return False
        _atomic_write_bytes(path, payload)
        return True
    return False


# ── the exclude line ─────────────────────────────────────────────────────────

def split_exclude_lines(text: str) -> list:
    """The exclude text as `(line, its own ending)` pairs.

    Each line keeps the ending it arrived with -- `\\r\\n`, `\\n`, a lone `\\r`, or none on the last
    line -- so a write never half-normalizes a file it did not author.
    """
    lines = []
    index = 0
    while index < len(text):
        end = index
        while end < len(text) and text[end] not in '\r\n':
            end += 1
        if end < len(text) and text[end] == '\r' and text[end + 1:end + 2] == '\n':
            lines.append((text[index:end], '\r\n'))
            index = end + 2
        elif end < len(text):
            lines.append((text[index:end], text[end]))
            index = end + 1
        else:
            lines.append((text[index:end], ''))
            index = end
    return lines


def with_exclude_line(text: str, line: str, keep: bool) -> tuple:
    """Put this project's line into the exclude text (`keep`), or take it out; say whether it was there.

    Exactly one line per project root: a duplicate of this project's line is dropped, and a line
    spelling the same project root is replaced in place, so a repeated write never leaves two lines
    behind. Lines belonging to **other** project roots are left exactly as they are -- taking one away
    would silently un-ignore another project's records. A line this run adds takes the ending the file
    already shares, and LF otherwise.
    """
    out, had, placed = [], False, False
    for current, ending in split_exclude_lines(text):
        if current.strip() == line:
            had = True
            if keep and not placed:
                out.append((line, ending))
                placed = True
            continue
        out.append((current, ending))
    if keep and not placed:
        shared = out[0][1] if out else '\n'
        endings = {ending for _current, ending in out}
        out.append((line, shared if len(endings) == 1 and shared else '\n'))
    if not out:
        return '', had
    pieces = []
    for index, (current, ending) in enumerate(out):
        if ending == '' and index < len(out) - 1:
            ending = '\n'                      # only the last line may end without an ending
        pieces.append(current + ending)
    return ''.join(pieces), had


def read_exclude_text(path) -> str | None:
    """The exclude file's text, or None when the file is not there.

    The reader is strict: bytes that are not valid UTF-8 raise rather than decode leniently, because
    rewriting such a file from a replacement-character reading would destroy lines this tool did not
    write. Any other read failure raises too — folding it to an empty file would drop every other
    project's line.
    """
    path = Path(path)
    if not path.exists():
        return None
    return path.read_bytes().decode('utf-8')


def resolve_exclude_path(project) -> Path | None:
    """This project's repository ignore list (`info/exclude`), or None when there is none to write.

    None when this is not a git project, or when git is not at hand: that kind of project has nowhere
    to be swept into, so the `--local` ignore line has no meaning either.
    """
    if _git(project, 'rev-parse', '--git-dir').returncode != 0:
        return None
    common = _git(project, 'rev-parse', '--git-common-dir').stdout.strip()
    if not common:
        return None
    gitdir = Path(common)
    if not gitdir.is_absolute():
        gitdir = (Path(project) / gitdir).resolve()
    return gitdir / 'info' / 'exclude'


def exclude_line_for(project, dir_name: str | None = None) -> str | None:
    """The line one directory name gets for this project, or None when git will not say where it sits.

    Anchored at the repository root, so several record directories in one repository each get their
    own. The directory name is a parameter because `migrate` asks for the old name's line -- the one
    it has to take out -- through the same probe that writes the new name's.
    """
    name = dir_name if dir_name is not None else LEDGER_NAME
    if _git(project, 'rev-parse', '--git-dir').returncode != 0:
        return None
    shown = _git(project, 'rev-parse', '--show-prefix')
    if shown.returncode != 0:
        return None
    prefix = shown.stdout.strip().replace('\\', '/').strip('/')
    return f'/{name}/' if prefix == '' else f'/{prefix}/{name}/'


def _write_exclude(path: Path, line: str, keep: bool) -> str:
    """Plant or pull this project's line in `info/exclude`, and return one sentence saying what was done."""
    try:
        current = read_exclude_text(path)
    except (OSError, UnicodeDecodeError) as failure:
        return (f'Warning: {path} was found but could not be read ({failure}), so rewriting it could '
                f'drop another project\'s line; nothing was written.')
    next_text, had = with_exclude_line(current or '', line, keep)
    if next_text == (current or ''):
        return (f'wrote into {path}: {line}' if keep
                else f'{path} had no ignore line in the first place.')
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_bytes(path, next_text.encode('utf-8'))
    if keep:
        return f'wrote into {path}: {line}' + (' (replaced the old pattern)' if had else '')
    return f'pulled the ignore line out of {path}.'


# ── the merge driver ────────────────────────────────────────────────────────

def driver_command() -> str:
    """The command registered into `merge.superworker.driver`.

    `sw` on PATH is preferred: that command carries no absolute path, so moving the checkout elsewhere
    or switching machines keeps the line already registered in the project valid, with no per-project
    redo. When `sw` is not on PATH yet, fall back to the absolute form and ask a human to fix PATH --
    after that, one more `sw register` makes the registered line location-independent.
    """
    for entry in os.environ.get('PATH', '').split(os.pathsep):
        if entry and (Path(entry) / 'sw').is_file():
            return DRIVER_ON_PATH
    return (f'"{sys.executable}" -X utf8 "{TOOLS / "bin" / "sw.py"}" '
            f'merge-ledger {MERGE_DRIVER_ARGS}')


def register_driver(project: Path) -> str:
    """Register one project's merge driver, and return the command that was registered."""
    command = driver_command()
    _git(project, 'config', MERGE_NAME_KEY, MERGE_NAME_TEXT)
    _git(project, 'config', MERGE_DRIVER_KEY, command)
    return command


def registered_driver(project) -> str:
    """The driver string this project's git config holds, or '' when nothing is registered."""
    result = _git(project, 'config', '--local', '--get', MERGE_DRIVER_KEY)
    return result.stdout.strip() if result.returncode == 0 else ''


def cmd_register(project: str | None = None) -> None:
    """`register`: register the merge driver, and print the command that was registered.

    `init` already does this along the way; the verb is here so that a project already init-ed can
    top the driver up on its own (re-running it once is enough after a new machine or a new
    checkout location), and so that `sw register` has something to forward to.
    """
    root = Path(project).expanduser().resolve() if project else _resolve_project_root(Path.cwd())
    print(register_driver(root))


def _git(project: Path, *args: str) -> subprocess.CompletedProcess:
    """`tools/shared/gitcmd.py`: the tree's one way of asking git (cwd, decoding, no-raise all live there)."""
    return _loader().load_shared('gitcmd').git(project, *args)


def _untrack(project: Path, ledger: Path) -> str:
    """The local kind: a `.superworker/` that already got into the project repository has to come out of
    the index, with the files left on disk.

    Leaving it in would make the mode a lie -- on disk it looks local (the ignore line is there, and
    the next commit does not carry it), while the project repository still takes the whole directory
    along. An ignore line cannot govern a file already tracked; only the index decides this one.
    """
    if _git(project, 'ls-files', '--', LEDGER_NAME).stdout.strip() == '':
        return 'the project repository never tracked `.superworker/`; the index was left alone.'
    done = _git(project, 'rm', '-r', '-q', '--cached', '--', LEDGER_NAME)
    if done.returncode != 0:
        return f'Warning: `git rm --cached` failed: {done.stderr.strip()}'
    return ('took `.superworker/` out of the project repository index (every file is still on disk; '
            'commit this round as you like).')


def cmd_init(project: str | None = None, mode: str = 'local', own_repo: bool = True) -> None:
    """Lay `.superworker/` out as a usable evidence directory, and settle whether it enters the project
    repository or stays on this machine alone.

    Idempotent: the three constant files are rewritten from their constants only when their bytes
    differ, the settings file is left alone once it is there, and the ignore line is planted or pulled
    according to the mode. Re-running on an already init-ed project has no side effect, and switching
    modes is just another run -- `--local` and `--commit` each settle the `info/exclude` line their own
    way.
    """
    root = Path(project).expanduser().resolve() if project else _resolve_project_root(Path.cwd())
    ledger = _loader().load_shared('ledger').ledger_dir_for(root)
    own_repo = own_repo and mode == 'local'
    ledger.mkdir(parents=True, exist_ok=True)
    written = write_layout_files(ledger)
    config_file = ledger / CONFIG_NAME

    print(f'Ledger: {ledger}')
    print(f'Layout files: {"wrote " + ", ".join(written) if written else "all current, nothing written"}')
    print(f'Mode: {"this machine only (.superworker/ does not enter the team repository)" if mode == "local" else "into the version repository with the project"}')
    print(f'Config file: {config_file}'
          f' ({"a freshly laid empty object" if CONFIG_NAME in written else "already there, left alone"}; '
          f'empty = all factory defaults, and `sw config set` is how to change one)')

    exclude_path = resolve_exclude_path(root)
    line = exclude_line_for(root)
    if exclude_path is None or line is None:
        print('This project is not a git checkout: there is no ignore line to speak of, and the '
              'whole pull / push story does not apply either.')
    else:
        print(_write_exclude(exclude_path, line, mode == 'local'))
        if mode == 'local':
            print(_untrack(root, ledger))

    if own_repo and not (ledger / '.git').exists():
        done = _git(ledger, 'init', '--quiet')
        if done.returncode == 0:
            print(f'Gave {ledger} a repository of its own: local history starts here, and once a '
                  f'remote is attached to it, it can travel to another machine on its own.')
        else:
            print(f'Warning: `git init` failed: {done.stderr.strip()}', file=sys.stderr)
    elif own_repo:
        print(f'{ledger} already has a repository of its own; it was left alone.')

    if mode == 'local':
        print('A reminder: `.superworker/` is ignored, so `git clean -xdf` deletes it, history and '
              'all. Attaching a remote is the only way to get it back.')

    if _git(root, 'rev-parse', '--git-dir').returncode == 0:
        command = register_driver(root)
        print(f'Merge driver registered: {MERGE_DRIVER_KEY} = {command}')
        if not command.startswith('sw '):
            print('  Note: `sw` is not on PATH yet, so the absolute form was registered instead. '
                  'Put its bin directory on PATH, run `sw register` once more, and this line '
                  'becomes location-independent.')