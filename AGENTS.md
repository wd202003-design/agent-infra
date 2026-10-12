# Repository instructions

## Scope

These instructions apply to the entire repository. Read README.md and the relevant docs before changes.
The repository includes an isolated sandbox PoC under poc/sandbox, plus design documents and scaffolding. A single-process development task service is available under devserver; it is not the complete platform. Read ADR 0002 for its temporary SQLite/Python scope. Formal modules now live in control-plane, runner, contracts and web; read ADR 0004 and docs/implementation-status.md for the implemented subset and remaining gates. See ADR 0001 and the PoC README before execution changes.

## Product constraints

- Preserve the server/local/online-edge unified Runner model.
- Separate real concurrent tasks, active tasks, simulated connections, and model concurrency.
- Do not describe acceptance targets as measured capabilities.
- Keep workflow engine, Runtime, language and gateway choices pending until an ADR records the decision.
- Use Chinese for project documentation unless a technical contract requires otherwise.

## Implementation

- Start with the smallest runnable single-task loop.
- Keep state authority, retry ownership and execution epochs explicit.
- Never automatically retry an external write with an unknown outcome.
- Enforce filesystem, network and tool permissions outside model prompts.
- Never commit credentials or user/production data.
- Document new startup commands and configuration alongside implementation.
- Test meaningful failure and authorization behavior; report what was actually run.
- Keep changes scoped; preserve unrelated work and avoid destructive Git operations.
