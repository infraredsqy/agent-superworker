"""The session roles and the driver that plays them.

`WorkerSession` is a node's worker; `Driver` plays the flow the engine asks for -- the host runs
`advance`, the worker reports, the worker's own `settle` closes the node it ran; `finish_node` is the
shared convenience builder.
"""

from __future__ import annotations

from harness_env import TERMINAL_ACTIONS
from harness_sandbox import Sandbox

__all__ = ['WorkerSession', 'Driver', 'finish_node']


# ═══════════════════════════════════════════════════════════════════════════════
#  The role a session plays
# ═══════════════════════════════════════════════════════════════════════════════

class WorkerSession:
    """One node's session: do the work (write a real deliverable), then run its ONE command.

    A node's reports live in the node's own ledger directory by convention -- nothing validates them
    any more -- so the worker writes its deliverable under `.superworker/nodes/<node>/`.
    """

    def __init__(self, sb: Sandbox, cycle='cyc', via_handoff=False, fail_nodes=(),
                 fail_status='failed'):
        self.sb = sb
        self.cycle = cycle
        self.via_handoff = via_handoff   # True: use handoff.py (the real ticket) as a process
        self.fail_nodes = set(fail_nodes)
        #: The status a failing node reports. `failed` is "I delivered nothing"; `stopped` is
        #: "the user called it off" -- both are the worker saying it did not deliver.
        self.fail_status = fail_status

    #: How many times this worker has reported, so `max_steps` can be checked in a test.
    reports = 0

    def outputs_for(self, action, deliverable, node):
        return {'deliverable': deliverable,
                'acceptance': [f'{action.get("skill") or "sw-conduct"} core acceptance points'],
                'touched': [deliverable]}

    def work(self, action):
        """Write what the signal will point at, then report it.

        Accepts a `wait` as well as a `dispatch`: a node whose signal is already `running` is
        reported on by the worker the planner is waiting for -- the planner's word for that state
        is `wait`.
        """
        assert action['action'] in ('dispatch', 'wait'), f'not a node to work on: {action}'
        node = action.get('node_id') or self.sb.current_node(self.cycle)
        assert node, f'no node to work on: {action}'
        skill = action.get('skill') or 'sw-conduct'
        # The deliverable lives under the project so a human can go and look at it.
        rel = '.superworker/nodes/%s/%s.md' % (node, skill)
        path = self.sb.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'{skill} delivered for {node}\n', encoding='utf-8')
        outs = self.outputs_for(action, rel, node)
        failed = node in self.fail_nodes
        status = self.fail_status if failed else 'completed'
        summary = f'{skill} {"failed" if failed else "done"}'
        self.reports += 1
        if self.via_handoff:
            return self.sb.handoff(self.cycle, node, status, summary, outs)
        return self.sb.write_signal(self.cycle, node, status, summary, outs)


# ═══════════════════════════════════════════════════════════════════════════════
#  The driver
# ═══════════════════════════════════════════════════════════════════════════════

