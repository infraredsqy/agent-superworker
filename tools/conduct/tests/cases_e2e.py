"""The whole walk, through the shipped processes.

`TestEndToEnd` drives the four words as a real caller does -- `sw plan` reads, `sw settle` records,
`sw advance` claims and opens -- through the harness's spawn entry, whose drill switch the harness
holds: no session is ever started and the network is never touched, but every command is the real one.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cases_base import Base, skill_of  # noqa: E402
from harness import TERMINAL_ACTIONS  # noqa: E402

__all__ = ['TestEndToEnd']


class TestEndToEnd(Base):
    """The shipped path: every command a real process, the spawn a dry run."""

    def project(self, count=2):
        sb = self.make(cli=True)
        ids = sb.add_chain(count)
        sb.start_cycle()
        return sb, ids

    def deliver(self, sb, node_id, outputs=None, status='completed'):
        """One worker's report, as the real process. The close is `sw settle <node>`."""
        return sb.handoff('cyc', node_id, status, 'done',
                          outputs if outputs is not None else {'deliverable': 'x',
                                                               'acceptance': ['a'],
                                                               'touched': []})

    def test_the_walk_from_start_cycle_to_the_end_of_the_line(self):
        """Two nodes, and the run parks itself: report, settle, advance -- three commands a node.

        The loop mirrors what the pipeline asks a caller to do: read the action, and when it is a
        dispatch, let that node's worker report; when it is a wait, settle the node it names and
        advance, which lands the next session. The first dispatch is the one `cycle start` pinned.
        """
        sb, ids = self.project(2)

        opening, proc = sb.plan_cli()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(opening['action'], 'dispatch')
        self.assertEqual(opening['node_id'], ids[0])
        self.assertEqual(skill_of(opening), 'sw-conduct')

        seen = []
        finished = None
        for _ in range(8):
            action, proc = sb.plan_cli()
            self.assertEqual(proc.returncode, 0, proc.stderr)
            if action['action'] in TERMINAL_ACTIONS:
                # The line's last settle parked it: a bare `plan` leaves it alone and names the
                # command that puts it back in.
                finished = action['action']
                break
            if action['action'] == 'dispatch':
                # the worker for this node does its work and writes its ticket, through the same
                # command its prompt carries
                self.deliver(sb, action['node_id'])
                continue
            self.assertEqual(action['action'], 'wait', action)
            node = action['node_id']
            seen.append(node)
            settled, proc = sb.settle_cli(node)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(settled['action'], 'settle')
            receipt, proc = sb.advance_cli()
            self.assertEqual(proc.returncode, 0, proc.stderr)
            if receipt['plan']['action'] == 'dispatch':
                self.assertEqual(receipt['plan']['node_id'], ids[len(seen)])

        self.assertIsNotNone(finished, 'the run never settled')
        self.assertEqual(seen, ids)
        self.assertEqual(sb.path_nodes(), ids)
        self.assertEqual(sb.cycle()['status'], 'inactive')
        self.assertEqual([sb.dag_status(uid) for uid in ids], ['completed'] * 2)

    def test_no_session_is_ever_started(self):
        """`DSH_CONDUCT_SPAWN_DRY_RUN=1` is what the harness sets; every `spawned` line it produces
        has to be the dry-run banner and never a session id."""
        sb, ids = self.project(2)
        seen = []
        for _ in range(8):
            action, _ = sb.plan_cli()
            if action['action'] in TERMINAL_ACTIONS:
                break
            if action['action'] == 'dispatch':
                self.deliver(sb, action['node_id'])
                continue
            sb.settle_cli(action['node_id'])
            receipt, _ = sb.advance_cli()
            seen.extend(receipt.get('spawned') or [])

        self.assertTrue(seen, 'no spawn was ever reported')
        for line in seen:
            self.assertIn('dry-run', line, f'a real session was started: {line}')
