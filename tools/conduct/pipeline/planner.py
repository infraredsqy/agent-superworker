"""`plan()` and the settle of one node.

The stop rule, the read-only planner that returns the one next action, and the settle a worker's
own `settle <node>` performs.
"""

from __future__ import annotations

from pathlib import Path

from . import plugin_host, seam
from .store import (_holding_ids, active_cycles, cycle_policy, find_cycle,
                    load_dag, load_signal, load_state, normalize_mode)
from .instruct import live_instructions
from .nodes import (blocked_reason, holding_ready_nodes, mode_for, node_in_tag_scope, node_row,
                    pick_node, ranked_ready_nodes, tag_blocked_ready_nodes)
from .prompts import started_marker
from .actions import (_ask, _close_commands, _dispatch_many, _insertions_due,
                      _pick_insertions_due, _retry_wait, _sessions_wait, _ticket_wait,
                      _stop_action, attention_items)


def stop_criterion(project: Path, cycle: dict, node: dict | None, dag: dict | None = None) -> tuple:
    """(must_stop, why). Invariant: instructions may only TIGHTEN the stop rule.

    **Whether this line runs on is decided by the node's own mode alone, and the human gate sits in
    a different place in each of the three tiers.** `auto` has no human gate: the close opens the
    next node itself; `manual`'s gate is in the slot's session -- the whole slot is a back-and-forth
    with the user, and `completed` on the note is the user's own "mark it done, carry on", so the
    closing beat raises no second gate; `checkpoint`'s gate is on the closing beat: one round runs
    and stops, and the next node opens only once the human has said continue.

    `_settle_plan` uses this same rule for the session's own closing call, and nothing a caller says
    can pry it open: the settle takes no `--continue` / `--stop`, so the rule is the only voice.
    `dag` is the one graph read at the close entry, handed to the instruction pass's scope judgement
    (the criterion reads the node's status, and the graph is not read again).
    """
    mode = normalize_mode((node or {}).get('mode'))
    if mode == 'checkpoint':
        return True, ('node mode=checkpoint: this round is done, and the next node opens only once '
                      'you say continue')
    for ins in live_instructions(project, target='boundary', cycle=cycle.get('id'),
                                 node=(node or {}).get('id'), dag=dag):
        cap = ins.get('stop_after_nodes')
        if isinstance(cap, int) and cap >= 0:
            # "stop after N": this close is the Nth -- by now all N nodes have been delivered.
            done = len(cycle.get('path') or []) + 1
            if done >= cap:
                return True, f'instruction "{ins.get("text", "")[:40]}": stop after {cap} nodes'
    return False, ''


# ── the pure planner ─────────────────────────────────────────────────────────

