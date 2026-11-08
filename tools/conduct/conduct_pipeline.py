#!/usr/bin/env python3
"""The conduct pipeline's advance logic, as code instead of prose.

What the pipeline is
--------------------
A cycle is one line of work. It picks ONE DAG node at a time, hands it to a session, and when that
session reports back it records the step and picks the next node -- or stops. One node at a time, one
cycle at a time (a second active cycle is legal, but then a bare `advance` has to be told which one).

    cycle -> node -> (the session's own work on it) -> next node or stop

How a node splits its own work is the session's business and never the pipeline's: those are
subagents it commands, not cells this tool tracks. Nothing about a node's internals is written to
`.superworker/` -- what the pipeline keeps is the cycle's identity, the node it is holding right now, and the
path of nodes it has finished. Its dispatch plumbing (prompt files, the broken-chain marker) sits in
`.superworker/meta/`, which `.superworker/.gitignore` keeps out of the history.

Two rules this module owns
--------------------------
**`commands` are never handed to an agent.** The tool runs them; the caller reads `action` and does
the one thing only a session can do: spawn a session, or ask a human. (The v1 bug this prevents: a
tool that both performed an action and handed the agent the commands to perform it again stepped the
pointer twice.)

**The DAG's node status is the single source of truth for "did it work".** A node reports by writing
a signal; a terminal signal on disk means its work is finished and only its settle is left. The
worker's own closing call -- `settle <node>` -- reads the outcome off that signal, records the path
step and marks the node; it pins nothing and opens nothing. The next round is a separate word,
`advance` (claim the free slots, land each cell's material, open one session per cell), because the
worker still has to commit and push before the next session boots in the same worktree.

Invariants this module owns, each of which cost a bug to learn
-------------------------------------------------------------
  1. A cycle holds the nodes it claimed (`holding`). A claim is how work is taken:
     another cycle's pick skips a node some line is holding.
  2. A terminal signal is the whole of "this node is done"; a `running` signal is never a green
     light. `cycle retry` is what preps a retry.
  3. A node whose work is finished but which has not been settled is a *stable* state: `plan`
     answers `wait` with the closing command, as many times as it is asked, and writes nothing.
  4. Settling a node is not the same as finishing the cycle: the settle records the step and marks
     the DAG node, and it is the worker's own `settle <node>` that performs it.
  5. The node a cycle is about is recorded when the cycle is pointed at it, not when it reports.
     That is what makes a mid-run edit of the DAG harmless.
  6. `commands` are run here and stripped from the returned action.
  7. apply() refuses to report success unless something really moved.
  8. Every child process is bounded (CMD_TIMEOUT); a hung child must fail loudly, not hang a night.
  9. Instructions may only TIGHTEN the stop rule, never loosen it: a node whose mode is `checkpoint`
     always stops the run at the end of its node, whatever the instructions say -- and there is no
     caller flag that could contradict it. `auto` and `manual` may carry on: a manual node's whole
     life is the conversation with the user, and its completion is the user's release, so there is
     no second gate at the close. The node's own mode is the one place that question is answered;
     there is no line-level mode above it.
 10. The node's outcome is coarse on purpose: `completed` is the worker saying it delivered and
     `failed`/`stopped` is the worker saying it did not. Deviation is normal; only a *total* failure
     is worth stopping a night for.

Usage:
    python conduct_pipeline.py plan    [--cycle ID] [--project DIR] [--after NODE_ID]
    python conduct_pipeline.py settle  [--cycle ID] [--project DIR] --node NODE_ID
    python conduct_pipeline.py advance [--cycle ID] [--project DIR]

Those three are this module's own entry, for drills and tests. The pipeline's one entry point is
`sw plan` / `sw settle` / `sw advance`; `state-manage.py plan --help` prints the action vocabulary
from `ACTIONS` on its tail.

Environment:
    DSH_CONDUCT_DAG_MANAGE   where `dag-manage.py` is, when it is not the sibling `dag/`
                             directory of this file (a review draft, a drill copy).
    DSH_CONDUCT_SPAWNER      which spawner opens the next session: `dsh`, `zcode`, or a path.
                             Default: whichever host this shell is running under (see
                             `spawn/host.py`'s `default_spawner`). The per-call `--spawner` beats
                             it, and a cycle's own `spawner` policy beats it too -- the full order
                             is `resolve_spawner`'s.
"""

