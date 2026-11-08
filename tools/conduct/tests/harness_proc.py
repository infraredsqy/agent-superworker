"""The real subprocesses and the CLI-call helpers: `_run_proc`, `run_state_manage`, `run_dag`,
`run_signal`, `run_handoff`, plus the small CLI helper `_json_out`.
`run_state_manage` stays a module-level global: the test suite patches it through this module
object, and every call site must see the patch.
"""

from __future__ import annotations

import json
from pathlib import Path

from harness_env import DAG_MANAGE, HANDOFF_PY, SIGNAL_PY, STATE_MANAGE, run_argv

__all__ = [
    "_run_proc", "run_state_manage", "run_dag", "run_signal", "run_handoff",
    "_json_out",
]


# ═══════════════════════════════════════════════════════════════════════════════
#  Real subprocesses (the CLI surface, and the two worker commands)
# ═══════════════════════════════════════════════════════════════════════════════

def _run_proc(project: Path, argv, check: bool = True, env=None):
    """One real child process against `project`, through the suite's one runner."""
    return run_argv(argv, cwd=project, env=env, check=check)


def run_state_manage(project: Path, *args, check: bool = True, env=None):
    """The real state-manage.py, as a process. Used by the CLI-subject tests."""
    return _run_proc(project, [STATE_MANAGE, *args], check=check, env=env)


def run_dag(project: Path, *args, check: bool = True):
    return _run_proc(project, [DAG_MANAGE, *args], check=check)


def run_signal(project: Path, cycle_id, node_id, status, summary='', outputs=None, check=True):
    """What a worker session's last act is: write the node's signal. The real signal.py.

    `python signal.py <cycle> <node> <status> -s "..." -o '{...}'`.
    """
    return _run_proc(project, [SIGNAL_PY, cycle_id, node_id, status,
                               '-s', summary,
                               '-o', json.dumps(outputs or {}, ensure_ascii=False)], check=check)


def run_handoff(project: Path, cycle_id, node_id, status, summary='', outputs=None,
                check=True):
    """The worker's one report: write the node's ticket. The close is `settle <node>`.

    `python handoff.py <cycle> <node> <status> -s "..." -o '{...}'`. It opens no session -- the
    session half is `advance`'s / `dispatch`'s -- so there is no drill flag to hold.
    """
    argv = [HANDOFF_PY, cycle_id, node_id, status,
            '-s', summary, '-o', json.dumps(outputs or {}, ensure_ascii=False)]
    return _run_proc(project, argv, check=check)


# ═══════════════════════════════════════════════════════════════════════════════
#  Small pieces the sandbox leans on
# ═══════════════════════════════════════════════════════════════════════════════

def _json_out(proc, argv):
    """Parse a tool's stdout, refusing to guess when the child produced no JSON at all.

    A `None` stdout means the child's output could not be decoded by the parent. Saying so here
    beats a `TypeError: ... not NoneType` three frames further down.
    """
    if proc.stdout is None:
        raise AssertionError(f'no stdout could be read from: {" ".join(map(str, argv))}\n'
                             f'rc={proc.returncode}\nstderr={proc.stderr!r}')
    return json.loads(proc.stdout)