def plan(project, cycle_id: str | None = None, after: str | None = None) -> dict:
    """Read state and return the single next action. Writes nothing.

    Two readings, one name: with `after` the question is the closing one -- this node's ticket is on
    disk, and settling it, what would the close record (`_settle_plan`, the rehearsal `sw plan
    --after` prints). Without it the answer is the step `sw advance` would take: pin the cells the
    line has free slots for, dispatch the cells whose material is still missing, or wait.

    `after` names the node the close is about: a line can hold several at once, so "which node is
    this close about" is an argument, not something the cycle's hand can answer. Absent it, the one
    node in hand is taken: a line holding only one has no ambiguity to read.

    **Two gates, each minding its own business**:
      * `settle <node>` is the gate through which a session closes its own node, and it **does not
        look at the line's status**: the node itself says which line it belongs to (on the CLI side
        `resolve_cycle_for_node` settles that), and a stopped line that could not close even its own
        node would leave that node pinned there forever. When the line is not active this gate only
        records -- see `_settle_plan`: it writes not one word of status, pins no node and opens
        nothing.
      * No other step drives a line that is not active: it pins nothing, opens nothing, and only
        reports the one command that puts the line back in service. The close path is the only gate
        into an inactive line.
    """
    project = Path(project).resolve()
    state = load_state(project)

    cycle = find_cycle(state, cycle_id)
    if cycle is None:
        if cycle_id:
            return _ask(f'no cycle named {cycle_id!r}; known cycles: '
                        f'{[c["id"] for c in state.get("cycles", [])]}', requested=cycle_id)
        return _no_line(state)

    if cycle.get('status') != 'active' and after is None:
        return _ask(f'cycle {cycle["id"]!r} exists but is not active ({cycle.get("status")})',
                    requested=cycle['id'], candidates=[cycle['id']],
                    hint=f'sw cycle status {cycle["id"]} active')

    tags = cycle.get('tags') or []
    holding = _holding_ids(cycle)

    # No node in hand: the cycle is starting, or every node it carried has been closed. Either way
    # the only question is what the DAG has, and the answer is the same rule for both.
    if not holding:
        return _pick_next(project, cycle, tags)

    if after is not None:
        # The worker's own closing call: settle the node whose signal it already wrote. The other
        # nodes this line holds stay in hand -- each is closed by its own worker's call.
        closing = after or (holding[0] if len(holding) == 1 else None)
        if closing is None or closing not in holding:
            return {'action': 'wait', 'cycle': cycle['id'], 'commands': [],
                    'note': f'{closing[:8] if closing else "(unnamed)"} is not in the hand of '
                            f'{cycle["id"]} (it is holding '
                            f'{", ".join(x[:8] for x in holding)}) -- there is nothing to settle.'}
        sig = load_signal(project, cycle['id'], closing)
        # The worker's own closing call IS the request to close, so a ticket that already says how it
        # went is enough: `completed` and `failed`/`stopped` are both answers, and one of them is not
        # a reason to wait. Only a ticket that is absent or still `pending` leaves nothing to settle.
        if sig is None or sig.get('status') == 'pending':
            return {'action': 'wait', 'cycle': cycle['id'], 'commands': [],
                    'node_id': closing,
                    'note': f'{closing[:8]} has not handed in a note yet '
                            f'(signals/{cycle["id"]}/{closing[:8]}….json is absent, or still '
                            f'pending) -- there is nothing to settle.'}
        return _settle_plan(project, cycle, closing, sig,
                            line_active=cycle.get('status') == 'active')

    # Every node in hand has a terminal ticket: the batch is over, and the close is what consumes
    # them -- one per call, the same stable state there has always been. A node whose ticket has
    # not been written yet is dispatched (its session is already the thing that writes it); a
    # `pending` ticket is the retry case (a cleared signal), and there is nothing to start for it.
    sigs = {x: load_signal(project, cycle['id'], x) for x in holding}
    terminal = [x for x in holding if (sigs[x] or {}).get('status') not in (None, 'pending')]
    if terminal:
        return _ticket_wait(cycle, terminal[0], holding)
    fresh = [x for x in holding if sigs[x] is None]
    waiting = [x for x in fresh if not started_marker(project, cycle['id'], x).is_file()]
    if waiting:
        # Freshly pinned nodes are what a dispatch is for. A node whose session already runs is left
        # out of the list: handing it to the spawner again would boot a second session onto it.
        dag = load_dag(project)
        gated = _plugin_gate(project, cycle, waiting, dag)
        if gated is not None:
            return gated
        return _dispatch_many(project, cycle, waiting, dag=dag)
    if fresh:
        # The slots a settled node freed: a line wider than one keeps itself full here, on the same
        # ranking a first pick uses. Nothing ready means the running cells are all there is.
        dag = load_dag(project)
        slots = max(cycle_policy(cycle)['parallel'] - len(holding), 0)
        if slots:
            filled = _fill_slots(project, cycle, holding, slots, dag, fresh)
            if filled is not None:
                return filled
        return _sessions_wait(cycle, fresh, holding)
    return _retry_wait(cycle, holding[0], holding)