from __future__ import annotations

import argparse
import importlib.util
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent      # the conduct tool directory: the entry sits at its top
CONDUCT = HERE
STATE_MANAGE = HERE / 'state-manage.py'
#: Every child process this module starts is bounded by this many seconds. It lives
#: HERE, in the entry module, with the command builders below and `_run`, because those are the
#: names a caller patches on the loaded module: the test harness replaces `pipeline._run` with its
#: in-process runner and sets `pipeline.CMD_TIMEOUT` to prove a hung child is reported. The logic
#: modules in `pipeline/` reach them through `seam`, which reads this module's globals at
#: call time; a by-name import would freeze a copy and make every one of those patches invisible.
CMD_TIMEOUT = 120


# ── command builders (the mechanical half) ───────────────────────────────────

def _cmd(*args) -> list:
    return [sys.executable, str(STATE_MANAGE), *[str(a) for a in args]]


def _dag(*args) -> list:
    return [sys.executable, str(DAG_MANAGE), *[str(a) for a in args]]


def _as_text(cmd: list) -> str:
    return ' '.join(str(c) for c in cmd)


def _run(cmd: list, cwd: Path) -> None:
    # cwd matters: `.superworker/` is resolved relative to the process cwd.
    try:
        proc = subprocess.run([str(c) for c in cmd], cwd=str(cwd), capture_output=True,
                              text=True, encoding='utf-8', errors='replace', timeout=CMD_TIMEOUT)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f'command did not finish within {CMD_TIMEOUT}s: {_as_text(cmd)}') from e
    if proc.returncode != 0:
        raise RuntimeError(f'command failed: {_as_text(cmd)}\n{proc.stderr or proc.stdout}')


def _capture(cmd: list, cwd: Path) -> str:
    """Run one command and hand back its stdout.

    `_run` discards stdout on purpose -- a command's effect is what the pipeline wants from it.
    `dag-manage add` is the one command whose EFFECT is a fact that only appears in its output (the
    uuid it minted), and `insertion.read_new_node_id` -- the core mechanism every insertion source
    goes through -- is the one place that needs the text. Same bound, same loud failure, and it lives
    here next to `_run` because it is the same seam: a caller patching child processes patches this
    too.

    These two functions are **the only two ways to a child process** (two halves of one seam):
    every module in the package reaches them through `seam.run` / `seam.capture`, and none of them
    calls `subprocess` itself. How a child's output is decoded is settled here too: always
    `errors='replace'` -- this machine's console code page is GBK and PYTHONIOENCODING is not
    necessarily set, so the bytes read back may as well carry replacement characters rather than
    have a decode error take the whole pipeline down.
    """
    try:
        proc = subprocess.run([str(c) for c in cmd], cwd=str(cwd), capture_output=True,
                              text=True, encoding='utf-8', errors='replace', timeout=CMD_TIMEOUT)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f'command did not finish within {CMD_TIMEOUT}s: {_as_text(cmd)}') from e
    if proc.returncode != 0:
        raise RuntimeError(f'command failed: {_as_text(cmd)}\n{proc.stderr or proc.stdout}')
    return proc.stdout or ''


# ── which spawner opens the next session ─────────────────────────────────────
# `spawn/host.py` owns that answer: the two shipped spawners, the process-tree probe that tells the
# two hosts apart, and the `DSH_CONDUCT_SPAWNER` override all live next to the scripts they name, so
# a change to the probe lands in that file while the drills that patch this module's seam keep
# working. What this module keeps of it is one seam name, `SPAWNER_SCRIPT` (bound below, next to the
# loader that reaches the file).


