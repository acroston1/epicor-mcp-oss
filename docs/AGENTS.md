# Public documentation

Purpose: explain the project to operators who have only this repository.

## Entry points
- [design.md](design.md): architecture, authorization, retrieval, and rationale.
- [microsoft-sso.md](microsoft-sso.md): optional identity and configuration setup.
- [verification.md](verification.md): verification evidence and untested integrations.
- [Root README](../README.md): installation, imports, hosting, and troubleshooting.
- [Bridge README](../bridge/README.md): executable builds and client configuration.

## Invariants
- Examples are synthetic; credentials and installation settings are operator-supplied.
- Explain default SSO-disabled operation independently of optional Microsoft setup.
- Link to existing files; identify generated/private inputs explicitly in prose.
- Do not cite unavailable project notes, tickets, or private benchmarks.
- Preserve rationale with current functions and regression-test references.
- Distinguish offline tests from live, Windows, and production-model validation.

## Gotchas
- Markdown links resolve relative to their containing document.
- Capability changes need matching setup docs and tool descriptions.
- Guide pairs remain identical and <=60 lines.
