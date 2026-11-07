#!/usr/bin/env python3
"""The project's one cross-process ledger lock: `<project>/.superworker/meta/ledger.lock`.

The records this tree keeps are whole-file read-modify-write -- `state.json`, `ideas.json`,
`instructions.json`, a node's session note, and the records an installed plugin keeps of its own --
so two processes walking that sequence at once means the later write silently erases the earlier one.
Every mutating command holds this one lock across its read -> write, which is what makes conduct,
dag-manage, a note's own writer and every plugin tool that keeps a record here exclude each other.

    ledger_lock(ledger_dir, *, timeout=LEDGER_LOCK_TIMEOUT, gate=None)

`ledger_dir` is `<project>/.superworker`; the lock file sits in `meta/`, which `.superworker/.gitignore`
keeps out of both the project's history and the ledger's own, so losing it only loses the mutual
exclusion and never wedges anybody. `gate` is the caller's own "this ledger may be written at all"
check (the init markers); it runs once, before the outermost acquire.

**The lock file exists while the lock is held, and taking the lock is creating it.** Exclusive
creation (`O_CREAT | O_EXCL`) is the one atomic primitive here: no byte-range locks, no platform
branch, and nothing on the file's contents -- `msvcrt` versus `fcntl` is not a question this file
asks. Releasing is removing the file, retried for a moment because Windows refuses the unlink while a
reader of it is still closing. A holder that dies mid-way leaves the file behind, and the next caller
waits out the whole timeout and gets a decidable `TimeoutError`; that is the one thing a person has to
step in for.

This is also the protocol a workspace's records are shared under: another implementation of the same
record directory treats an empty `ledger.lock` as "someone is writing". A lock that lingers after it
was released is therefore not a cosmetic detail -- it makes every later write in that workspace wait
and then fail. The one exception is a record directory of the generation before this one, whose lock
never came out: `migrate` knows that generation and reads it there.

The lock is reentrant per process and keyed by the lock file: a command that already holds it -- a
dag command writing a node's note inside its own read-modify-write -- may acquire it again, and only
the outermost acquire touches the file. The record of who holds what belongs to the PROCESS, not to
this module object: these files are loaded by path, so a caller that loads this one under a second
name would otherwise get a second record, and its acquire would sit out the whole timeout against the
first copy's file. The record therefore lives in `sys.modules` under its own name, which is one per
process by construction.

A caller that cannot get the lock within `timeout` gets a `TimeoutError`: wedging silently is the one
failure this must not have. Whether that ends the command with a message or gives up an optional
write is the caller's decision, so nothing here prints or exits.
"""

import contextlib
import os
import sys
import time
from pathlib import Path

#: The give-up ceiling, not the norm: a writer holds the lock for milliseconds.
LEDGER_LOCK_TIMEOUT = 10.0

#: The cadence a busy lock is looked at: releasing it broadcasts nothing, so there is no event to wait
#: on and the file is asked again a few times a second.
LEDGER_LOCK_POLL = 0.05

#: Where this process keeps how deep it is in each lock file. A name nothing imports: it is only the
#: carrier for the one dict per process that every copy of this file has to see alike.
_RECORD = 'sw_shared_ledger_lock_depth'


def _depth() -> dict:
    """This process's depth per lock file -- keyed by the file, so a second project's lock is taken."""
    record = sys.modules.get(_RECORD)
    if not isinstance(record, dict):
        record = {}
        sys.modules[_RECORD] = record
    return record


def _lock_path(ledger_dir) -> Path:
    return Path(ledger_dir) / 'meta' / 'ledger.lock'


def _take(path: Path, timeout: float) -> None:
    """Create the lock file exclusively, waiting up to `timeout` for whoever holds it to let go."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            os.close(os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            return
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f'the ledger lock {path} stayed busy for {timeout:g}s; another process is '
                    f'mid-write, or a holder died and left the file behind (an empty lock file is '
                    f'all a dead holder leaves). Nothing was changed.') from None
            time.sleep(LEDGER_LOCK_POLL)


def _let_go(path: Path) -> None:
    """Remove the lock file: retried while a reader of it is still closing (Windows refuses then)."""
    for attempt in range(4):
        try:
            path.unlink(missing_ok=True)
            return
        except PermissionError:
            if attempt == 3:
                raise
            time.sleep(LEDGER_LOCK_POLL * (attempt + 1))


@contextlib.contextmanager
def ledger_lock(ledger_dir, *, timeout: float = LEDGER_LOCK_TIMEOUT, gate=None):
    """Hold one project's ledger lock for the duration of a read-modify-write. See the docstring."""
    path = _lock_path(ledger_dir)
    key = str(Path(path).resolve())
    depth = _depth()
    if depth.get(key):
        depth[key] += 1
        try:
            yield
        finally:
            depth[key] -= 1
        return
    if gate is not None:
        gate()
    path.parent.mkdir(parents=True, exist_ok=True)
    _take(path, timeout)
    depth[key] = 1
    try:
        yield
    finally:
        depth[key] = 0
        _let_go(path)