# Python source

Purpose: hold the installable package in a standard src layout.

## Entry points
- [Runtime router](epicor_mcp/AGENTS.md): entrypoints and integration-package contracts.
- [Project metadata](../pyproject.toml): dependencies, extras, and console entrypoints.
- Install from the root with `python -m pip install -e '.[dev]'`.

## Invariants
- Runtime code lives in `epicor_mcp`; operator scripts remain outside the package.
- Never package private generated data.
- The server exposes exactly five public MCP tools.
- Read each integration's guide before changing its contract.
- Guide pairs remain identical and <=60 lines.
