"""The pipeline's front door: `plan`, `hold`, `settle`, `advance`, and `dispatch`.

One verb per operation, and each owns exactly its own step:

  * `cmd_plan`    -- read-only: what the next step is (and, with `--after <node>`, the closing
                     rehearsal of one node's settle). Writes nothing at all.
  * `cmd_hold`    -- claim the nodes this line will run: the one way work is taken.
  * `cmd_settle`  -- record one node's end: the step into the path, the claim laid down, the graph
                     mark, the close-time plugin seam. Pins nothing, opens nothing.
  * `cmd_advance` -- claim the free slots, land each cell's material, open one session per cell.
  * `cmd_dispatch`-- the session half on its own: open the sessions of the cells a line holds.

The decision logic itself lives in `conduct_pipeline.py`, loaded via `_load_pipeline()`. `dispatch`
lives next to `advance` on purpose: it is the same spawn call, under the same drill switch
(`DSH_CONDUCT_SPAWN_DRY_RUN`), against the same prompt path -- and a second file would be a second
place for that convention to drift.
"""

import json
import sys
from pathlib import Path

from .store import (_closed_steps_of_node, _cycles_holding_node, _holding_ids, _read_state,
                    _resolve_project_root)
from .cycles import claim_nodes as _claim
from .fork import gate_close_on_fork
from .pipeline_loader import _load_pipeline


# ── a node's own line ────────────────────────────────────────────────────────

def resolve_cycle_for_node(cycles: list, node: str) -> dict | None:
    """Which cycle is the one holding `node`? -- None if it is not decidable.

    The closing call names only the node: a worker should not have to learn cycle bookkeeping. The
    node names its own cycle, and this is where that is read.
    """
    holders = _cycles_holding_node(cycles, node)
    return holders[0] if len(holders) == 1 else None


def describe_holders(cycles: list, node: str) -> str:
    """A human sentence for why `--cycle` could not be resolved from the node."""
    holders = _cycles_holding_node(cycles, node)
    if len(holders) > 1:
        return (f'{node} is the node of several cycles '
                f'({", ".join(c["id"] for c in holders)}); name it with --cycle.')
    closed = _closed_steps_of_node(cycles, node)
    if closed:
        return (f'{node} is already closed on {closed}; there is nothing left to settle. '
                f'Reopen it, or start a new cycle.')
    heads = ', '.join(f'{c["id"]}:{", ".join(_holding_ids(c)) or "(none)"}' for c in cycles)
    return (f'{node} does not name a node any cycle is holding. '
            f'Every cycle holds {heads or "none"}.')


def _project(named_project) -> Path:
    """The project root a call means: `--project`, or cwd -- and never inside a linked worktree.

    cwd inside a linked worktree goes back to the main tree: in parallel mode the closing calls run
    from the worktree's cwd, while the record, the pick and the prompt all land in the main tree's
    records and scratch -- the worktree is only the code checkout.
    """
    return (Path(named_project).expanduser().resolve() if named_project
            else _resolve_project_root(Path.cwd()))


def _several(project: Path, pipeline) -> bool:
    """Whether a bare call is ambiguous: more than one line in service.

    Prints the refusal (the candidates, and the flag that names one) and answers True, so a caller
    stops there instead of guessing.
    """
    running = [c['id'] for c in pipeline.load_state(project).get('cycles', [])
               if c.get('status') == 'active']
    if len(running) > 1:
        print('Error: several cycles are active (' + ', '.join(running)
              + '); a bare call cannot pick one. Name the line: --cycle <id>', file=sys.stderr)
        return True
    return False


# ── plan ─────────────────────────────────────────────────────────────────────

def cmd_plan(args) -> None:
    """Print the single next action (and, with `--after`, one node's closing rehearsal).

    Read-only by construction: `pipeline.plan` writes nothing, and the renderer strips the
    mechanical steps of a preview before the answer goes out.
    """
    pipeline = _load_pipeline()
    project = _project(args.project)
    if args.cycle is None and args.after is None and _several(project, pipeline):
        sys.exit(2)
    try:
        action = pipeline.plan(project, args.cycle, after=args.after)
    except (RuntimeError, ValueError) as e:
        print(f'Error: {e}', file=sys.stderr)
        sys.exit(2)
    print(pipeline.render_action(action, args.json, preview=True))


# ── hold ─────────────────────────────────────────────────────────────────────

