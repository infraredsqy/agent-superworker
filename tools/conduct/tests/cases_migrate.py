"""`migrate`: the one move out of the old record directory, and the states that refuse it.

What is worth a case here, and why each fails silently otherwise:

  * a workspace whose records moved keeps everything that was in them -- the records on LF, the
    merge attribute on the new name, the driver registered, and a `plan_path` that followed the
    directory (a value left naming the old one makes its owner recreate that directory)
  * the clone's own ignore line switches in the same step, and another project's line in the same
    file is left exactly as it was
  * the machine layer is **copied**, never over a file that is already at the new home
  * the lock file a 0.1.x run left behind is taken away -- the protocol now reads an empty
    `ledger.lock` as "someone is writing", so leaving it would make every later write wait out the
    timeout
  * the five refusals each stop before a byte moves, and each names the fact and an agent
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import CONDUCT_DIR, GIT, STATE_MANAGE, git, run_argv, shared_loader  # noqa: E402

__all__ = ['TestMigrate']

#: The record directory's `.gitattributes` as 0.1.x wrote it: the attribute names the old driver.
OLD_ATTRIBUTES = """\
# Records do not go through EOL conversion: the tool writes CRLF on Windows, and no machine's
# core.autocrlf should rewrite that.
** -text

ideas.json merge=pf
state.json merge=pf
signals/** merge=pf
"""

#: The marker 0.1.x planted above its `info/exclude` line.
OLD_MARK = '# pf working system: this ledger stays on this machine, never in the team repo'

#: One record as 0.1.x wrote it: the platform separator, byte for byte.
OLD_RECORD = '{\r\n  "cycles": [\r\n    {\r\n      "id": "cyc",\r\n      "title": "t",\r\n      "status": "inactive",\r\n      "holding": [],\r\n      "path": []\r\n    }\r\n  ]\r\n}'

#: One record that carries a `plan_path` naming the old directory (the shape its owner keeps).
OLD_POOL = '{\r\n  "issues": {\r\n    "i-1": {\r\n      "plan_path": ".pf/plans/i-1.md"\r\n    }\r\n  }\r\n}'

#: Somebody else's project in the same repository, whose ignore line may not be touched.
OTHER_LINE = '/elsewhere/.pf/'


def migrate(root: Path, *args, home: Path | None = None):
    argv = [STATE_MANAGE, 'migrate', *args, '--project', str(root)]
    if home is not None:
        argv += ['--home', str(home)]
    env = {k: v for k, v in os.environ.items()
           if k not in ('SUPERWORKER_HOME_DIR', 'SUPERWORKER_PLUGINS_DIR')}
    return run_argv(argv, env=env, check=False)


@unittest.skipUnless(GIT, 'git is not on PATH')
class TestMigrate(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='sw-migrate-case-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / 'proj'
        self.root.mkdir(parents=True)
        self.home = Path(tmp.name) / 'home'
        self.home.mkdir()
        git(self.root, 'init', '-b', 'main')

    # ── fixtures ─────────────────────────────────────────────────────────────
    def seed(self, *, marks: bool = True):
        """A record directory as 0.1.x laid it down: the layout files, two CRLF records, a stale lock
        and the usage note of that generation."""
        legacy = self.root / '.pf'
        legacy.mkdir()
        if marks:
            (legacy / '.gitignore').write_text('meta/\nworktrees/\ntemp/\n', encoding='utf-8')
            (legacy / '.gitattributes').write_text(OLD_ATTRIBUTES, encoding='utf-8')
        (legacy / 'state.json').write_bytes(OLD_RECORD.encode('utf-8'))
        (legacy / 'issues').mkdir()
        (legacy / 'issues' / 'issues.json').write_bytes(OLD_POOL.encode('utf-8'))
        (legacy / 'meta').mkdir()
        (legacy / 'meta' / 'ledger.lock').write_bytes(b'')
        contract = self.contract()
        (legacy / 'AGENTS.md').write_text(contract.LEGACY_AGENTS_BODY, encoding='utf-8')
        return legacy

    def contract(self):
        """`state/migrate.py` by path: the generation label and the note the move compares against."""
        return shared_loader().load_by_path(
            'sw_state_migrate_case', CONDUCT_DIR / 'state' / '__init__.py',
            package_dir=CONDUCT_DIR / 'state', register=True, reuse=True).migrate

    def exclude_path(self) -> Path:
        common = git(self.root, 'rev-parse', '--git-common-dir').stdout.strip()
        return (self.root / common).resolve() / 'info' / 'exclude'

    def plant_old_exclude(self):
        path = self.exclude_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'{OTHER_LINE}\n{OLD_MARK}\n/.pf/\n', encoding='utf-8')
        return path

    # ── the move ─────────────────────────────────────────────────────────────
    def test_the_records_move_and_every_attachment_follows(self):
        legacy = self.seed()
        exclude = self.plant_old_exclude()

        proc = migrate(self.root, '0.1.0', home=self.home)

        self.assertEqual(0, proc.returncode, proc.stderr)
        record = self.root / '.superworker'
        self.assertFalse(legacy.exists(), 'the old directory is still there')
        self.assertTrue((record / 'state.json').is_file())

        state = (record / 'state.json').read_bytes()
        self.assertNotIn(b'\r', state, 'a record still carries the 0.1.x separator')
        attributes = (record / '.gitattributes').read_text(encoding='utf-8')
        self.assertIn('ideas.json merge=superworker', attributes)
        self.assertNotIn('merge=pf', attributes)
        self.assertIn('superworker ledger three-way merge',
                      attributes + git(self.root, 'config', '--get', 'merge.superworker.name').stdout)

        lines = exclude.read_text(encoding='utf-8').splitlines()
        self.assertIn('/.superworker/', lines, 'the new line was not written')
        self.assertNotIn('/.pf/', lines, "the old line is still there")
        self.assertNotIn(OLD_MARK, lines, 'the old marker line is still there')
        self.assertIn(OTHER_LINE, lines, "another project's line was rewritten")

        pool = (record / 'issues' / 'issues.json').read_text(encoding='utf-8')
        self.assertIn('.superworker/plans/i-1.md', pool, 'a plan path still names the old directory')

        self.assertFalse((record / 'meta' / 'ledger.lock').exists(),
                         'the dead lock was carried into the migrated tree')
        self.assertNotIn('# .pf usage note', (record / 'AGENTS.md').read_text(encoding='utf-8'))

    def test_a_note_somebody_edited_is_left_alone(self):
        legacy = self.seed()
        mine = '# my own note\n\nask me before changing this\n'
        (legacy / 'AGENTS.md').write_text(mine, encoding='utf-8')

        proc = migrate(self.root, '0.1.0', home=self.home)

        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertEqual(mine, (self.root / '.superworker' / 'AGENTS.md').read_text(encoding='utf-8'))

    def test_the_machine_layer_is_copied_and_never_overwritten(self):
        self.seed()
        (self.home / '.pf').mkdir()
        (self.home / '.pf' / 'config.json').write_text('{"test_tier": "full"}', encoding='utf-8')

        proc = migrate(self.root, '0.1.0', home=self.home)

        self.assertEqual(0, proc.returncode, proc.stderr)
        target = self.home / '.superworker' / 'config.json'
        self.assertEqual('{"test_tier": "full"}', target.read_text(encoding='utf-8'))
        self.assertTrue((self.home / '.pf' / 'config.json').is_file(),
                        'the old machine layer was moved instead of copied')

        # A second workspace on the same machine: the new home is already there, so it is not rewritten.
        (target).write_text('{"test_tier": "lean"}', encoding='utf-8')
        other = Path(tempfile.mkdtemp(prefix='sw-migrate-other-')) / 'proj'
        other.mkdir(parents=True)
        git(other, 'init', '-b', 'main')
        (other / '.pf').mkdir()
        (other / '.pf' / '.gitignore').write_text('meta/\n', encoding='utf-8')
        (other / '.pf' / '.gitattributes').write_text('** -text\n', encoding='utf-8')
        proc = migrate(other, '0.1.0', home=self.home)
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertEqual('{"test_tier": "lean"}', target.read_text(encoding='utf-8'),
                         "the settings already at the new home were overwritten")

    # ── the quiet answer, and the refusals ───────────────────────────────────
    def test_naming_no_generation_lists_them_and_moves_nothing(self):
        legacy = self.seed()

        proc = migrate(self.root)

        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn('0.1.0', proc.stdout)
        self.assertTrue(legacy.is_dir(), 'the list of generations moved something')
        self.assertFalse((self.root / '.superworker').exists())

    def test_an_unknown_generation_refuses_before_anything_moves(self):
        legacy = self.seed()

        proc = migrate(self.root, '9.9.9')

        self.assertNotEqual(0, proc.returncode)
        self.assertIn('unknown generation', proc.stderr)
        self.assertTrue(legacy.is_dir())
        self.assertFalse((self.root / '.superworker').exists())

    def test_a_migrated_workspace_reads_as_empty_work(self):
        self.seed()
        self.assertEqual(0, migrate(self.root, '0.1.0', home=self.home).returncode)

        proc = migrate(self.root, '0.1.0', home=self.home)

        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn('nothing to move', proc.stdout)

    def test_both_directories_refuse(self):
        self.seed()
        (self.root / '.superworker').mkdir()
        (self.root / '.superworker' / '.gitignore').write_text('meta/\n', encoding='utf-8')
        (self.root / '.superworker' / '.gitattributes').write_text('** -text\n', encoding='utf-8')

        proc = migrate(self.root, '0.1.0', home=self.home)

        self.assertNotEqual(0, proc.returncode)
        self.assertIn('both', proc.stderr)
        self.assertIn('consult an agent', proc.stderr)
        self.assertTrue((self.root / '.pf' / 'state.json').is_file(), 'a refusal moved a byte')

    def test_an_old_directory_nobody_initialized_refuses(self):
        self.seed(marks=False)

        proc = migrate(self.root, '0.1.0', home=self.home)

        self.assertNotEqual(0, proc.returncode)
        self.assertIn('does not carry the marks', proc.stderr)
        self.assertTrue((self.root / '.pf').is_dir())

    def test_a_parallel_mode_worktree_refuses(self):
        legacy = self.seed()
        (legacy / 'worktrees' / 'abcd1234').mkdir(parents=True)

        proc = migrate(self.root, '0.1.0', home=self.home)

        self.assertNotEqual(0, proc.returncode)
        self.assertIn('parallel-mode worktree', proc.stderr)
        self.assertTrue((legacy / 'state.json').is_file())

    def test_a_live_old_generation_lock_refuses(self):
        """The file's presence alone is not the reading: 0.1.x left one behind after every run, and
        what it promised was the byte range it held on it."""
        legacy = self.seed()
        try:
            import msvcrt
        except ImportError:
            self.skipTest('the 0.1.x lock was a byte-range lock, which this platform asks another way')
        handle = open(legacy / 'meta' / 'ledger.lock', 'a+')
        self.addCleanup(handle.close)
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        try:
            proc = migrate(self.root, '0.1.0', home=self.home)
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

        self.assertNotEqual(0, proc.returncode)
        self.assertIn('holds the record lock', proc.stderr)
        self.assertTrue(legacy.is_dir(), 'a held lock did not stop the move')


if __name__ == '__main__':
    unittest.main()