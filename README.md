# agent-superworker

English | [中文](README.zh.md)

`agent-superworker` (`sw`) is a command that keeps a project's todo graph, and a pipeline that
hands one node of it at a time to a coding agent.

Ideas go into a DAG: a node is a piece of work, an edge is a dependency. A **cycle** takes the
node that matters most right now, gives it to a fresh agent session, and records what came back.
The session works under the `sw-conduct` skill — prepare, execute, independent review, fix, close
— and hands back one line saying whether it delivered.

Everything the pipeline knows lives in `.superworker/`.

## Status

Early, and moving fast. Expect breaking changes, and expect file formats to shift under you.

`sw advance` and `sw dispatch` start real agent sessions that spend real tokens.

## Install

Needs Python 3.10+, git, and PowerShell on Windows. From this checkout:

```pwsh
git clone https://github.com/infraredsqy/agent-superworker/
cd agent-superworker
pwsh -File install.ps1 -SkillsFor none    # no skill links; -SkillsFor zcode links them
```

The script has no default host, because a skill root belongs to one: `-SkillsFor zcode` links the
`sw-*` skills into `~/.zcode/skills`, ZCode's own root, `-SkillsFor none` only puts `tools/bin` on
your `PATH`. `-SkillsFor dsh` is refused on purpose — the DSH plugin installs its own skills, in
DSH's shape, into `~/.dsh/skills`. The two roots are disjoint: ZCode scans `~/.zcode/skills` first and
the compatibility `~/.agents/skills` after it, and this script writes only the first one, so a
same-named skill never has two homes. Both hosts' installs drop any other checkout's `PATH` entry, so
`sw` resolves here; re-run the script after moving the checkout, it is idempotent, and its closing
self-check includes `sw doctor`. `-Check` prints every link, `PATH` and record change it would make
and writes nothing.

The skills ship in both languages — one language per install, because a method name has exactly one
skill. The default is English; add `-Lang zh` for the Chinese ones. The choice is recorded in
`~/.superworker/lang.txt`, so a later re-run repairs the install without switching the language you picked.

```pwsh
sw version     # where this checkout lives
sw doctor      # whether this checkout and the current project look right
```

## Use it

In the project you want to work on:

```pwsh
sw init                                        # put .superworker/ in place, register the merge driver
sw dag add "billing page" --tags coding
sw dag add "rate-limit the API" --depends-on <uuid>
sw dag list                                    # sorted by urgency × importance
```

Then start a line and let it open the first session:

```pwsh
sw cycle start billing --title "billing" --tags coding
sw advance
```

The pipeline is one verb per operation: `sw plan` reads the next step and writes nothing, `sw hold`
claims a node, `sw settle` records one node's end, `sw dispatch` opens the sessions of the cells a
line holds, and `sw advance` is claim, material and session in one word — the one a host or a
person runs to move a line on.

## Skills

Worker sessions are skills, linked into `~/.zcode/skills`:

| Skill | What it does |
|---|---|
| `sw-route` | talk a request into a node; edit and read the graph |
| `sw-conduct` | run one node: prepare, execute, independent review, fix, close |
| `sw-instruct` | record a standing instruction that later sessions follow |

Four more skills come with the plugins — `sw-issue-scan`, `sw-issue-discuss`, `sw-issue-repair` and
`sw-test-audit`. A plugin's skills live in that plugin's folder (`plugins/<name>/skills/`) and are
linked and removed with the plugin, so `sw doctor` is where a checkout's own set is listed. A skill
names no skill from another plugin — it says "run `sw doctor`" instead.

## Relationship to DSH

Another implementation of the same record layer ships with DeepSeek Harness as the
`dsh-superworker` plugin: one `.superworker/` layout, one byte rule for the records, one merge
driver, so a project's records can be read and written from either side. The flow words agree on
both sides — `plan` reads, `hold` claims, `settle` records one node's end, `advance` claims the
free slots and opens the sessions, `dispatch` only opens — while each side keeps its own route for
what opens a session (this command hands that to a spawner script; DSH's host opens it itself). The
skill trees are separate too: the plugin writes DSH's into `~/.dsh/skills`, and the links this script
makes go to `~/.zcode/skills`. Neither host scans the other's root, so a machine carrying both reads
each implementation's skills in the shape that host can run — and neither root ever needs the other's
files to make sense.

## Development

Each tool's suite runs from `tools/` — nothing is discovered under the `tools/` root itself:

```pwsh
cd tools
python -m unittest discover -s dag/tests -t .
python -m unittest discover -s shared/tests -t .
python -m unittest discover -s conduct/tests -t .
```

A plugin's suite is the plugin's own: it runs from the checkout root, with the plugin's `tools/` as
the top level — a plugin is a folder, and its tests move with it.

```pwsh
python -m unittest discover -s plugins/issue/tools/tests -t plugins/issue/tools
```

The contract docs live in `docs/` (Chinese for now).

## License

[MIT](LICENSE)