# Security

## Reporting

Use GitHub's private advisory form:

https://github.com/infraredsqy/agent-superworker/security/advisories/new

Please do not open a public issue for anything exploitable.

## What to send

`sw version` output, the command you ran, and what you expected instead. For the installer, add
`sw doctor` output.

## In scope

- `tools/bin`, `tools/dag`, `tools/conduct`, `tools/shared` -- the `sw` command and what it runs
- `tools/conduct/spawn/` -- the spawners: they open sessions through the local DSH API on a cookie,
  and a session spends real tokens
- `install.ps1` -- it links skills into a host's root (`~/.zcode/skills` for `-SkillsFor zcode`)
  and puts `tools/bin` on `PATH`
- `skills/` -- the method text a session works under
- `plugins/` -- a plugin's own tools and skills

## Out of scope

- A project whose files you control anyway: `sw init` writes `.superworker/` into it, and hand-editing that
  is yours to do.
- Running your own commands with your own tokens. That is the pipeline's job.

## Supported versions

The current `main`. Fixes land as commits, not as backports.

## Disclosure

You get an answer as soon as one is possible, with no promised timeline. Credit unless you ask
otherwise.