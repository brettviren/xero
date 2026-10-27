# Project instructions

## Overview

This provides a Python package with CLI entry `xero` to manage
topic-oriented, multi-repo, multi-Spack-env development areas: bare repos with
one worktree per ref, topics described by `topic.toml` and built per Spack env.
README.org is the reference; `docs/howtos/*.org` are short command recipes.

Tests: `uv venv && uv pip install -e . pytest && .venv/bin/pytest`

The first real area is `~/dune/xero` (its `extern/` is a symlink to the legacy
`~/dune/xerosere/extern`, so Spack commands there change the legacy area too).

## Issues and tasks

This project uses **bd (beads)** for issue tracking.  `.beads/` is local only
(git-ignored).

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

- Use `bd` for ALL task tracking
- Run `bd prime` for detailed command reference
- Use `bd remember` for persistent knowledge