# ═══════════════════════════════════════════════════════════════════════════════
#  Loading a neighbouring file, and the implementation package
# ═══════════════════════════════════════════════════════════════════════════════

# Bootstrap: the loader is fetched by path too, so every file has to carry this paragraph itself
# (see `shared/loader.py`).
SHARED = CONDUCT.parent / 'shared'

_LOADER = None


def _loader():
    """`tools/shared/loader.py`: the one loader, for the modules this tree keeps outside `shared/`."""
    global _LOADER
    if _LOADER is None:
        spec = importlib.util.spec_from_file_location('sw_shared_loader', SHARED / 'loader.py')
        _LOADER = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_LOADER)
    return _LOADER


def _tool_path(env_var: str, default):
    """`tools/shared/toolpath.py`: `<env var>` when it is set, else the neighbour this tree ships.

    The one tool below is a part of this tree that this module only calls; one rule, resolved once,
    is what keeps `DSH_CONDUCT_DAG_MANAGE` meaning the same thing here as it does in the merge
    driver.
    """
    toolpath = _loader().load_shared('toolpath')
    return toolpath.tool_path(env_var, default)


# `dag-manage.py` lives in the sibling `dag/` directory of the shipped tree, and that is the only
# place this module looks for it. A tree that keeps the conduct scripts somewhere else (a review
# draft, a drill copy) points at the real tool with `DSH_CONDUCT_DAG_MANAGE` rather than relying on
# this module having a second search path of its own: a silent fallback into `~/.agents` would also
# hide the failure that matters -- a shipped tree whose sibling `dag/` went missing.
DAG_MANAGE = _tool_path('DSH_CONDUCT_DAG_MANAGE', CONDUCT.parent / 'dag' / 'dag-manage.py')


def _load_impl():
    """Load `pipeline/` by path, fresh, and hand it back.

    By PATH, not by name, and with the name dropped from `sys.modules` first: every caller loads
    this file fresh (`tests/harness.py`, `handoff.py`, `state-manage.py`), and a by-name import
    would hand the second load the first load's modules. Inside the package, `from . import store`
    resolves through the registration the load makes.
    """
    pkg = 'pf_pipeline'
    impl_dir = CONDUCT / 'pipeline'
    return _loader().load_by_path(pkg, impl_dir / '__init__.py',
                                  package_dir=impl_dir, register=True, fresh=True)


def _host():
    """`spawn/host.py`: the two spawners, the host probe and the pid question, loaded by path.

    One copy per process (`register` + `reuse`): this module and the logic package's writer both ask
    for it, and a caller patching the probe patches the copy they both see.
    """
    return _loader().load_by_path('conduct_spawn_host', CONDUCT / 'spawn' / 'host.py',
                                  register=True, reuse=True)


#: The entry's `SPAWNER_SCRIPT`: what `seam.spawner_script()` hands `writer.spawn_session`, read at
#: call time because a caller may replace it. Its value is the host's own answer, asked once here.
SPAWNER_SCRIPT = _host().default_spawner()

_impl = _load_impl()

# The seam: the logic modules read `_cmd` / `_dag` / `_as_text` / `_run` / `CMD_TIMEOUT` /
# `SPAWNER_SCRIPT` out of THIS module's live namespace, so a caller patching them here is honoured
# everywhere. `globals()` is the live dict, not a copy -- that is the whole point.
_impl.seam.bind(globals())

# Everything the package exports, reachable from here: the CLI below, `handoff.py`, `state-manage.py`
# and the test harness all load this module and see one flat surface -- exactly the one it offered at
# module scope before the split.
globals().update({name: getattr(_impl, name) for name in _impl.__all__})


# ── CLI ──────────────────────────────────────────────────────────────────────