def _fill_slots(project: Path, cycle: dict, holding: list, slots: int, dag: dict,
                fresh: list) -> dict | None:
    """Pin the cells this line has room for -- or `None` when the ranking has nothing to add.

    The one reading behind the fill: the ready set in the graph's order, this line's own hand and
    every other line's hand taken out, cut to the free slots. The dispatch gate and the pick-time
    insertion seam are asked exactly where a first pick asks them, because this IS a pick, made on a
    line that already holds cells.
    """
    ready = ranked_ready_nodes(project, limit=slots, exclude_many=holding,
                               tags=cycle.get('tags') or [], dag=dag)
    if not ready:
        return None
    gated = _plugin_gate(project, cycle, [n['id'] for n in ready], dag)
    if gated is not None:
        return gated
    offers = _plugin_pick_insertions(project, cycle, dag, ready[0])
    if offers:
        action = _pick_insertions_due(project, cycle, offers, ready[0])
        if action is not None:
            return action
    return _open(project, cycle, ready)


def _no_line(state: dict) -> dict:
    """The sentence for when there is no line to drive: none at all, or lines but none in service.

    The two cases must read differently. Saying "no cycle" while a stopped line is plainly sitting on
    disk (perhaps with an unfinished node still pinned) sends the reader off to start a new one, when
    what should happen is putting that one back in service.
    """
    cycles = state.get('cycles', [])
    in_service = [c['id'] for c in active_cycles(state)]
    if len(in_service) > 1:
        return _ask('several active cycles: continue which one?', candidates=in_service)
    if not cycles:
        return {'action': 'no_cycle', 'cycle': None, 'commands': [],
                'note': 'no active cycle. Creating one needs human intent (which nodes, '
                        'unattended or not); this planner will not invent it.',
                'known_cycles': []}
    idle = [c['id'] for c in cycles]
    if len(idle) == 1:
        only = idle[0]
        return _ask(f'no cycle is active; {only!r} is out of service (inactive)',
                    requested=only, candidates=idle, known_cycles=idle,
                    hint=f'sw cycle status {only} active')
    return _ask(f'no cycle is active right now; these lines are all out of service: {idle}',
                candidates=idle, known_cycles=idle,
                hint='sw cycle status <cycle> active')


def _pick_next(project: Path, cycle: dict, tags) -> dict:
    """The cycle has no node in hand: name the next one(s), or stop for a reason a human can act on.

    Several ready rows are not a question: the ranking decides unattended at every close
    (`pick_node` takes the top row), so a line's first pick takes the same rows the fill would.
    The line fills the slots its `parallel` policy allows -- one pin per slot, in ranking order --
    and the pipeline never stops here: a human who wants a different one names it (`--node`,
    `hold`). A plugin may put a node in front of the pick (`_plugin_pick_insertions`); that
    answer is a graph edit with no pins, so the pick itself is then made on the next step, with the
    inserted node in the same ranking.
    """
    slots = cycle_policy(cycle)['parallel']
    dag = load_dag(project)
    ready = ranked_ready_nodes(project, limit=slots, tags=tags, dag=dag)
    if ready:
        # A gate sits at the dispatch, and pinning is the first half of one: asked before the pins so
        # a refused dispatch leaves nothing pinned behind it.
        gated = _plugin_gate(project, cycle, [n['id'] for n in ready], dag)
        if gated is not None:
            return gated
        # The other timing an insertion is allowed at: asked here, after the ready set is known and
        # before anything is pinned, so the nodes a plugin wants enter the graph ahead of the pick
        # instead of behind it. What comes back pins nobody -- the next step picks by ranking, and
        # the inserted node is in that ranking.
        offers = _plugin_pick_insertions(project, cycle, dag, ready[0])
        if offers:
            # `None` = every offer was skipped (a plugin whose text builder raises): the pick is
            # then the ordinary one, exactly as if nothing had asked.
            action = _pick_insertions_due(project, cycle, offers, ready[0])
            if action is not None:
                return action
        return _open(project, cycle, ready)
    tagged = tag_blocked_ready_nodes(project, tags, dag=dag)
    holding = holding_ready_nodes(project, dag=dag)
    if tagged or holding:
        return _stop_action(project, cycle, blocked_reason(project, tags, tagged, holding),
                            _park_commands(cycle), dag=dag)
    return _stop_action(project, cycle, 'the DAG has no ready node left', _park_commands(cycle),
                        dag=dag)