class Driver:
    """Drive a line the way the engine asks: advance -> report -> settle -> advance ...

    It never performs a pipeline step itself. Per round it does the three things only a host and a
    session can do:

      * the host runs `advance` -- claim the free slots, land each cell's material, open one session
        per cell (a drill here: nothing opens and no `started` stamp lands);
      * the worker reports on the cell the plan dispatched, through the real command;
      * the worker's own `settle` closes the node it ran -- that is what records the step, marks the
        graph and lets the stop rule say whether the line parks. **A settle that parks the line is
        where a run ends**: the line is out of service and nothing else moves by itself.

    Every move that reports `applied` is checked against a before/after snapshot of the project taken
    from disk: the tool may not report success while nothing changed.
    """

    def __init__(self, sb: Sandbox, cycle='cyc', worker=None, max_steps=80, verify=True):
        self.sb = sb
        self.cycle = cycle
        self.worker = worker or WorkerSession(sb, cycle)
        self.max_steps = max_steps
        self.verify = verify
        self.actions = []           # every action the run read, in order
        self.receipts = []          # every `advance` receipt, in order
        self.applied = []           # (action, state_before, state_after) for each carried-out move
        self.dispatched = []        # (node_id, skill) in order
        self.nodes_closed = []      # nodes whose settle ran

    # ── the host's move ──────────────────────────────────────────────────────
    def _advance(self, action):
        """Run the host's word for the round the plan named, and check the contract it promises.

        `action` is the reading taken BEFORE the word ran: `applied` is only asserted when the word
        really moved the project (a claim landed, a prompt was written, a session was opened), and
        the reading is what that move was for.
        """
        before = self.sb.state_sig(self.cycle)
        receipt = self.sb.advance(self.cycle)
        self.receipts.append(receipt)
        after = self.sb.state_sig(self.cycle)
        if receipt.get('pinned'):
            self.applied.append((action, before, after))
            if self.verify and after == before:
                raise AssertionError(
                    f'advance claimed {receipt["pinned"]} but no state on disk changed: {after}')
        return receipt

    # ── the worker's two acts ────────────────────────────────────────────────
    def _work(self, node, action):
        """One worker act: report on `node` under the action the host handed over."""
        if action.get('action') == 'dispatch':
            self.dispatched.append((node, action.get('skill')))
        self.worker.work(dict(action, action='dispatch', node_id=node))

    def _settle(self, node):
        """The worker's own settle for `node`: record it, and say whether the line parked."""
        before = self.sb.state_sig(self.cycle)
        settled = self.sb.settle(node, cycle_id=self.cycle)
        self.actions.append(settled)
        self.nodes_closed.append(node)
        after = self.sb.state_sig(self.cycle)
        if settled.get('applied'):
            self.applied.append((settled, before, after))
            if self.verify and after == before:
                raise AssertionError(
                    f'settle of {node} reported applied but no state on disk changed: {after}')
        return settled

    def _apply(self, action):
        """A plan that is a graph edit and nothing else: a pick-time insertion.

        `insert_node` comes back from `plan` as a graph edit with nothing to open (no session, no
        pin). `advance` carries it out -- and stops the round there, because nothing was opened.
        """
        return self._advance(action)

    # ── the whole walk ───────────────────────────────────────────────────────
    def step(self):
        """One reading of the line: the action the plan names, before anything is carried out."""
        action = self.sb.plan(self.cycle)
        self.actions.append(action)
        return action

    def run(self, until=None):
        """Play the host and the two worker acts until `until(action)` says stop, or the run ends.

        `until` is how a caller picks its own finish line -- `finish_node` stops at the point where
        only the node's settle is left. The pipeline ending before that line is reached means the
        walk ended somewhere the caller did not ask for, so it is raised rather than returned.
        """
        for _ in range(self.max_steps):
            action = self.step()
            kind = action['action']

            if until is not None and until(action):
                return action

            if kind in ('open_node', 'dispatch'):
                receipt = self._advance(action)
                view = receipt.get('plan') or action
                for node in self._nodes_of(action):
                    self._work(node, view)
                continue

            if kind == 'wait' and action.get('close_with'):
                # Only the node's own settle is left. The worker that just reported runs it.
                settled = self._settle(action['node_id'])
                if settled.get('parked'):
                    return settled
                continue

            if kind == 'wait':
                # A `pending` ticket is a red light: the retry pair is the only way forward, and the
                # note says so. A driver that polled here would spin forever.
                raise AssertionError(f'stalled on wait: {action}')

            if kind == 'insert_node':
                # A graph edit and nothing else: `advance` lands it, claims nobody and opens
                # nothing, and the next round picks the new node up by the usual ranking.
                self._apply(action)
                continue

            if kind == 'settle':
                continue

            if kind == 'stop_pipeline':
                # The plan's own stop, written down: a line it stopped with nothing in hand is
                # turned `inactive` by the word that carries this plan out.
                self._advance(action)
                if until is not None:
                    raise AssertionError(
                        f'the pipeline stopped before the point asked for: {action}')
                return action

            if kind in TERMINAL_ACTIONS:
                if until is not None:
                    raise AssertionError(
                        f'the pipeline stopped before the point asked for: {action}')
                return action

            raise AssertionError(f'unexpected action {kind!r}: {action}')
        raise AssertionError('the pipeline never settled; last actions: '
                             + str(self.kinds()[-8:]))

    @staticmethod
    def _nodes_of(action):
        """Every node this action asks a worker for, in the line's own order.

        A dispatch names one entry per cell (`nodes`), and so does the claim behind it (`open_node`
        names bare ids): one reading, so the two rounds cannot drift apart on which cells were
        opened.
        """
        entries = action.get('nodes') or []
        ids = [item.get('node_id') if isinstance(item, dict) else item for item in entries]
        if not ids and action.get('node_id'):
            ids = [action['node_id']]
        return [node for node in ids if node]

    # ── inspection helpers ───────────────────────────────────────────────────
    def kinds(self):
        return [a['action'] for a in self.actions]

    def no_commands_anywhere(self):
        """No returned action carries commands for the caller to RUN.

        An action that answers "nothing to do" may say so with an empty list (`commands: []` on
        `ask_user` / `no_cycle`) -- there is nothing in it to run, so it is not a handover. A
        populated list is, and that is what the action set must never contain.
        """
        return [a for a in self.actions if a.get('commands')]


# ── convenience builders used by several test classes ────────────────────────

def _closable(action):
    """`finish_node`'s stop line: the node's ticket is on disk, and only its settle is left."""
    return action['action'] == 'wait' and bool(action.get('close_with'))


def finish_node(sb, cycle='cyc', worker=None):
    """Walk one node to the point where only its settle is left. Returns that `wait` action.

    The action is the worker's own cue: `close_with` names the command that records the node, and
    running it (`sandbox.settle`) is what closes it. The walk itself is `Driver`'s -- the host's
    advance, the applied-state contract and the worker's act all live there -- with this predicate as
    its finish line.
    """
    return Driver(sb, cycle, worker=worker, max_steps=20).run(until=_closable)
