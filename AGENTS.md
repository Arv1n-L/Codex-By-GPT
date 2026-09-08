# Codex-By-GPT workflow

- ChatGPT owns planning, review, and root-cause judgement; Codex owns execution.
- Work only in registered workspaces and keep MCP access least-privileged.
- Do not commit or push without explicit user authorization.
- Keep task and iteration handling idempotent and make the smallest coherent change.
- Run targeted tests, the relevant regression suite, `compileall`, and `git diff --check` after changes.
- Report only verification that was actually executed, including unresolved limitations.