def _open(project: Path, cycle: dict, nodes: list) -> dict:
    """Point the cycle at one or more nodes: the claim's own reading, ready to be applied.

    `hold` is the word that writes it, one call per node, and `apply` runs them. The dispatch that
    follows is a separate reading of the same state -- claiming and opening are two words.
    """
    nodes = list(nodes)
    head = nodes[0]
    titles = ', '.join(n.get('title') or n['id'] for n in nodes)
    return {'action': 'open_node', 'cycle': cycle['id'],
            'node_id': head['id'], 'node_title': head.get('title', ''),
            'nodes': [n['id'] for n in nodes],
            'note': f'cycle {cycle["id"]} holds {titles}',
            'commands': [seam.cmd('hold', n['id'], '--cycle', cycle['id']) for n in nodes]}


def _park_commands(cycle: dict) -> list:
    """Stopping is one sentence: turn this line `inactive`.

    There is no paused / completed fork: whether a stopped line should run on is a human's call --
    a graph grows and its work multiplies, so no status can answer it. The tool never writes
    `active`.
    """
    return [seam.cmd('cycle', 'status', cycle['id'], 'inactive')]


def _settle_plan(project: Path, cycle: dict, node_id: str, sig: dict,
                 line_active: bool = True) -> dict:
    """Record one node's end: the step, the graph mark, the close-time seam. Nothing else.

    **A settle records; it does not advance.** It pins no node, lands no prompt, opens no session
    and moves no slot -- the next node is `advance`'s step (or a human's `hold`), and the session
    that runs it is `dispatch`'s. What it does own is the line's one stop decision: a `checkpoint`
    round ends at its close, a non-delivered node the `when_shelved` policy says stop ends it, and a
    graph with nothing ready ends it -- so a line whose round is over is turned `inactive` here, in
    the same call that records the node. The criterion is the node's own `mode` and the line's own
    policies; there is no `--continue` / `--stop` for a caller to contradict it with (why only a
    human writes `active`, and why the gate sits here, see `docs/conduct-state.md` "the two ways
    in").

    The outcome comes from the signal the worker already wrote ("completed" = it delivered,
    "failed"/"stopped" = it did not), so there is always an answer and nobody to ask. The verdict
    word goes onto the line's path, the graph row lands `completed` / `shelved`.

    The other nodes this line holds stay in hand -- each one is settled by its own worker's call,
    and a settle only ever moves its own node. The slot this close frees is filled by the next
    `advance` (`plan`'s free-slot reading), not here: what this pass reads the ready set for is the
    decision ("is there something to carry on to") and the close seam's target.

    `line_active=False` -- this line is not in service (a human turned it inactive): closing is still
    the session's right (otherwise that node would stay pinned forever), but it records only -- not
    one word of the line's status is written, and it keeps the status the human gave it.

    This closing pass **reads the graph once** (the `dag` below) and hands it down as an argument:
    the stop rule (the node's own mode, plus the instruction pass's scope judgement), the list the
    decision reads, the blocked reason, the plugins' insertion judgement and the attention list all
    read it that one time.
    """
    dag = load_dag(project)
    delivered = sig.get('status') == 'completed'
    policy = cycle_policy(cycle)
    policy_carries = policy.get('when_shelved') == 'continue'
    carry_on = delivered or policy_carries
    others = [x for x in _holding_ids(cycle) if x != node_id]
    if not line_active:
        carry_on = False
    decision = 'continue' if carry_on else 'stop'
    tags = cycle.get('tags') or []
    parallel = policy['parallel']
    slots = max(parallel - len(others), 0)
    several = parallel > 1

    # The close commands have not run yet, so the DAG still has this node `todo`. Reading it as
    # completed is what un-blocks its dependents for this one question -- excluding it would only
    # hide the node, and the reading would then miss the work it just un-blocked.
    assumed = [node_id] if delivered else []

    # ── what the line would carry on to: read for the decision, never pinned here ──
    filled: list = []
    if carry_on and decision == 'continue' and slots:
        filled = ranked_ready_nodes(project, limit=slots, exclude=node_id,
                                    exclude_many=others, assume_completed=assumed,
                                    tags=tags, dag=dag)

    # ── the stop rule: where it sits is the parameter above ──────────────────
    stop_why = ''
    if carry_on and (not several or not filled):
        # A one-slot line's close is its whole round, so the gate is asked whatever the graph has
        # next. A line holding several asks only when nothing was filled: a slot that CAN be
        # refilled carries the line on regardless.
        must_stop, why = stop_criterion(project, cycle, node_row(dag, node_id), dag=dag)
        if must_stop:
            decision, stop_why = 'stop', why

    outcome = 'completed' if delivered else (sig.get('status') or 'failed')
    # The node this close shelves has to be named by this package itself: the DAG command has not
    # run yet, so on disk it is still todo.
    # (`outcome` is the note's verdict, `shelved` is a node status -- each has its own recipient, so
    # do not mix them.)
    shelved = [] if delivered else [node_id]

    if not line_active:
        # A line that is not in service: records only. Close this node, mark the DAG, then stop --
        # not even the line's status is written.
        reason = (f'cycle {cycle["id"]!r} is not active ({cycle.get("status")}): a close on this '
                  f'line only records -- it closes {node_id[:8]} and marks the DAG, pins no next '
                  f'node and opens nothing; the line keeps the status it was given.')
        reason += f' To carry on: sw cycle status {cycle["id"]} active'
        cmds = _close_commands(project, cycle, node_id, outcome)
        return _settle_action(project, cycle, node_id, outcome, delivered, cmds, parked=False,
                              reason=reason, shelved=shelved, dag=dag)

    if decision == 'stop' or not filled:
        # With other nodes still in flight this line is not stopped: those nodes are still held, so
        # the line has not stopped, and the baton is on their own closes -- parking it would make
        # their closes record-only, and the work they free would have nobody to take it up.
        park_line = decision == 'stop' or not others
        cmds = _close_commands(project, cycle, node_id, outcome, park=park_line)
        open_notes = (f' (still holding {", ".join(x[:8] for x in others)}, their notes are '
                      f'collected as they arrive)' if decision == 'stop' and others else '')
        if not delivered and not policy_carries:
            slipped = sig.get('summary') or '(no reason written)'
            reason = f'this node did not deliver ({outcome}): {slipped}'
        elif stop_why:
            reason = stop_why
            # The middle tier stops to wait for a human's "continue" -- stopping turns the line
            # inactive, so hand over both commands (back in service, and carry on); otherwise
            # "continue" would have to guess how to continue.
            if mode_for(node_id, dag) == 'checkpoint':
                reason += (f'; to carry on: sw cycle status {cycle["id"]} active, '
                           f'then sw advance --cycle {cycle["id"]} --json')
        elif not filled and carry_on:
            # Nothing is being filled: either this line still holds work (the baton is on those
            # closes), or the graph has nothing it can pick -- and then say which of the two.
            if others:
                reason = (f'{node_id[:8]} is settled; still holding '
                          f'{", ".join(x[:8] for x in others)}, their notes are collected as they '
                          f'arrive (sw settle <node id> --json) -- this line stays in service, and '
                          f'the next advance fills the freed slot.')
            else:
                # What a one-slot line's close counts as delivered is this node: the dependents it
                # just unblocked are the work the reader is looking for. A list close reads the
                # graph as it stands on disk.
                blocked_assumed = assumed if not several else ()
                tagged = tag_blocked_ready_nodes(project, tags, exclude=node_id,
                                                 exclude_many=others,
                                                 assume_completed=blocked_assumed, dag=dag)
                holding = holding_ready_nodes(project, exclude=node_id, exclude_many=others,
                                              assume_completed=blocked_assumed, dag=dag)
                if tagged or holding:
                    reason = blocked_reason(project, tags, tagged, holding)
                else:
                    reason = 'the graph has no ready node left'
        else:
            reason = f'stopping at the close: {stop_why or "the round is over"}'
        if open_notes:
            reason += open_notes
        return _settle_action(project, cycle, node_id, outcome, delivered, cmds,
                              parked=park_line, reason=reason, shelved=shelved, dag=dag)

    # ── the close-time seam: what the plugins want in front of the next node ──
    # The plugins' one judgement, at the one moment it can be made: the settle that closes a node is
    # the only place a reading and the node this close would run next are both in hand. With nothing
    # configured no plugin offers anything and this whole pass costs one dict lookup. An inserted
    # node is wired in front of `filled[0]` -- the next node this line would pick -- so the ordinary
    # ranking takes it first. An insertion pins nothing and opens nothing, exactly like the settle
    # it rides on.
    offers = _plugin_insertions(project, cycle, node_id, sig, dag, filled[0])
    if offers:
        settled_cmds = _close_commands(project, cycle, node_id, outcome)
        inserted = _insertions_due(project, cycle, node_id, offers, filled[0],
                                   close_commands=settled_cmds)
        if inserted is not None:
            titles = ', '.join(item.get('title') or '(untitled)'
                               for item in inserted.get('insertions') or [])
            reason = (f'{node_id[:8]} settled; {len(inserted.get("insertions") or [])} node(s) '
                      f'inserted in front of the next pick ({titles}) -- commit and push, then '
                      f'run `sw advance --cycle {cycle["id"]} --json`')
            action = _settle_action(project, cycle, node_id, outcome, delivered,
                                    list(inserted.get('commands') or []), parked=False,
                                    reason=reason, shelved=shelved, dag=dag, inserted=inserted)
            return action
        # `None` = every offer was skipped (a plugin whose text builder raises): this close goes on
        # as the ordinary one below. One broken folder must not freeze the line at its close.

    cmds = _close_commands(project, cycle, node_id, outcome)
    following = filled[0].get('title') or filled[0]['id']
    reason = (f'{node_id[:8]} settled; {following} ({filled[0]["id"]}) is what this line would run '
              f'next -- commit and push, then run `sw advance --cycle {cycle["id"]} --json`')
    return _settle_action(project, cycle, node_id, outcome, delivered, cmds, parked=False,
                          reason=reason, shelved=shelved, dag=dag)


