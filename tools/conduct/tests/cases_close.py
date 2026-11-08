"""The settle: one worker ends the node it just ran, and what the settle refuses.

`sw settle <node>` is the only thing that ends a step. The outcome is read off the signal the worker
already wrote; the settle decides nothing, pins nothing and opens nothing -- the next round is
`advance`. The guards next to it are the states a settle may NOT be written over -- no ticket at all,
somebody else's node, a node that already left the line.

The picking side (start / filter set / weight) lives in `cases_select.py`; the line's holding and the
two driven paths live in `cases_contract.py`.

    python -m unittest conduct.tests.cases_close         # this file on its own
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cases_base import NODE, NODE2, NODE3, Base  # noqa: E402
from harness import Driver, WorkerSession  # noqa: E402

__all__ = ['TestWorkerSettlesItsOwnNode', 'TestCloseGuards']


class TestWorkerSettlesItsOwnNode(Base):
    """`sw settle <node>`: the worker closes the node it just ran.

    The outcome is read off the signal the worker already wrote, so settling is one call that says
    "the node I just finished is done" -- and a node that has not finished answers `wait` rather than
    being taken as delivered.
    """

    def finished_node(self, sb, cli=False):
        """Pin the node, let its worker report `completed`."""
        sb.add_node(NODE, 'UI design', mode='auto')
        sb.start_cycle()
        first = sb.advance()['plan']
        WorkerSession(sb).work(first)
        return first

    def test_the_settle_records_the_node_and_the_next_advance_opens_the_next_one(self):
        """One settle, then one advance: the record lands in this call, the session behind it in the
        next. The two are separate words because the worker still has to push its own work first."""
        sb = self.make()
        sb.add_node(NODE, 'UI design', mode='auto')
        sb.add_node(NODE2, 'another line', mode='auto')
        sb.start_cycle()
        first = sb.advance()['plan']
        WorkerSession(sb).work(first)

        settled, proc = sb.settle_cli(first['node_id'])

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(settled['action'], 'settle')
        self.assertFalse(settled['parked'])
        self.assertEqual(sb.path_nodes(), [NODE])
        self.assertIsNone(sb.current_node(), 'the settle claimed a successor')

        receipt, proc = sb.advance_cli(spawner=sb.fake_spawner(), dry_run=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(receipt['pinned'], [NODE2])
        self.assertTrue(receipt['spawned'], 'the advance opened no session for the next cell')
        self.assertIn('spawned:', receipt['spawned'][0])
        self.assertEqual(sb.current_node(), NODE2)

    def test_the_last_node_stops_the_run(self):
        """No next node means stop: the plan's own rule parks the line, and the `attention` list
        still reaches whoever is reading."""
        sb = self.make()
        first = self.finished_node(sb)

        settled, proc = sb.settle_cli(first['node_id'])

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(settled['action'], 'settle')
        self.assertTrue(settled['parked'])
        self.assertEqual(sb.cycle()['status'], 'inactive')
        self.assertEqual(sb.path_nodes(), [NODE])

    def test_a_node_that_is_still_running_is_not_settled(self):
        """A `running` signal is never a green light -- the rule every path enforces."""
        sb = self.make()
        first = self.finished_node(sb)
        sb.write_signal('cyc', NODE, 'pending', 'not finished yet')

        settled, proc = sb.settle_cli(first['node_id'])

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(settled['action'], 'wait')
        self.assertEqual(sb.current_node(), NODE)
        self.assertEqual(sb.path(), [])

    def test_naming_the_wrong_node_changes_nothing(self):
        """The guard runs before every write, so a mistyped node id leaves the run untouched."""
        sb = self.make()
        sb.add_node(NODE, 'UI design', mode='auto')
        sb.add_node(NODE2, 'another line', mode='auto')
        sb.start_cycle()
        sb.hold(NODE)
        sb.write_signal('cyc', NODE, 'completed', 'done')
        before = sb.tree_hashes()

        _, proc = sb.settle_cli(NODE2, check=False)

        self.assertEqual(proc.returncode, 2)
        self.assertIn('does not name', proc.stderr)
        self.assertEqual(sb.tree_hashes(), before)
        self.assertEqual(sb.path(), [])

    def test_a_non_delivery_parks_the_node_shelved_and_stops(self):
        """A worker that reports `failed` is asking to be looked at, and the DAG's node status is
        where that shows: the node becomes `shelved`, out of play until a human revives it."""
        sb = self.make()
        sb.add_node(NODE, 'UI design', mode='auto')
        sb.add_node(NODE2, 'another line', mode='auto')
        sb.start_cycle()
        first = sb.advance()['plan']
        WorkerSession(sb, fail_nodes={NODE}).work(first)

        settled, proc = sb.settle_cli(first['node_id'])

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(settled['action'], 'settle')
        self.assertTrue(settled['parked'])
        self.assertEqual(settled['graph'], 'shelved')
        self.assertEqual(sb.dag_status(NODE), 'shelved')
        self.assertIn(NODE, [a.get('node_id') for a in settled.get('attention', [])])

    def test_a_settled_node_is_not_settled_twice(self):
        """The second call has nothing to settle and must say so rather than closing something
        again. On a one-node run the cycle is finished, which is the honest reason."""
        sb = self.make()
        first = self.finished_node(sb)
        sb.settle_cli(first['node_id'])
        before = sb.tree_hashes()

        _, proc = sb.settle_cli(first['node_id'], check=False)

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('already closed', proc.stderr)
        self.assertEqual(sb.tree_hashes(), before)

    def test_a_node_nobody_holds_is_refused_by_name(self):
        """`settle` is aimed by node alone, so an id that is not any cycle's node has to be refused
        by name -- with a reading of where every cycle actually stands."""
        sb = self.make()
        sb.add_node(NODE, 'UI design', mode='auto')
        sb.start_cycle()

        _, proc = sb.settle_cli(NODE3, check=False)

        self.assertEqual(proc.returncode, 2)
        self.assertIn(NODE3, proc.stderr)
        self.assertIn(NODE, proc.stderr)
        self.assertEqual(sb.path(), [])


class TestCloseGuards(Base):
    """A node's step may only be settled over work that actually reports."""

    def test_a_settle_arriving_before_the_worker_reports_is_refused(self):
        """No ticket means nothing to settle. The tool must not read "no signal" as "delivered":
        settling here would throw away the work the session is still doing."""
        sb = self.make()
        sb.add_node(NODE, 'UI design', mode='auto')
        sb.start_cycle()
        sb.advance()
        before = sb.tree_hashes()

        action = sb.settle(NODE)

        self.assertEqual(action['action'], 'wait')
        self.assertFalse(action.get('applied'))
        self.assertEqual(sb.tree_hashes(), before)
        self.assertEqual(sb.path(), [])
        self.assertEqual(sb.current_node(), NODE)

    def test_a_running_signal_is_not_a_green_light(self):
        sb = self.make()
        sb.add_node(NODE, 'UI design', mode='auto')
        sb.start_cycle()
        sb.advance()
        sb.write_signal('cyc', NODE, 'pending', 'retry is ready')
        action = sb.plan()

        self.assertEqual(action['action'], 'wait')
        self.assertIn('cycle retry', action['note'])
        self.assertIn(NODE, action['note'])

    def test_the_settle_reports_a_half_open_state_stably(self):
        """Asked twice with the ticket on disk and the settle not yet run, the answer is the same
        and nothing is consumed -- the whole point of `wait` being a stable state."""
        sb = self.make()
        sb.add_node(NODE, 'UI design', mode='auto')
        sb.start_cycle()
        sb.advance()
        sb.write_signal('cyc', NODE, 'completed', 'done')
        before = sb.tree_hashes()

        first, second = sb.plan(), sb.plan()

        self.assertEqual(first['close_with'], second['close_with'])
        self.assertEqual(sb.tree_hashes(), before)

    def test_a_whole_run_of_nodes_settled_by_their_own_workers(self):
        """Three auto nodes, each settled by its own worker's call: the DAG drains, the path lists
        every step in order, and nothing needs a human.

        Two invariants ride along with the drain: the dispatches come out in chain order (a run
        IS the line), and the last settle parks the line (`inactive`) -- the uniform end-of-line
        write, whatever is left in the graph."""
        sb = self.make()
        ids = sb.add_chain(3)
        sb.start_cycle()
        final, driver = self.run_to_stop(sb)

        self.assertEqual(final['action'], 'settle')
        self.assertTrue(final['parked'])
        self.assertEqual(sb.path_nodes(), ids)
        self.assertEqual(driver.nodes_closed, ids)
        self.assertEqual([sb.dag_status(uid) for uid in ids], ['completed'] * 3)
        self.assertEqual([uid for uid, _ in driver.dispatched], ids)   # dispatch order = chain order
        self.assertEqual(sb.cycle()['status'], 'inactive')             # the settle takes the line off

    def test_the_cycle_can_be_driven_by_the_cli_alone(self):
        """The shipped path end to end: every word a real process -- `advance` claims and opens,
        `settle` records -- with no session ever started (the dry-run switch)."""
        sb = self.make(cli=True)
        ids = sb.add_chain(2)
        sb.start_cycle()
        driver = Driver(sb, worker=WorkerSession(sb))
        final = driver.run()

        self.assertEqual(final['action'], 'settle')
        self.assertTrue(final['parked'])
        self.assertEqual(sb.path_nodes(), ids)