# The action vocabulary, which `state-manage.py plan --help` prints on the tail of its help. Kept as
# data so the help and the code cannot drift: the same list answers "what can `plan` hand me". This
# dict is the single source of the names.
ACTIONS = {
    'dispatch':      'a node is claimed and the prompt has landed -- send it out (`dispatch <cycle>` '
                     'opens its session; `advance` is claim, material and session in one word)',
    'open_node':     'the line holds no node and the ranking has one -- `hold <node>` claims it, '
                     'then `dispatch <cycle>` opens it',
    'insert_node':   'a new node has just been put into the graph: it is created, its spec is '
                     'written and its edges are wired, and **nothing was opened** -- no session, no '
                     'prompt file, and the new node is not pinned. Which node runs next is the next '
                     'step\'s decision (`advance` picks it up by the usual ranking)',
    'stop_pipeline': 'nothing left to do / this line is not in service -- close out and tell the '
                     'user how it ended',
    'wait':          'it is not yet time to act -- read note / close_with, never poll',
    'ask_user':      'the state is ambiguous -- ask a human',
    'no_cycle':      'there is no cycle yet -- opening one takes a human\'s intent',
    'settle':        'one node\'s end, as `plan --after <node>` rehearses it: the step, the graph '
                     'mark and the close-time seam -- never returned by a forward plan',
}


def _render_contract() -> str:
    lines = ['the full set of actions `plan` returns (the discriminant is the `action` field):']
    for name, what in ACTIONS.items():
        lines.append(f'  {name:<15} {what}')
    lines.append('')
    lines.append('Invariant: the returned action **carries no `commands`** -- the tool runs the '
                 'commands itself; the caller only reads `action` and does what only a session can do.')
    return '\n'.join(lines)


def main(argv=None) -> int:
    """This module's own entry: parse the line, run `plan` / `settle` / `advance`, print the answer.

    What is here is the parse and the one call into the package. The output -- JSON or the human
    rendering, and the rule that a preview carries no commands -- is `actions.render_action`'s, the
    same one `sw plan` prints through, so the two spellings of the pipeline's front door cannot drift
    apart in what they show.

    Three words, three readings: `plan` reads (and `--after <node>` is the closing rehearsal),
    `settle` writes one node's end down, `advance` claims the free slots, lands the material and
    opens the sessions. The old op-selecting flags (`--plan`, `--after` on the writing side,
    `--continue` / `--stop`) are gone: an option shapes how an operation runs, it never swaps the
    operation for another one.
    """
    # Human words everywhere: reason, options and the contract are all prose. Both streams go out
    # as UTF-8, otherwise the console code page (GBK by default on this machine) writes the bytes
    # wrong and a parent process decoding as UTF-8 blows up in its read thread (stdout becomes None).
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(
        prog='conduct_pipeline.py',
        description='Decide what the conduct pipeline does next (read-only planner + writer).')
    parser.add_argument('command', choices=['plan', 'settle', 'advance'])
    parser.add_argument('--cycle', help='Cycle id (default: the single active cycle)')
    parser.add_argument('--project', default='.', help='Project root (default: cwd)')
    parser.add_argument('--after', dest='after', default=None, metavar='NODE_ID',
                        help='plan: the closing rehearsal -- this node has handed in its ticket, '
                             'and settling it, where would the line go')
    parser.add_argument('--node', default=None, metavar='NODE_ID',
                        help='settle: which node is being closed (a line holding several needs to '
                             'be told)')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)

    if args.after and args.command != 'plan':
        print('Error: --after is `plan`\'s closing rehearsal; the writing word is `settle --node`. '
              'Nothing was changed.', file=sys.stderr)
        return 2
    if args.node and args.command != 'settle':
        print('Error: --node names the node `settle` writes down, and nothing is settled by '
              f'`{args.command}`. Nothing was changed.', file=sys.stderr)
        return 2
    try:
        if args.command == 'settle':
            action = settle(args.project, args.cycle, args.node)
        elif args.command == 'advance':
            action = advance(args.project, args.cycle)
        else:
            action = plan(args.project, args.cycle, after=args.after)
    except (RuntimeError, ValueError) as e:
        print(f'Error: {e}', file=sys.stderr)
        return 2
    print(render_action(action, args.json, preview=args.command == 'plan'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