def _settle_action(project: Path, cycle: dict, node_id: str, outcome: str, delivered: bool,
                   cmds: list, *, parked: bool, reason: str, shelved=(), dag: dict | None = None,
                   inserted: dict | None = None) -> dict:
    """One node's settle as the caller reads it: what was recorded, and whether the line parked.

    The shape is deliberately neither a dispatch nor a stop package: a settle records one node's end
    and moves nothing else. `parked` is the one line-level fact it carries -- the stop rule said this
    round was over and the line was turned `inactive` -- and `note` says why, in the rule's own
    words. `inserted` is the insertion action the close seam built (a plugin's offer); its graph
    edits ride ahead of this close's own commands, and its minted uuid is filled by the writer.
    """
    title = (node_row(dag or {}, node_id) or {}).get('title', '')
    action = {
        'action': 'settle', 'cycle': cycle['id'], 'node_id': node_id, 'node_title': title,
        'result': outcome, 'delivered': delivered,
        'graph': 'completed' if delivered else 'shelved',
        'parked': parked, 'note': reason,
        'attention': attention_items(project, cycle, shelved, dag=dag),
        'commands': cmds,
    }
    if inserted is not None:
        # The insertion action's reader-facing cells, mirrored onto the settle it rides on: the
        # renderer prints the first insertion from these top-level keys and the rest from
        # `insertions[1:]`, so a `why` / `before_node` left behind would be a line nobody prints.
        for key in ('insertions', 'steps', 'spec', 'add_command', 'why', 'before_node',
                    'before_title'):
            if key in inserted:
                action[key] = inserted[key]
        # The insertion's own `node_id` is the NEW_NODE placeholder until `apply` mints it; the
        # settled node keeps its own id on this action, so the two never collide.
        action['inserted_node'] = inserted.get('node_id')
    return action


