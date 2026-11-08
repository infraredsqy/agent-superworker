"""The sandbox: the `Sandbox` throwaway temp project every test drives, and the spawn spelling.

Owns the fixture builders for the on-disk world the pipeline reads, and the two ways of
running the tools against it: in-process by default, `cli=True` through the real processes.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import harness_proc as hproc
from harness_env import (DAG_MANAGE, SPAWN_DRY_RUN, STATE_MANAGE, _child_env, _decode,
                         chdir, init_markers, patched, spawn_dry_run)
from harness_fast import FastStateManage, InProcessDagManage, _dag_hook, _signal_module
from harness_proc import _json_out

__all__ = ['SPAWN_FLAG', 'Sandbox']

_SANDBOX_SEQ = 0

#: The one flag a spawn call carries: which spawner opens the session -- a word (`dsh` / `zcode`)
#: or the path of a stand-in. It is spelled here and nowhere else in the suite: a case that wants a
#: session opened names `Sandbox.advance` / `Sandbox.dispatch` with a `spawner=`, and both build the
#: argv from this name and hold the drill switch behind it.
SPAWN_FLAG = '--spawner'

#: The line a stand-in spawner prints: the shape `conduct_spawn.session_id_of` reads an id out of.
_STUB_LINE = 'spawned: session session-stub (node session)'


# ═══════════════════════════════════════════════════════════════════════════════
#  The sandbox
# ═══════════════════════════════════════════════════════════════════════════════

class Sandbox:
    """A temp project with the wiring the pipeline reads: one `.superworker/`, and a DAG.

    That one root carries both the account (`state.json`, `signals/`, `instructions.json`,
    `ideas.json`, each node's own directory) and what may be thrown away (`meta/launch/`, each
    node's `temp/`). It also holds the machine's settings layer (`self.home`) for as long as it
    lives, so a case never reads the one outside the sandbox.

    `cli=True` routes every `state-manage.py` verb through the real process. The default
    (in-process) is the same modules and the same files without the process start;
    `test_the_fast_path_and_the_cli_agree` is what keeps that honest.
    """

    def __init__(self, nodes=(), cycle=None, cli=False):
        global _SANDBOX_SEQ
        _SANDBOX_SEQ += 1
        # The prefix must differ between sandboxes in the same process: the in-process
        # state-manage cache is keyed by project path, and a path reused after a cleanup would
        # hand a new sandbox an old module.
        self._tmp = tempfile.TemporaryDirectory(prefix=f'conduct-test-{_SANDBOX_SEQ:03d}-')
        self.root = Path(self._tmp.name)
        self.cli = cli
        # The machine's own settings layer (`~/.superworker/config.json`) is pointed inside the sandbox: it is
        # the one thing a run reads from outside the project (a knob's widest layer), and what a case
        # reads must not change with the machine it happens to run on. `SUPERWORKER_HOME_DIR` is the same lever
        # `cases_config` pulls; this file's cases have no setUp of their own. `close()` puts it back.
        self.home = self.root / '.home'
        self._env_home = os.environ.get('SUPERWORKER_HOME_DIR')
        os.environ['SUPERWORKER_HOME_DIR'] = str(self.home)
        # `.superworker/` holds the account: state.json + signals/ + instructions.json + ideas.json, and
        # every node's own directory. There is no `loops/`: a cycle carries its own path now.
        ledger = init_markers(self.root)
        (ledger / 'nodes').mkdir()
        self.write_dag({})
        self._fast = FastStateManage(self.root)
        for node in nodes:
            self.add_node(**node) if isinstance(node, dict) else self.add_node(*node)
        if cycle:
            self.create_cycle(**cycle) if isinstance(cycle, dict) else self.create_cycle(*cycle)

    # ── lifecycle ────────────────────────────────────────────────────────────
    def close(self):
        """Drop the throwaway project, and never fail the case for a directory that would not go.

        The one case where a Windows temp directory survives `rmtree` is a child process that has
        not quite let go of a file (the CLI cases start real processes). That is the machine's
        timing talking, so it must not turn a green case red: what a case asserts is the project's
        contents, and the directory is about to be reaped by the OS anyway.
        """
        # The settings home this sandbox claimed (`__init__`) goes back either way: the next case in
        # this worker process reads its own project, not this one's leftovers.
        if self._env_home is None:
            os.environ.pop('SUPERWORKER_HOME_DIR', None)
        else:
            os.environ['SUPERWORKER_HOME_DIR'] = self._env_home
        try:
            self._tmp.cleanup()
        except OSError:
            pass

    # ── test setup only: building the world the pipeline will CONSUME ────────
    # Fixture data: the graph, a node's directory, a forge for hand-written state. Nothing here
    # advances the pipeline -- the cycle pointer and the path only ever move through the tools.
    def write_dag(self, nodes: dict):
        path = self.root / '.superworker' / 'ideas.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'nodes': nodes}, ensure_ascii=False, indent=2),
                        encoding='utf-8')

    def dag(self) -> dict:
        return json.loads((self.root / '.superworker' / 'ideas.json').read_text(encoding='utf-8'))

    def add_node(self, uid, title, status='todo', mode='auto', deps=(), tags=(), weight=None):
        """One DAG node on disk. `status` is the account's own word -- `todo` by default, which is
        what `dag-manage add` writes."""
        dag = self.dag()
        dag['nodes'][uid] = {'title': title, 'status': status, 'mode': mode,
                             'dependencies': list(deps)}
        if tags:
            # A node with no tag looks like this in the ledger today: the `tags` key does not
            # appear at all.
            dag['nodes'][uid]['tags'] = list(tags)
        if weight:
            # `weight` is the same as `tags`: the key appears only when it was really written.
            # Passing a value outside the vocabulary is what tests the "an unknown word reads as
            # standard" fallback.
            dag['nodes'][uid]['weight'] = weight
        self.write_dag(dag['nodes'])
        node_dir = self.root / '.superworker' / 'nodes' / uid
        node_dir.mkdir(parents=True, exist_ok=True)
        (node_dir / 'spec.md').write_text(f'# {title}\n\nspec body\n', encoding='utf-8')
        return uid

    def add_temp(self, uid, name='probe.txt', body='raw output\n'):
        """One file in a node's own `temp/` -- what `clean-temp` sweeps.

        Scratch, so it is NOT in the node's ledger directory: `.superworker/nodes/<uid>/temp/`.
        """
        temp = self.root / '.superworker' / 'nodes' / uid / 'temp'
        temp.mkdir(parents=True, exist_ok=True)
        (temp / name).write_text(body, encoding='utf-8')
        return temp

    def add_chain(self, count, prefix='node', mode='auto'):
        """`count` nodes in a line: each depends on the one before it."""
        ids, prev = [], None
        for i in range(count):
            uid = f'{prefix}-{i:02d}'
            self.add_node(uid, f'Node {i}', mode=mode, deps=[prev] if prev else ())
            ids.append(uid)
            prev = uid
        return ids

    def dag_status(self, uid):
        return (self.dag()['nodes'].get(uid) or {}).get('status')

    def node(self, uid):
        return self.dag()['nodes'].get(uid)

    def temp_dir(self, uid):
        """Where a node's generated history goes: scratch, and NOT the node's ledger directory."""
        return self.root / '.superworker' / 'nodes' / uid / 'temp'

    # ── the real tools ───────────────────────────────────────────────────────
    def _tool(self, *args, check=True, cli=None):
        """Run one state-manage verb.

        `cli=True/False` forces one path; the default is the sandbox's own mode. Tests that are
        ABOUT a verb's output (its refusal message, its stdout) use `cli=True` regardless: the
        in-process path reports a refusal the same way, but only the process is the shipped thing.
        """
        use_cli = self.cli if cli is None else cli
        if use_cli:
            return hproc.run_state_manage(self.root, *args, check=check)
        try:
            # Some verbs reach back into conduct_pipeline (`cycle start` picks the node with the same
            # rule the close uses), which calls dag-manage; hook that too.
            with _dag_hook(self.root):
                self._fast.run([sys.executable, str(STATE_MANAGE), *[str(a) for a in args]])
        except RuntimeError as e:
            if check:
                raise
            return subprocess.CompletedProcess(args, 1, self._fast.stdout, str(e))
        return subprocess.CompletedProcess(args, 0, self._fast.stdout, self._fast.stderr)

    def create_cycle(self, cycle_id='cyc', title='test cycle', force=False, tags='coding'):
        """`cycle create`: a new line, with the filter set every new line must carry.

        `tags` defaults to `coding` here because `cycle create` / `cycle start` now REFUSE a call
        without `--tags` (a line's range is also the set the pick filters by). A case whose subject is
        that refusal calls `_tool('cycle', 'create', …)` itself, with no `--tags`, so the helper's
        default can never hide it.
        """
        args = ['cycle', 'create', cycle_id, '--title', title]
        if tags:
            args += ['--tags', ','.join(tags) if not isinstance(tags, str) else tags]
        if force:
            args += ['--force']
        self._tool(*args)
        return cycle_id

    def start_cycle(self, cycle_id='cyc', title='test cycle', force=False, tags='coding',
                    parallel=None, check=True):
        """`cycle start`: create the cycle AND point it at the node the rule picks.

        `tags` is the cycle's filter set, and it reaches the pick as well as the cycle record. It
        is required by the tool now (see `create_cycle`), so the helper supplies it unless a case
        names its own. The launcher refuses when nothing is ready, so `check=False` returns a
        `CompletedProcess` with the refusal instead of raising.

        `parallel` is how many slots this line starts with: it lands in the cycle's policies and
        decides how many nodes the start pins, exactly like `cycle policy <cycle> parallel <n>`
        does for every close after it.
        """
        if tags and not isinstance(tags, str):
            tags = ','.join(tags)
        return self._tool('cycle', 'start', cycle_id, '--title', title,
                          *(['--tags', tags] if tags else []),
                          *(['--parallel', str(parallel)] if parallel is not None else []),
                          *(['--force'] if force else []), check=check)

    def set_cycle_status(self, cycle_id, status, cli=None, check=True):
        """`cycle status <cycle> <status>`: put a line in or out of service."""
        return self._tool('cycle', 'status', cycle_id, status, cli=cli, check=check)

    def set_policy(self, cycle_id, key, value, cli=None, check=True):
        """`cycle policy <cycle> <key> <value>`: one of the line's own policies."""
        return self._tool('cycle', 'policy', cycle_id, key, value, cli=cli, check=check)

    def hold(self, node, *more, cycle_id='cyc', check=True):
        """`hold <node…> [--cycle <id>]`: claim nodes for a line -- the one way work is taken.

        Several ids in one line are one write, all or none.
        """
        args = ['hold', node, *more]
        if cycle_id:
            args += ['--cycle', cycle_id]
        return self._tool(*args, check=check)

    def clean_temp(self, node=None, yes=False, project=None, check=True):
        args = ['clean-temp', '--project', str(project or self.root)]
        if node:
            args += ['--node', node]
        if yes:
            args += ['--yes']
        return hproc.run_state_manage(self.root, *args, check=check)

    def dispatch(self, cycle_id='cyc', project=None, as_json=False, cli=None, check=True,
                 spawner=None, dry_run=True):
        """`dispatch <cycle> [--project <root>] [--json]`: start the sessions of the cells a line
        holds.

        A DRILL unless the caller names its own `spawner` stub and says `dry_run=False`: the drill
        switch is set here -- in this process for the in-process rail, in the child's environment for
        the CLI rail -- so no case can reach this host's spawner by forgetting to hold it. A case that
        only wants to check WHICH node would be dispatched reads the plan (`Sandbox.plan` / the
        receipt's `plan`) and never comes through here at all: what this hands down is the argv a real
        `dsh-spawn.ps1` would run, and that opens a session.
        """
        args = ['dispatch', cycle_id]
        if project:
            args += ['--project', str(project)]
        if as_json:
            args += ['--json']
        if dry_run:
            # A drill: the flag names the stand-in and the switch below keeps any host spawner out
            # of reach. The other rail builds both in `_spawn_argv`, which is why it is not handed
            # a flag here.
            if spawner is not None:
                args += [SPAWN_FLAG, str(spawner)]
            with spawn_dry_run():
                return self._tool(*args, cli=cli, check=check)
        return self._spawn_proc(args, spawner=spawner, dry_run=dry_run, check=check)

    def instruct(self, text, scope=None, target=None, **extra):
        """One `instruction add`. The three scopes are global / cycle / node.

        `stop_after_nodes` and `note` come through `extra`; `cycle` / `node` name the binding by
        hand, which is what a session does while two lines run side by side.
        """
        args = ['instruction', 'add', '--text', text]
        if scope:
            args += ['--scope', scope]
        if target:
            args += ['--target', target]
        for key, value in extra.items():
            args += ['--' + key.replace('_', '-'), str(value)]
        return self._tool(*args)

    def instruct_list(self, live_only=False):
        """The account, read straight off disk: the file IS the account (one entry per id)."""
        items = _decode(self.root / '.superworker' / 'instructions.json') or {}
        items = items.get('instructions', [])
        if live_only:
            items = [i for i in items if i.get('state', 'live') == 'live']
        return items

    def instruct_list_cli(self, live_only=False):
        """The raw account as the CLI sees it: `--all` asks for the rows, not the reading."""
        args = ['instruction', 'list', '--json', '--all'] + (['--live'] if live_only else [])
        proc = hproc.run_state_manage(self.root, *args)
        return json.loads(proc.stdout)

    def instruct_visible_cli(self, node=None, cycle=None):
        """`instruction list --json`: what a session standing here now would read."""
        args = ['instruction', 'list', '--json']
        if node:
            args += ['--node', str(node)]
        if cycle:
            args += ['--cycle', str(cycle)]
        proc = hproc.run_state_manage(self.root, *args)
        return json.loads(proc.stdout)

    def instruct_resolve(self, ins_id, state, check=True, **extra):
        args = ['instruction', 'resolve', ins_id, '--state', state]
        for key, value in extra.items():
            args += ['--' + key.replace('_', '-'), str(value)]
        return self._tool(*args, check=check)

    def instruct_rm(self, ins_id, check=True, cli=None, **extra):
        """`instruction rm`: the owner takes one entry off the account, text and all."""
        args = ['instruction', 'rm', ins_id]
        for key, value in extra.items():
            args += ['--' + key.replace('_', '-'), str(value)]
        return self._tool(*args, check=check, cli=cli)

    def instruct_revive(self, ins_id, check=True, cli=None, **extra):
        """`instruction revive`: the owner puts an archived entry back in force."""
        args = ['instruction', 'revive', ins_id]
        for key, value in extra.items():
            args += ['--' + key.replace('_', '-'), str(value)]
        return self._tool(*args, check=check, cli=cli)

    # ── a worker's report ────────────────────────────────────────────────────
    def signal_path(self, cycle_id, node_id):
        return self.root / '.superworker' / 'signals' / cycle_id / f'{node_id}.json'

    def write_signal(self, cycle_id, node_id, status, summary='', outputs=None):
        """A worker's report, through the real signal.py in-process: it is a pure file writer
        whose contract is the file it leaves behind, and handoff.py calls this very function."""
        mod = _signal_module()
        return mod.write_signal(cycle_id, node_id, status, summary, outputs or {},
                                base_dir=self.root / '.superworker' / 'signals')

    def handoff(self, cycle_id, node_id, status='completed', summary='', outputs=None,
                check=True):
        """The worker's ONE report, as a real process: write the ticket, spawn nothing."""
        return hproc.run_handoff(self.root, cycle_id, node_id, status, summary, outputs, check)

    def read_signal(self, cycle_id, node_id):
        return _decode(self.signal_path(cycle_id, node_id))

    def write_raw_signal(self, cycle_id, node_id, status='completed', summary='forged',
                         payload=None):
        """One signal file written straight to disk, for forging what the tools will not produce
        (a malformed payload, a hand-edited file). Fixture data, never a pipeline step."""
        path = self.signal_path(cycle_id, node_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        if payload is None:
            payload = {'cycle': cycle_id, 'node': node_id, 'status': status, 'summary': summary,
                       'outputs': {}, 'timestamp': '2026-01-01T00:00:00Z'}
        path.write_text(payload if isinstance(payload, str)
                        else json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        return path

    # ── the four words: plan / advance / settle / dispatch ───────────────────
    def plan(self, cycle_id=None, after=None):
        """`pipeline.plan`: the next action, read-only (writes not one byte).

        The answer is the CALLER-FACING reading -- what `sw plan` prints: the engine's own
        `render_action` with `preview=True`, parsed back. A preview is not executable, so the
        mechanical `commands` an action still carries half way are not in it; that is the shape every
        caller (and every case) reads. `after` is the closing rehearsal -- `sw plan --after <node>` in
        the shipped spelling -- and `cycle_id` aims the reading at a named line (a bare reading uses
        the single active one). The CLI rail is `plan_cli`.
        """
        return self._pipeline_call(self.root, 'plan', cycle_id=cycle_id, after=after)

    def plan_cli(self, *args, check=True):
        """`plan` through the real CLI process. Returns (action, completed_process)."""
        argv = ['plan', '--json', *[str(a) for a in args]]
        proc = hproc.run_state_manage(self.root, *argv, check=False, env=_child_env())
        if check and proc.returncode != 0:
            raise AssertionError(f'plan {argv} failed (rc={proc.returncode}):\n'
                                 f'{proc.stdout}\n{proc.stderr}')
        return (_json_out(proc, argv) if proc.stdout.strip() else None), proc

    def advance(self, cycle_id=None, spawner=None, dry_run=True, cli=None):
        """`pipeline.advance`: claim the free slots, land each cell's material, open one session per
        cell. Returns the RECEIPT -- `receipt['plan']` is the action a caller acts on.

        A DRILL unless the caller names its own `spawner` stub and says `dry_run=False`: the drill is
        the default here so no case can reach this host's spawner by forgetting to hold it. In a drill
        the spawn call is only reported -- no session opens and the `started` stamp does not land, so
        a re-plan still says `dispatch`. The CLI rail is taken when the sandbox asks for it
        (`cli=True`) or the caller says so; `advance_cli` is that rail with the process in hand.
        """
        use_cli = self.cli if cli is None else cli
        if use_cli:
            args = ['--cycle', str(cycle_id)] if cycle_id else []
            return self.advance_cli(*args, spawner=spawner, dry_run=dry_run)[0]
        return self._pipeline_call(self.root, 'advance', cycle_id=cycle_id, spawner=spawner,
                                   dry_run=dry_run)

    def advance_cli(self, *args, spawner=None, dry_run=True, check=True):
        """`advance` through the real CLI process. Returns (receipt, completed_process)."""
        argv, env = self._spawn_argv(['advance', '--json', *[str(a) for a in args]],
                                     spawner, dry_run)
        proc = hproc.run_state_manage(self.root, *argv, check=False, env=env)
        if check and proc.returncode != 0:
            raise AssertionError(f'advance {argv} failed (rc={proc.returncode}):\n'
                                 f'{proc.stdout}\n{proc.stderr}')
        return (_json_out(proc, argv) if proc.stdout.strip() else None), proc

    def settle(self, node, cycle_id=None, cli=None, check=True):
        """The worker's own closing call: one node's end, and nothing else.

        Returns the action -- `settle`, or `wait` / `ask_user` when there is nothing to record. The
        CLI rail is taken when the sandbox asks for it (`cli=True`), and its refusal is returned on
        the `CompletedProcess` there; in process the CLI's own gate is restated here, so a case that
        names a node this line is not holding fails loudly instead of closing somebody else's work.
        """
        use_cli = self.cli if cli is None else cli
        if use_cli:
            return self.settle_cli(node, cycle_id=cycle_id, check=check)[0]
        holder = cycle_id or next((c['id'] for c in (self.state() or {'cycles': []})['cycles']
                                   if node in self._holding_ids_of(c)), None)
        here = self._holding_ids_of(self.cycle(holder)) if holder else []
        assert node in here, f'settle {node} does not name a node the cycle holds ({here})'
        return self._pipeline_call(self.root, 'settle', cycle_id=holder, node=node)

    def settle_cli(self, node, *args, cycle_id=None, dry_run=False, check=True):
        """`settle <node>` through the real CLI process. Returns (action, completed_process).

        `--dry-run` prints the same action and writes nothing: the rehearsal a case can hold side by
        side with the real one.
        """
        argv = ['settle', str(node), '--json']
        if cycle_id:
            argv += ['--cycle', str(cycle_id)]
        if dry_run:
            argv += ['--dry-run']
        argv += [str(a) for a in args]
        proc = hproc.run_state_manage(self.root, *argv, check=False, env=_child_env())
        if check and proc.returncode != 0:
            raise AssertionError(f'settle {argv} failed (rc={proc.returncode}):\n'
                                 f'{proc.stdout}\n{proc.stderr}')
        return (_json_out(proc, argv) if proc.stdout.strip() else None), proc

    # ── asking for a session ─────────────────────────────────────────────────
    def mark_started(self, node, cycle_id='cyc'):
        """The `started` stamp a successful dispatch leaves, written straight to disk.

        Fixture data, never a pipeline step: a case whose subject is "what a line whose slots are
        already running does" states that state instead of starting a spawner -- the same footing
        `add_temp` and `rewrite_cycle` stand on. A case whose subject IS the spawn goes through
        `advance` / `dispatch` with a stand-in spawner instead.
        """
        marker = self.root / '.superworker' / 'meta' / 'launch' / cycle_id / node / 'started'
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text('0 started (fixture)\n', encoding='utf-8')
        return marker

    def fake_spawner(self, name='spawner.ps1', line=None, body=None):
        """A stand-in spawner script: by default it prints the contract line and opens nothing.

        `line` states a different contract line (a case that reads a session id back out of it);
        `body` replaces the script outright, for the stand-ins whose subject is what the spawner does
        (a failing one). Never a host spawner: what a case does with this path is hand it to
        `--spawner`, on the rail that has taken the drill switch back out.
        """
        script = body if body is not None else 'Write-Output "%s"\n' % (line or _STUB_LINE)
        path = self.root / name
        path.write_text(script, encoding='utf-8')
        return path

    def _spawn_proc(self, argv, spawner=None, dry_run=True, check=True):
        """One CLI command line that may start a session, on the caller's own rail."""
        argv, env = self._spawn_argv(argv, spawner, dry_run)
        proc = hproc.run_state_manage(self.root, *argv, check=False, env=env)
        if check and proc.returncode != 0:
            raise AssertionError(f'{" ".join(argv)} failed (rc={proc.returncode}):\n'
                                 f'{proc.stdout}\n{proc.stderr}')
        return proc

    def _spawn_argv(self, argv, spawner, dry_run):
        """One spawn call's argv and child environment -- the suite's only decision about either.

        `--spawner` goes in when a caller named a stub. The drill switch comes out only for a run
        aimed at that stub: `dry_run=False` with no stub would reach this host's real spawner, so it
        is refused outright rather than trusted.
        """
        if spawner is not None:
            argv = [*argv, SPAWN_FLAG, str(spawner)]
        env = _child_env()
        if not dry_run:
            assert spawner is not None, (
                'a spawn with no drill and no stand-in spawner would reach this host\'s real one')
            env.pop(SPAWN_DRY_RUN, None)
        return argv, env

    def _run_tool_command(self, cmd, cwd=None):
        """Run one of conduct_pipeline's command lists.

        Those lists mix the two tools: `dag-manage status <node> completed` (the close marks the
        node) and `state-manage.py <verb> ...` (everything else the pipeline records). Both are
        dispatched here, through the tools' own functions -- the commands and their order are the
        real ones, only the two process starts are gone.
        """
        argv = [str(c) for c in cmd]
        if len(argv) > 1 and Path(argv[1]).name == 'dag-manage.py':
            assert Path(argv[1]).resolve() == Path(DAG_MANAGE).resolve(), \
                f'the tool aimed dag-manage at {argv[1]}, harness resolved {DAG_MANAGE}'
            proc = InProcessDagManage.handle(self.root, argv)
            if proc.returncode:
                raise RuntimeError(f'dag-manage {" ".join(argv[2:])} failed: {proc.stderr}')
            return proc.returncode
        return self._fast.run(argv, cwd)

    # ── the in-process call into conduct_pipeline.py ─────────────────────────────
    def _pipeline_call(self, project, mode, *, cycle_id=None, after=None, node=None,
                       spawner=None, dry_run=None):
        """Call the real module's plan / advance / settle with the sandbox's state-manage hooked in,
        so no process start is needed for the bookkeeping.

        One name per word: `plan` reads (and `after` is its closing rehearsal, handed back the way
        the CLI prints it -- a preview), `advance` claims, lands the material and opens the sessions,
        `settle` writes one node's end down.
        """
        with _dag_hook(self.root) as pipeline:
            pipeline.DAG_MANAGE = DAG_MANAGE
            # Installed only for this call and put back afterwards: `load_pipeline()` is shared with
            # the tests that drive `pipeline._run` on purpose, and a leftover substitution would aim
            # those at the wrong sandbox.
            with patched(pipeline, '_run', self._run_tool_command), chdir(self.root):
                if mode == 'plan':
                    action = pipeline.plan(self.root, cycle_id, after=after)
                    return self._preview(pipeline, action)
                if mode == 'advance':
                    receipt = pipeline.advance(self.root, cycle_id, spawner=spawner,
                                               dry_run=dry_run)
                    # The receipt's two action cells go through the same preview rule the reading
                    # does: a caller is handed an action, never a list of commands to run.
                    for key in ('plan', 'acted'):
                        if isinstance(receipt.get(key), dict):
                            receipt[key] = self._preview(pipeline, receipt[key])
                    return receipt
                if mode == 'settle':
                    return pipeline.settle(self.root, cycle_id, node)
        raise AssertionError(mode)

    @staticmethod
    def _preview(pipeline, action):
        """One action as the caller reads it: `render_action(preview=True)` parsed back."""
        return json.loads(pipeline.render_action(dict(action), as_json=True, preview=True))

    # ── readers ──────────────────────────────────────────────────────────────
    def state(self):
        return _decode(self.root / '.superworker' / 'state.json')

    def cycle(self, cycle_id='cyc'):
        return [c for c in self.state()['cycles'] if c['id'] == cycle_id][0]

    @staticmethod
    def _holding_ids_of(cycle):
        """The node ids this cycle holds, in claim order (the `holding` list's one reading)."""
        holding = cycle.get('holding')
        if isinstance(holding, list):
            return [item['id'] if isinstance(item, dict) else item for item in holding]
        return []

    def holding_ids(self, cycle_id='cyc'):
        """Every node the cycle holds right now, in claim order."""
        return self._holding_ids_of(self.cycle(cycle_id))

    def current_node(self, cycle_id='cyc'):
        """The first node the cycle holds, or None (the singular view of `holding`)."""
        holding = self.holding_ids(cycle_id)
        return holding[0] if holding else None

    def path(self, cycle_id='cyc'):
        """The nodes this cycle has finished, in order."""
        return self.cycle(cycle_id).get('path') or []

    def path_nodes(self, cycle_id='cyc'):
        return [step.get('node') for step in self.path(cycle_id)]

    def read_instructions(self):
        return _decode(self.root / '.superworker' / 'instructions.json') or {'instructions': []}

    def prompt_of(self, action):
        return Path(action['prompt']).read_text(encoding='utf-8')

    def state_sig(self, cycle_id='cyc'):
        """The real project state an action is allowed to move.

        Read straight off disk so a test can check movement without trusting the `progress` block
        the module reports about itself. Call it BEFORE the action too: the module's own contract
        check compares its pre-command snapshot with the post-command one, and that pre-command
        state is not reachable any more once the command has run.
        """
        cycle = self.cycle(cycle_id)
        node = self.current_node(cycle_id)
        return (node, len(cycle.get('path') or []),
                (self.read_signal(cycle_id, node) or {}).get('status') if node else None,
                cycle.get('status'))

    def files(self):
        """Every file under the project, keyed by relative path."""
        return {str(p.relative_to(self.root)).replace('\\', '/'): p
                for p in sorted(self.root.rglob('*')) if p.is_file()}

    def tree_hashes(self):
        """sha256 of every file under the project.

        The honest way to check "plan writes nothing": compare content, not just the file list.
        """
        return {rel: hashlib.sha256(p.read_bytes()).hexdigest()
                for rel, p in self.files().items()}

    def tree_contents(self):
        """Every file's bytes, for "the two paths produced the same project" comparisons."""
        return {rel: p.read_bytes() for rel, p in self.files().items()}

    # ── forge on-disk state the tools refuse to produce ──────────────────────
    def rewrite_cycle(self, **fields):
        """Write fields onto a cycle record by hand.

        One raw writer, for forging a cycle the tools will not write (a hand-edited path, a status
        the levers do not reach). Never used to advance the pipeline.
        """
        state = self.state()
        for cycle in state['cycles']:
            if cycle['id'] == fields.pop('id', 'cyc'):
                cycle.update(fields)
                break
        self.rewrite_state(state)

    def rewrite_state(self, state):
        (self.root / '.superworker' / 'state.json').write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
