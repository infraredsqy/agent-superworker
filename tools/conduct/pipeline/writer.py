"""The writing half: `apply` runs what `plan` decided; `settle`, `dispatch`, `advance` are the words.

`settle` is one node's end (plan's closing reading, written down); `dispatch` opens the sessions of
the cells a line holds; `advance` is claim + material + session in one word. Also `_snapshot`,
`resolve_spawner`, `spawn_session`, and `clean_launch_dir`.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from . import insertion, seam
from .store import (_holding_ids, _loader, cycle_policy, find_cycle, load_dag, load_signal,
                    load_state, note_session)
from .prompts import (launch_dir, launch_prompt_path, started_marker, write_prompt_file)
from .planner import plan

#: How long a spawn claim may sit before it counts as stale: a spawn takes seconds (the spawner's
#: own ceiling is 300), and a claim left behind by a killed process must not wedge the retry
#: forever. A live claim is removed by `clean_launch_dir` together with the launch directory.
SPAWN_CLAIM_STALE_SECONDS = 30 * 60

#: The one place an argv for `dsh-spawn.ps1` is built. It sits next to the entry scripts rather than
#: inside this package so that the spawner stays reachable without loading the pipeline.
SPAWN_PY = Path(__file__).resolve().parent.parent / 'spawn' / 'conduct_spawn.py'

#: The two spawners and the host's own answer about itself, next to those scripts for the same
#: reason.
HOST_PY = Path(__file__).resolve().parent.parent / 'spawn' / 'host.py'


def _spawner():
    """`conduct_spawn.py`, loaded by path -- the way every other file in this tree is loaded.

    One copy per process (`register` + `reuse`): one dispatch asks this module three times (the call,
    then the session id and the host of its last line), and `spawn/host.py` is asked for the same
    way. Which spawner SCRIPT it runs is not state here -- that arrives as an argument.
    """
    return _loader().load_by_path('conduct_spawn', SPAWN_PY, register=True, reuse=True)


def _host():
    """`spawn/host.py`, loaded by path -- the same copy the entry module asks for."""
    return _loader().load_by_path('conduct_spawn_host', HOST_PY, register=True, reuse=True)


def resolve_spawner(project, cycle_id: str | None, cli_value: str | None = None) -> Path:
    """Which spawner opens this dispatch's session: the most specific answer wins.

    `--spawner` (whatever the caller names this once) > this line's own `spawner` policy
    (`sw cycle policy <cycle> spawner dsh|zcode`, which rides along with the line's record) >
    `DSH_CONDUCT_SPAWNER` (this machine) > the host's own answer > `dsh`. A line with no policy and a
    call with no flag fall back to `SPAWNER_SCRIPT` -- an unconfigured line behaves unchanged.
    `SPAWNER_SCRIPT` rather than working out the host now: it is the replaceable seam at the entry
    (the `seam.bind` convention), so a caller that replaces it is still honoured when a line policy
    is absent.
    """
    host = _host()
    if cli_value:
        return host.spawn_path(cli_value)
    if cycle_id:
        cycle = find_cycle(load_state(project), cycle_id)
        word = cycle_policy(cycle).get('spawner') if cycle else None
        if word:
            return host.SPAWN_SCRIPTS[word]
    return seam.spawner_script()


# ── the writer ───────────────────────────────────────────────────────────────

def _snapshot(project: Path, cycle_id: str) -> tuple:
    """Everything an action is allowed to be "progress" on: `(hand, path, status)`.

    The cycle's hand (which nodes, collectively), where it points, and its status -- in a
    one-node-per-slot pipeline those are the only things a command may move. One shape for both
    answers: when the line is not on disk the three positions are still there, each answering
    "nothing" (`None`) -- a caller reading by position always reads the same thing, and never the
    cell of another shape whose meaning has shifted.
    """
    state = load_state(project)
    cycle = find_cycle(state, cycle_id)
    if cycle is None:
        return (None, None, None)
    sigs = tuple((node, (load_signal(project, cycle_id, node) or {}).get('status'))
                 for node in _holding_ids(cycle))
    return (sigs, bool(cycle.get('path')), cycle.get('status'))


#: `_snapshot`'s three positions, by name: `progress` reports them under these keys, in this order.
_SNAPSHOT_FIELDS = ('hand', 'path', 'status')


def _graph_nodes(project: Path) -> frozenset:
    """The node ids on the graph, for the one action whose whole product IS the graph's growth.

    An insertion edits the graph and nothing else: it opens no session, pins no node and writes no
    path step. At a close the commands that carry it also settle the node (so `_snapshot` moves), but
    at a PICK there is no node to settle and the action would look like a no-op to the apply
    contract -- while it has in fact created a node. This is the insertion's own progress reading,
    and it is only ever asked for that action kind.
    """
    try:
        return frozenset((load_dag(project).get('nodes') or {}))
    except (RuntimeError, ValueError):
        return frozenset()


def _claim_pid(body: str) -> int | None:
    """The pid a claim body opens with (`<pid> started <iso>`); None when there is none to read.

    A body without a parseable leading int (an empty read, a hand-touched file) has no liveness to
    check, and the caller falls back to the staleness rule for it -- the pid check is a tightening
    of the takeover rule, never a replacement that needs the pid to exist.
    """
    try:
        pid = int(body.split()[0])
    except (IndexError, ValueError):
        return None
    return pid if pid > 0 else None


def _claim_spawn(project: Path, cycle_id: str, node_id: str) -> tuple[bool, Path | None]:
    """Claim the right to start this node's session, exclusively.

    Two dispatches of the same node -- a retry racing the in-flight one, a rescue running while the
    first queue row is still alive -- must not both reach the spawner: two sessions would boot onto
    the same node. `O_CREAT | O_EXCL` is the one atomic primitive here; a losing caller gets the
    claim's path and a message. An EXISTING claim is judged by its writer before its age: a live pid
    refuses the dispatch however old the claim looks (a spawn can legitimately run for minutes, so
    mtime alone would take the claim over mid-flight), and only a dead writer falls through to the
    `SPAWN_CLAIM_STALE_SECONDS` takeover.

    Two takers in the window between an unlink and a rebuild would delete each other's FRESH claim,
    so the unlink is followed by the create itself as the re-check: the replacement is built with
    `O_CREAT | O_EXCL` again, and if it loses -- someone rebuilt the claim between the unlink and the
    open -- the loop re-reads THAT claim's pid and mtime and judges it from the top. Three rounds,
    then give up with the refusal; a caller that really is alone will win one of them, and a caller
    that is not must not win any.
    """
    directory = launch_dir(project, cycle_id) / node_id
    directory.mkdir(parents=True, exist_ok=True)
    claim = directory / 'SPAWN.claim'
    for _ in range(3):
        try:
            fd = os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as f:
                f.write(f'{os.getpid()} started {time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}')
            return True, claim
        except FileExistsError:
            try:
                body = claim.read_text(encoding='utf-8', errors='replace')
            except OSError:
                body = ''
            pid = _claim_pid(body)
            if pid is not None and _host().pid_alive(pid):
                return False, claim
            try:
                age = time.time() - claim.stat().st_mtime
            except OSError:
                age = 0.0
            if age <= SPAWN_CLAIM_STALE_SECONDS:
                return False, claim
            try:
                claim.unlink()
            except OSError:
                return False, claim
            # The re-check and the rebuild are one step: the next round creates again with
            # O_CREAT|O_EXCL, which only succeeds once the path is really gone; if another taker
            # rebuilt it first, open raises FileExistsError on the spot, and that round re-reads the
            # new claim's pid and mtime and walks the whole judgement again from the top. Three
            # rounds and it returns with the refusal.
    return False, claim


def spawn_session(project: Path, cycle_id: str, prompt_file: str | None = None,
                  dry_run: bool = False, spawner: Path | None = None) -> str:
    """Open the next session for a node. `prompt_file` is what it will be told to do.

    This is the one place the pipeline starts a session, and `prompt_file` is REQUIRED in practice:
    the spawner on the other end refuses a call without one, because a session with nothing to load
    is not a thing the pipeline has. A `None` here is a caller bug and surfaces as the spawner's own
    loud failure, not as a silent second kind of session.

    The callers are the two spellings of one call: `dispatch <cycle>` (open the sessions of the
    cells a line holds) and `advance` (claim, land the material, open them); both go through
    `dispatch_cells`, and nobody hand-rolls the API calls.

    The call is guarded by a spawn claim (`_claim_spawn`), so a double dispatch -- cron rescue
    against an in-flight spawn -- is refused instead of booting two sessions onto one node. A
    successful spawn leaves the claim in place for `clean_launch_dir` to remove with the launch
    directory; a failed one releases it, because the prompt file is exactly what a human retries.
    Only a PIPELINE dispatch is claimed: the prompt must sit in the launch directory it belongs to
    (`meta/launch/<cycle>/<node_id>/prompt.txt`).

    The argv, the ceiling and the wording of a failure live in `conduct_spawn.py` -- this function is
    the pipeline's half of that seam (which spawner, and nothing else). `spawner` is how a caller
    answers "which one" outright; left out, the entry's `SPAWNER_SCRIPT` answers, and that default is
    the host's own (`spawn/host.py`'s `default_spawner`).
    """
    claim = None
    if not dry_run and prompt_file:
        prompt_path = Path(prompt_file)
        node_id = prompt_path.parent.name
        node_dir = launch_dir(project, cycle_id) / node_id
        if prompt_path.parent == node_dir:
            got, claim = _claim_spawn(project, cycle_id, node_id)
            if not got:
                raise RuntimeError(
                    f'this node already has a spawn claim ({claim}); the pid that wrote it may '
                    f'still be dispatching, and a live pid refuses every retry however old the '
                    f'claim looks (only a dead pid falls back to the '
                    f'{SPAWN_CLAIM_STALE_SECONDS // 60}-min staleness rule). A recycled pid can '
                    f'trip this by mistake: if you are sure nothing is running, delete the claim '
                    f'file and dispatch again')
    chosen = Path(spawner) if spawner else seam.spawner_script()
    try:
        line = _spawner().spawn_session(project, cycle_id, prompt_file,
                                        spawner=chosen, dry_run=dry_run)
    except (RuntimeError, ValueError, OSError):
        if claim is not None:
            try:
                claim.unlink()
            except OSError:
                pass
        raise
    if not dry_run:
        _note_run(project, chosen, prompt_file, line)
    return line


def _note_run(project: Path, spawner: Path, prompt_file: str | None, line: str) -> None:
    """Note the session this dispatch just opened, next to the node it will run.

    Best effort, for one reason worth stating: the session is ALREADY RUNNING by the time this
    returns, so a note that cannot be written must not turn a finished dispatch into a failure --
    what it costs is a conversation that is harder to open, not a node that never ran.

    The node id comes from the prompt file's own directory (`meta/launch/<cycle>/<node id>/prompt.txt`,
    the same reading the spawn claim makes), the session id and the host from the spawner's last line
    through `conduct_spawn.py` -- the only place either exists at all. A spawner that is not one of
    the two shipped scripts (a caller's own, a test's stand-in) states no host; the note then takes
    the id's own shape as the answer, which is what a reader would do with it anyway.
    """
    if not prompt_file:
        return
    spawner_module = _spawner()
    session = spawner_module.session_id_of(line)
    if not session:
        return
    try:
        note_session(project, Path(prompt_file).parent.name, 'run', session,
                     host=spawner_module.host_of(spawner))
    except (OSError, ValueError):
        pass


def clean_launch_dir(project: Path, cycle_id: str, node_id: str) -> None:
    """Mark one node's launch dir as taken: its session is running.

    Called ONLY after a successful spawn. The prompt goes (the session has it), the claim goes (that
    dispatch is over), and a `started` stamp stays behind -- when a line holds several slots, the
    close re-plans "every slot in hand" into the same dispatch, and this stamp is the landing point
    for "this slot's session has already been opened": without it, the same node would be handed to
    the spawner again and two sessions would boot onto one node.

    A failed spawn is the other case: the prompt file is what a human retries by hand, so it has to
    survive -- which is why this runs only on the success path. The directory itself stays as the
    stamp's home and is reaped by `_prune_launch_dirs` once the node leaves the line.
    """
    directory = launch_dir(project, cycle_id) / node_id
    try:
        for name in ('prompt.txt', 'SPAWN.claim'):
            f = directory / name
            if f.exists():
                f.unlink()
        marker = started_marker(project, cycle_id, node_id)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            f'{os.getpid()} started {time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}\n',
            encoding='utf-8')
    except OSError:
        pass
    _prune_launch_dirs(project, cycle_id)


def _prune_launch_dirs(project: Path, cycle_id: str) -> None:
    """Drop the launch dirs of the nodes this cycle no longer holds.

    A node whose session is running needs its footprint kept (that is where the stamp lives), so the
    directory outlives the spawn and is reaped here instead. A node leaves the hand on the close
    beat, and the dispatch follows right after it: the moment to sweep is exactly here.
    """
    import shutil
    try:
        cycle = find_cycle(load_state(project), cycle_id)
    except (RuntimeError, ValueError):
        return
    keep = set(_holding_ids(cycle)) if cycle else set()
    try:
        dirs = [d for d in launch_dir(project, cycle_id).iterdir()
                if d.is_dir()]
    except OSError:
        return
    for d in dirs:
        if d.name not in keep:
            shutil.rmtree(d, ignore_errors=True)


def apply(project, cycle_id: str | None = None, action: dict | None = None) -> dict:
    """plan() + carry out the writing half, then enforce the contract invariant.

    The `commands` are RUN HERE and stripped from the returned action. A caller that reads
    `commands` and runs them itself is doing the v1 double-step; the field simply is not there to
    read.

    `action` is for a caller that ALREADY planned (`settle` and `advance` do): handing it in is what
    keeps the executed package the one that was planned -- re-planning here would build a second
    action for the same state, and anything the first plan carried outside `commands` (a multi-node
    dispatch's per-node prompt list, for one) would be planned twice differently.
    """
    project = Path(project).resolve()
    if action is None:
        action = plan(project, cycle_id)
    kind = action['action']
    commands = action.get('commands') or []
    # Built separately by the planner, run first and then dropped from the list (see the marker slot
    # below); popped so it never reaches a caller as something to run.
    add_command = action.pop('add_command', None)
    spec_of = action.pop('spec_of', None)
    # An insertion action may carry SEVERAL nodes (one per source that asked for one). Each has its
    # own `add`, its own text and its own commands; the first one's list already carries the close
    # this they all ride on, so the order a caller sees is the order they run in.
    insertions = [item for item in (action.get('insertions') or []) if isinstance(item, dict)]

    prompts = ([{'node_id': action['node_id'], 'prompt': action['prompt'],
                 'prompt_text': action['prompt_text']}] if action.get('prompt_text')
               else [n for n in (action.get('nodes') or []) if isinstance(n, dict)])
    if prompts:
        # Every dispatch carries one prompt per node in hand; writing them is not conditional on
        # there being commands to run (a multi-node dispatch has none -- the pins already ran).
        # Skip the slot that is already running: the prompt is the "waiting to be opened" stamp, and
        # putting it back says it has not opened yet.
        for item in prompts:
            if not item.get('prompt_text'):
                continue
            if started_marker(project, action['cycle'], item['node_id']).is_file():
                continue
            write_prompt_file(project, action['cycle'], item['node_id'], item['prompt_text'])
    if not commands and add_command is None:
        action['applied'] = False
        return action

    if add_command is None and kind == insertion.INSERT_ACTION and not insertions:
        raise RuntimeError(f'action {kind!r} arrived without its `add` command: the inserted '
                           f'node cannot be created')

    before_state = _snapshot(project, action['cycle'])
    # An insertion may be the only thing that runs (a pick-time one carries no close commands):
    # then the graph's own growth is the progress, and it is read only for that action kind.
    before_graph = _graph_nodes(project) if kind == insertion.INSERT_ACTION else None
    # What runs, in order: one list per insertion (a singular action is one list too). The marker
    # slot is where `add` belongs: the close commands ahead of it have to land first (the dedup
    # judgement reads the graph, and `add`'s uuid is what the rest of the list is written in terms
    # of), so `done` counts what has already run when it fires.
    runs = ([{'commands': list(item.get('commands') or []), 'add': item.get('add_command'),
              'spec_of': item.get('spec_of'), 'spec': item.get('spec_path'),
              'steps': list(item.get('steps') or [])} for item in insertions]
            if insertions else
            [{'commands': list(commands), 'add': add_command, 'spec_of': spec_of,
              'spec': action.get('spec'), 'steps': list(action.get('steps') or [])}])
    # An index loop, not `for cmd in commands`: the marker step REBINDS a run's command list
    # (placeholder substituted), and a `for` would go on iterating the pre-substitution list it
    # captured.
    ran: list = []
    done = 0
    total = sum(len(run['commands']) for run in runs)
    filled_lists: list = []
    for index, run in enumerate(runs):
        cmds = run['commands']
        add = run['add']
        i = 0
        while i < len(cmds):
            cmd = cmds[i]
            try:
                # The marker slot is where `add` belongs: run it there (its stdout carries the uuid
                # the rest of the list is written in terms of), read the uuid, substitute it in.
                if add is not None and insertion.is_add_slot(cmd):
                    node_id, cmds = insertion.mint(project, add, run['spec_of'], cmds)
                    ran.append(insertion.fill_all(list(add), node_id))
                    # The uuid did not exist when this action was built: the paths it reports and the
                    # steps it shows a reader were written against the placeholder. The first
                    # insertion is the action's own (`node_id` / `spec` / `steps`) -- unless the
                    # action is something else that an insertion rode on (a `settle`, whose own
                    # `node_id` is the node that was closed), and then the new node is reported in
                    # `inserted_node` instead.
                    if kind == insertion.INSERT_ACTION:
                        if index == 0:
                            action['node_id'] = node_id
                    else:
                        action['inserted_node'] = node_id
                    if insertions:
                        # Each insertion reports the node it became, next to the first one's mirror.
                        insertions[index]['node_id'] = node_id
                    if run.get('spec'):
                        run['spec'] = insertion.fill(run['spec'], node_id)
                    if run.get('steps'):
                        run['steps'] = insertion.fill_all(run['steps'], node_id)
                    add = None
                    done += 1
                    continue              # the rebind moved every later command: re-read this slot
                seam.run(cmd, project)
            except RuntimeError as e:
                if done:
                    raise RuntimeError(f'{e}\n(commands {done}/{total} of action {kind!r} '
                                       f'already ran: the state is half-applied, look before '
                                       f'retrying)') from e
                raise
            done += 1
            i += 1
        if add is not None:
            raise RuntimeError(f'action {kind!r} carried an `add` command that never ran: the '
                               f'inserted node cannot be created')
        filled_lists.append(cmds)
    commands = [cmd for cmds in filled_lists for cmd in cmds]
    # The uuid did not exist when this action was built, so the fields a reader is shown were written
    # against the placeholder; they now carry the first insertion's real one (a singular action's only
    # one is its own).
    head = runs[0]
    if action.get('spec'):
        action['spec'] = head['spec']
    if action.get('steps'):
        action['steps'] = head['steps']
    if insertions:
        # An insertion's own `add`, text builder and command list are the writer's half of the
        # mechanism, never the caller's: they are what `apply` just consumed.
        for item, run in zip(insertions, runs):
            item['spec_path'] = run['spec']
            item['steps'] = run['steps']
            for key in ('add_command', 'spec_of', 'commands'):
                item.pop(key, None)
        action['insertions'] = insertions
    after_state = _snapshot(project, action['cycle'])

    if before_state == after_state and (before_graph is None or before_graph == _graph_nodes(project)):
        raise RuntimeError(
            f'contract violation: action {kind!r} ran {len(commands)} command(s) but nothing '
            f'changed (before={before_state}, after={after_state})')
    action['applied'] = True
    action['did'] = [seam.as_text(c) for c in ran + commands]
    action['progress'] = {'before': dict(zip(_SNAPSHOT_FIELDS, before_state)),
                          'after': dict(zip(_SNAPSHOT_FIELDS, after_state))}
    action.pop('commands', None)
    # An open_node is a dispatch in disguise: run the follow-up too, so callers have exactly one
    # shape to handle ("spawn a session with this prompt").
    #
    # An `insert_node` is NOT, and that is the whole point of it: the graph grew a node, no session
    # was opened and the inserted node is not pinned, so this action is where the call stops. What
    # picks the node up is the next dispatch (`_pick_next` and the ranking), exactly as for any other
    # node.
    if kind == 'open_node':
        follow = apply(project, action['cycle'])
        follow['applied'] = True
        follow['opened_node'] = action.get('node_id')
        follow['did'] = action.get('did', []) + follow.get('did', [])
        # The sentence the open carried rides along: what the caller gets is the re-planned dispatch,
        # and the open_node action itself never reaches him, so anything left there would be read by
        # nobody (for one, "the node named to run is not in the filter set").
        spoken = (action.get('follow') or {}).get('note') or action.get('note')
        if spoken:
            follow['note'] = spoken
        return follow
    return action


def settle(project, cycle_id: str | None = None, node: str | None = None) -> dict:
    """Record one node's end: `plan(after=node)` and the writing half of it, in one call.

    The settle is the whole of a worker's close, and the whole of it: the plan behind it is
    `plan(project, cycle_id, after=node)` -- the same reading `sw plan --after` prints as a
    rehearsal -- and `apply` runs the three steps it names (the step into the line's `path`, the
    graph mark, the close-time plugin seam). It pins nothing, lands no prompt and opens nothing; a
    line whose round the stop rule ended is turned `inactive` by that same write, and `parked` in
    the answer says so.

    `node` is required in practice (a line holding several needs to know which close this is); with
    it absent the plan takes the one node in hand, which is unambiguous only when the line holds
    exactly one.
    """
    project = Path(project).resolve()
    action = plan(project, cycle_id, after=node)
    if action.get('action') != 'settle':
        # `wait` (no ticket yet, or not this line's node) and `ask_user` (the line does not hold it)
        # carry no commands: handing them back as they stand is the honest answer, and writing
        # nothing is what they promise.
        return action
    return apply(project, action=action)


def dispatch_cells(project, cycle_id: str, spawner=None, dry_run: bool | None = None) -> dict:
    """Open one session per cell this line is holding, and nothing else.

    The session half of both `dispatch` and `advance`: every cell in hand whose `started` stamp is
    absent and whose prompt is on disk is handed to the spawner, in the line's own order. A cell
    whose session already ran is skipped (`skipped`), and a cell with no prompt is reported
    (`unprompted`) rather than guessed at -- landing material is the dispatch's own step, and
    `advance` has already run it.

    `dry_run` left as `None` reads `DSH_CONDUCT_SPAWN_DRY_RUN` (the machine-wide drill): the call is
    made and answered, no session opens, and the `started` stamp does not land either.
    """
    project = Path(project).resolve()
    cycle = find_cycle(load_state(project), cycle_id)
    holding = _holding_ids(cycle) if cycle else []
    if dry_run is None:
        dry_run = os.environ.get('DSH_CONDUCT_SPAWN_DRY_RUN', '') not in ('', '0', 'false', 'False')
    ready, skipped, unprompted = [], [], []
    for node_id in holding:
        if started_marker(project, cycle_id, node_id).is_file():
            skipped.append(node_id)
            continue
        prompt = launch_prompt_path(project, cycle_id, node_id)
        if not prompt.is_file():
            unprompted.append(node_id)
            continue
        ready.append((node_id, prompt))
    receipt = {'spawned': [], 'skipped': skipped, 'unprompted': unprompted}
    if not ready:
        return receipt
    # One answer for the whole dispatch: CLI naming > line policy > env var > the host's own guess.
    chosen = resolve_spawner(project, cycle_id, spawner)
    for node_id, prompt in ready:
        receipt['spawned'].append(spawn_session(project, cycle_id, str(prompt),
                                                dry_run=dry_run, spawner=chosen))
        if not dry_run:
            clean_launch_dir(project, cycle_id, node_id)
    return receipt


def advance(project, cycle_id: str | None = None, spawner=None,
            dry_run: bool | None = None) -> dict:
    """The composed word: claim, land the material, open the sessions. One call, three moves.

    Every move in it is a word of its own -- `hold`, `dispatch` -- and this is only their order:

      1. **claim**: the plan's `open_node` (the head of the ranking, as many cells as this line has
         free slots for), applied -- the pins land, and `apply` re-plans into the dispatch;
      2. **material**: that dispatch's prompt files, which `apply` writes on the way through;
      3. **session**: `dispatch_cells`, one session per cell in hand.

    Nothing else. It **settles nothing** -- a node whose work ended is recorded by `settle` -- and it
    never writes a node's status. One exception, and it is the plan's own answer: a line whose plan
    says `stop_pipeline` while it holds nothing and is still `active` is turned `inactive` here
    (`parked`), because a stop is the plan's answer and somebody has to write it down.

    An insertion at a pick stops the step where it is: the graph grew a node and nothing was opened,
    and the caller commits and pushes before the next `advance` picks it up.
    """
    project = Path(project).resolve()
    receipt = {'cycle': cycle_id, 'pinned': [], 'plan': None, 'acted': None, 'parked': False,
               'spawned': [], 'skipped': [], 'unprompted': [], 'inserted': []}
    action = plan(project, cycle_id)
    # The reading this word carried out, kept beside the fresh one below: after a park the plan on
    # an inactive line reads `ask_user`, so the reason the line stopped would be nowhere in the
    # receipt without this.
    receipt['acted'] = action
    if action.get('action') == 'open_node':
        # `open_node` names the cells as ids; the dispatch `apply` re-plans into names them as
        # entries (one prompt file each), so the claim is read here, before the re-plan.
        receipt['pinned'] = list(action.get('nodes') or [])
        action = apply(project, cycle_id, action=action)
        receipt['acted'] = action
    elif action.get('action') == insertion.INSERT_ACTION:
        # A pick-time insertion is a graph edit and nothing else: it lands here, no cell is claimed
        # and no session opens, and the caller commits and pushes before the next `advance` picks
        # the new node up by the usual ranking.
        applied = apply(project, cycle_id, action=action)
        receipt['inserted'] = [item.get('node_id') for item in
                               (applied.get('insertions') or []) if item.get('node_id')]
        receipt['cycle'] = applied.get('cycle') or cycle_id
        receipt['acted'] = applied
        receipt['plan'] = plan(project, receipt['cycle']) if receipt['cycle'] else applied
        return receipt
    cycle_id = action.get('cycle') or cycle_id
    receipt['cycle'] = cycle_id
    if action.get('action') == 'dispatch' and cycle_id:
        # The material first (`apply` writes each cell's prompt and returns the same action); no
        # session is opened by `apply` itself.
        action = apply(project, cycle_id, action=action)
        receipt['acted'] = action
        cells = dispatch_cells(project, cycle_id, spawner=spawner, dry_run=dry_run)
        receipt.update(cells)
    elif action.get('action') == 'stop_pipeline' and not action.get('gate') and cycle_id:
        # A gate's refusal is a `stop_pipeline` too, and it is the one stop that must NOT park the
        # line: the gate is asking again on the next step, and an inactive line would answer
        # "line not in service" instead of asking.
        line = find_cycle(load_state(project), cycle_id)
        if line and line.get('status') == 'active' and not _holding_ids(line):
            apply(project, cycle_id, action={**action,
                                             'commands': [seam.cmd('cycle', 'status',
                                                                  cycle_id, 'inactive')]})
            receipt['parked'] = True
    receipt['plan'] = plan(project, cycle_id) if cycle_id else action
    return receipt