def _plugin_pick_insertions(project: Path, cycle: dict, dag: dict,
                             target: dict | None = None) -> list:
    """The insertions the plugins want at a PICK, in registry order (empty is the common case).

    The second of the two timings an insert action may occupy (`plugin_registry.TIMINGS` has three
    cells for three timings; a plugin declares exactly one). A pick has no node being closed and no
    ticket to read, so what a plugin is handed here is the line, the graph read for the pick and the
    node the ranking chose -- nothing else. That is also why `node_id` / `sig` are left out: a
    plugin that ran at a close and one that runs at a pick are different folders with different
    declarations, and this one has no ticket to be handed.
    """
    ctx = plugin_host.Context(project, cycle=cycle, dag=dag, target=target)
    return plugin_host.insertions('pick', ctx)


def _plugin_insertions(project: Path, cycle: dict, node_id: str, sig: dict | None, dag: dict,
                        target: dict | None = None) -> list:
    """The insertions the plugins want at this close, in registry order (empty is the common case).

    The whole judgement is `plugin_host.insertions`: this only states WHICH state a plugin is
    handed -- the line, the node being closed, that node's ticket, the graph read at the close entry
    and the node the close had picked. A line with nothing configured gets an empty list here, and
    that is what keeps the close byte-for-byte what it was before plugins existed.

    The `dag` handed down is the one read at the close entry: a plugin asking "does this metric
    already have a node open" reads that copy, not the disk again.
    """
    ctx = plugin_host.Context(project, cycle=cycle, node_id=node_id, sig=sig, dag=dag,
                               target=target)
    return plugin_host.insertions('close', ctx)


