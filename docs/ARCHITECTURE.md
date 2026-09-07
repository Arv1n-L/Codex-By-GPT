# Architecture decisions

## Authority

Workspace authority comes only from the local registry in `~/.codex-by-gpt/machine.json`. MCP callers select a registered `workspace_id`; they cannot introduce a new absolute root in a tool call.

## Data plane

Workspace tools are read-only and operate inside the selected registered root. Path normalization occurs before reads. Known secret names/key formats and noisy build/VCS directories are blocked.

## Control plane

`submit_result` is intentionally the only write-capable MCP tool. Its write target is not a workspace: it is the Gateway mailbox. Payload size and result kind are bounded.

## Transport

The Gateway binds to loopback. OpenAI Secure MCP Tunnel is the remote transport and initiates outbound HTTPS to OpenAI. No public inbound MCP URL is required.

## Execution

Codex is the executor. It remains responsible for file edits, shell commands, tests and Git mutation. ChatGPT is planner/reviewer/researcher and consumes evidence through MCP.

## Execution loop

The v0.2.0 listener processes only unacknowledged `PLAN` and `REVIEW` mailbox
records for one explicitly selected registered workspace. It fixes the Codex
working directory to that workspace root, records bounded stdout/stderr and a
schema-constrained test status in `executions.jsonl`, and acknowledges the
source result only after the execution record is durable.

Starting a workspace listener authorizes `PLAN` and `REVIEW` messages to trigger
Codex execution within that registered root. Mailbox read-modify-write
transactions are serialized across local processes, and all model-visible
execution output and test-status text is secret-redacted before it is persisted
or exposed through MCP.

`EXECUTED` is not an accepted `submit_result` kind, so ChatGPT cannot fabricate
executor evidence. ChatGPT reads it through the read-only `wait_execution` MCP
tool and cross-checks it against workspace files and Git state.

The wait tool uses a bounded long poll. It does not actively wake an ended
ChatGPT turn and does not introduce an external callback, queue, database, or
service manager.