def cmd_hold(args) -> None:
    """Claim one or more nodes for a line: `hold <node…> [--cycle]`.

    Claiming is the one way work is taken. Several ids in one line are one write, all or none (see
    `cycles.claim_nodes`): every id is judged first, and the batch lands under one lock.
    """
    pipeline = _load_pipeline()
    project = _project(args.project)
    if args.cycle is None and _several(project, pipeline):
        sys.exit(2)
    nodes = [n for n in args.node if n]
    if not nodes:
        print('Error: `hold` needs at least one node id.', file=sys.stderr)
        sys.exit(2)
    state = pipeline.load_state(project)
    cycle_id = args.cycle
    if cycle_id is None:
        # A bare claim takes the one line in service; the node itself names no line (it may be
        # waiting for a line to take it), so the line's status is the only thing to read.
        service = [c['id'] for c in state.get('cycles', []) if c.get('status') == 'active']
        if len(service) != 1:
            print(f'Error: a bare `hold` needs exactly one line in service, and there are '
                  f'{len(service)} ({", ".join(service) or "none"}). Name the line: --cycle <id>. '
                  f'Nothing was claimed.', file=sys.stderr)
            sys.exit(2)
        cycle_id = service[0]
    elif pipeline.find_cycle(state, cycle_id) is None:
        print(f'Error: no cycle named {cycle_id!r}; nothing was claimed.', file=sys.stderr)
        sys.exit(2)
    try:
        _claim(cycle_id, nodes)
    except (RuntimeError, ValueError) as e:
        print(f'Error: {e}', file=sys.stderr)
        sys.exit(2)


# ── settle ───────────────────────────────────────────────────────────────────

def cmd_settle(args) -> None:
    """Record one node's end -- and nothing else.

    The gates first, each with its own sentence and no writes: the node names its line (or `--cycle`
    says which), the line holds it, and its ticket is on disk with a verdict. Then the plan's closing
    reading is written down: the step into `path`, the claim laid down, the graph mark, and the
    close-time plugin seam.

    `--dry-run` is the same reading with the effect shaped away: it reports the step, the graph mark
    and what the seam would insert, and writes not one byte.
    """
    pipeline = _load_pipeline()
    project = _project(args.project)
    state = pipeline.load_state(project)
    cycles = state.get('cycles', [])
    if args.cycle:
        cycle = pipeline.find_cycle(state, args.cycle)
        if cycle is None:
            print(f'Error: no cycle named {args.cycle!r}; nothing was settled.', file=sys.stderr)
            sys.exit(2)
        cycle_id = args.cycle
    else:
        cycle = resolve_cycle_for_node(cycles, args.node)
        if cycle is None:
            print(f'Error: {describe_holders(cycles, args.node)} Nothing was settled.',
                  file=sys.stderr)
            sys.exit(2)
        cycle_id = cycle['id']
    if args.node not in _holding_ids(cycle):
        print(f'Error: {args.node} does not name a node this cycle is holding '
              f'({"+".join(x[:8] for x in _holding_ids(cycle)) or "none"} of cycle '
              f'{cycle["id"]}). Nothing was settled. '
              f'Name the node whose work you are closing.', file=sys.stderr)
        sys.exit(2)
    try:
        # The parallel mode's backstop: a node that still carries a worktree may only be settled once
        # the merge-back is finished (worktree and exit branch both gone). A node with no worktree
        # passes straight through -- git is not even asked.
        gate_close_on_fork(project, args.node)
        if args.dry_run:
            # The rehearsal is the plan's closing reading, printed as printed: the same action the
            # writing word would carry out, with the effect shaped away.
            print(pipeline.render_action(pipeline.plan(project, cycle_id, after=args.node),
                                         args.json, preview=True))
            return
        # The writing word: `pipeline.settle` plans that same reading and applies it. A `wait` /
        # `ask_user` answer (no ticket yet, or the line does not hold this node) carries no commands,
        # and `settle` hands it back as it stands -- nothing is written.
        action = pipeline.settle(project, cycle_id, args.node)
    except (RuntimeError, ValueError) as e:
        print(f'Error: {e}', file=sys.stderr)
        sys.exit(2)
    print(pipeline.render_action(action, args.json))


# ── advance ──────────────────────────────────────────────────────────────────

def cmd_advance(args) -> None:
    """Claim, land the material, open the sessions -- the composed word, in that order.

    It settles nothing (that is `settle`) and it never moves a node's status; the one line-level
    write is the plan's own answer, turning a line `inactive` when its plan stopped with nothing in
    hand. With no opener behind it, the session half answers `spawn_error` and claims nothing.
    """
    pipeline = _load_pipeline()
    project = _project(args.project)
    if args.cycle is None and _several(project, pipeline):
        sys.exit(2)
    try:
        receipt = pipeline.advance(project, args.cycle, spawner=args.spawner)
    except (RuntimeError, ValueError) as e:
        print(f'Error: {e}', file=sys.stderr)
        sys.exit(2)
    print(pipeline.render_advance(receipt, args.json))


# ── dispatch ─────────────────────────────────────────────────────────────────