def _gate_reasons(project: Path, cycle: dict, node_ids, dag: dict | None = None) -> list:
    """The dispatch gate's answers: one sentence per plugin that says not now; empty is a go.

    The one place the gate is asked, whichever path is asking -- the pick before its pins, the close
    before the fill it hands over, and the dispatch the writer builds from either. The context
    carries the node that would be dispatched, which is what a gate judging "may this node's session
    open now?" reads.
    """
    ctx = plugin_host.Context(project, cycle=cycle, dag=dag,
                               node_id=(list(node_ids) or [None])[0])
    return plugin_host.gates('dispatch', ctx)


def _gate_says(reasons) -> str:
    """A refused dispatch's opening sentence: the gate's own words, verbatim, one per plugin."""
    return 'a plugin gate blocked this dispatch: ' + '; '.join(reasons)


def _plugin_gate(project: Path, cycle: dict, node_ids, dag: dict | None = None) -> dict | None:
    """The stop answer when a plugin gate refuses this dispatch, or `None` for a green light.

    A gate sits at the dispatch -- the one moment "does anybody mind another session opening now?"
    can be answered -- and **it is asked before anything is pinned or written, on every path**: the
    pick asks it before its pins (`_pick_next`), the free-slot fill asks it before its own pins
    (`_fill_slots`), and the dispatch the writer builds from either comes after. A refused dispatch
    therefore leaves no node held, no prompt file and no session.

    **A refused dispatch does not park the line.** The contract's own example is a gate about time,
    and the pipeline is not a resident process: whatever wakes it only knows how to run one more
    step. Taking the line out of service would make that step answer "line not in service", which
    kills the gate's whole purpose -- so this answer carries no commands and says the line stays in
    service; the next step simply asks the gate again. The words name the gate(s) that refused, so a
    human can act on the reason rather than on the tool.
    """
    reasons = _gate_reasons(project, cycle, node_ids, dag)
    if not reasons:
        return None
    answer = _stop_action(
        project, cycle,
        _gate_says(reasons) +
        '. No session was opened, no prompt was written and nothing was pinned; the line stays in '
        'service, and the next step will ask the gate again.',
        [], dag=dag, stop_line=False)
    # Which stop this is: a gate's refusal reads like a plan that stopped, and the writer must not
    # park the line on it (the line is deliberately left in service so the next step asks again).
    answer['gate'] = True
    return answer


def _named_node_outside_scope(dag: dict, node_id: str, tags) -> list | None:
    """Does the node named by `--node <id>` carry a tag the filter set does not recognize? If so,
    report it back."""
    if not tags:
        return None
    node = (dag.get('nodes') or {}).get(node_id)
    if not node:
        return None
    node_tags = [t for t in (node.get('tags') or []) if isinstance(t, str)]
    if not node_tags or node_in_tag_scope(node, tags):
        return None
    return node_tags