def cmd_dispatch(args) -> None:
    """Open the sessions of the cells this line holds, and nothing else.

    `args` is the parsed `dispatch` line (state-manage's namespace), read here field by field.

    The prompt this verb starts is landed by whichever step PREPARES the cell -- usually `advance`,
    whose `apply` writes each cell's prompt to `.superworker/meta/launch/<cycle>/<node_id>/prompt.txt`.
    The worker that settled its node still has to commit and push its own work, and the next session
    boots in the SAME worktree and pulls on its first line, so starting it before the push would
    hand it a half-finished tree. The two halves are therefore two words:

        sw settle <node> --json          record this node's end
        git commit … && git push         the worker's own work
        sw advance --cycle <cycle>       claim, material, session -- or `dispatch <cycle>`

    It only ever READS the ledger and starts the sessions: no `status`, no `path`, no `holding`, no
    writes anywhere except the spawner's own `.superworker/meta/` bookkeeping (the stale-marker
    cleanup, which lives in `dsh-spawn.ps1` and is untouched here) and the `started` stamp. A real
    spawn drops the delivered launch directory's prompt, exactly as `advance` does;
    `DSH_CONDUCT_SPAWN_DRY_RUN=1` reports the call without opening a session and keeps the prompt
    file, which is what a human retries by hand.

    The one question it asks of the tree before spawning is the dispatch gate (`pipeline.gates`): the
    plugins installed at that timing say whether a session may open now, and this verb is a dispatch
    -- skipping it here would leave `sw dispatch` as the way around a gate. A refused gate writes
    nothing, opens nothing and leaves the line in service, exactly like `advance`'s refusal.
    """
    pipeline = _load_pipeline()
    cycle_id, as_json, spawner, named_project = args.id, args.json, args.spawner, args.project
    root = _project(named_project)
    try:
        state = pipeline.load_state(root)
    except (RuntimeError, ValueError) as e:
        print(f'Error: {e}', file=sys.stderr)
        sys.exit(2)
    cycle = pipeline.find_cycle(state, cycle_id)
    if cycle is None:
        print(f'Error: no cycle named {cycle_id!r} in {pipeline.ledger_dir(root)}; nothing to '
              f'dispatch.', file=sys.stderr)
        sys.exit(2)
    # The account carries exactly the two words, so this one comparison is the whole judgement.
    if cycle.get('status') != 'active':
        print(f'Error: cycle "{cycle_id}" is {cycle.get("status")!r}, not active: a line that is not '
              f'in service does not dispatch. `sw cycle status {cycle_id} active` '
              f'puts it back.', file=sys.stderr)
        sys.exit(2)
    holding = _holding_ids(cycle)
    if not holding:
        print(f'Error: cycle "{cycle_id}" is holding no node, so there is nothing to dispatch. '
              f'`sw advance --cycle {cycle_id} --json` claims the next one and lands its prompt.',
              file=sys.stderr)
        sys.exit(2)
    # The dispatch gate belongs HERE as much as on `advance`'s path: "may a session open now?" is
    # the dispatch moment's own question, and this verb is a dispatch. Asked before any spawn, so a
    # refused dispatch writes nothing and opens nothing -- the line keeps its hand and stays in
    # service (`advance` asks the same gate again on the next step). Read-only: one plugin folder
    # scan, no writes.
    blocked = pipeline.gates(
        'dispatch', pipeline.Context(root, cycle=cycle, node_id=holding[0]))
    if blocked:
        print('Error: a plugin gate blocked this dispatch: ' + '; '.join(blocked), file=sys.stderr)
        sys.exit(2)
    try:
        receipt = pipeline.dispatch_cells(root, cycle_id, spawner=spawner)
    except (RuntimeError, ValueError) as e:
        # The same shape as `advance`: the spawner's failure is reported and its own marker (written
        # by dsh-spawn.ps1) is left exactly as it is. What did start stays started; the retry is
        # this same command, and the SPAWN.claim of every started node refuses a re-spawn.
        print(f'Error: {e}', file=sys.stderr)
        sys.exit(3)
    if not receipt['spawned']:
        if receipt['unprompted']:
            print(f'Error: no prompt file for {", ".join(x[:8] for x in receipt["unprompted"])}. '
                  f'That file is landed by the step that prepares the cell: '
                  f'`sw advance --cycle {cycle_id} --json`.', file=sys.stderr)
        else:
            named = ', '.join(x[:8] for x in receipt['skipped']) or '(none)'
            print(f'Error: every node this cycle holds already had its session started ({named}), '
                  f'so there is nothing to dispatch: doing it again would boot a second session '
                  f'onto the same node. To reopen one, retry it the documented way: '
                  f'`sw cycle retry {cycle_id} <node> --drop`, then '
                  f'`sw hold <node> --cycle {cycle_id}`, then '
                  f'`sw advance --cycle {cycle_id} --json`.', file=sys.stderr)
        sys.exit(2)
    if as_json:
        print(json.dumps({'action': 'dispatch', 'cycle': cycle_id,
                          'nodes': [n for n in holding if n not in receipt['skipped']],
                          'spawned': receipt['spawned'],
                          'skipped': receipt['skipped']},
                         ensure_ascii=False, indent=2))
    else:
        print('\n'.join(receipt['spawned']))
