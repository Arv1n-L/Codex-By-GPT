# Architecture decisions

## Authority

Workspace authority comes only from the local registry in `~/.codex-by-gpt/machine.json`. MCP callers select a registered `workspace_id`; they cannot introduce a new absolute root in a tool call.

## Data plane

Workspace tools are read-only and operate inside the selected registered root. Path normalization occurs before reads. Known secret names/key formats and noisy build/VCS directories are blocked.

## Control plane

`submit_result` is intentionally the only write-capable MCP tool. Its write target is not a workspace: it is the Gateway mailbox. Payload size and result kind are bounded.

`(workspace_id, task_id, iteration)` identifies one logical submission. Exact
retries return the original result, including after acknowledgement; a changed
kind or payload for the same key fails closed and must use a higher iteration.

## Transport

The Gateway binds to loopback. OpenAI Secure MCP Tunnel is the remote transport and initiates outbound HTTPS to OpenAI. No public inbound MCP URL is required.

## Execution

Codex is the executor. It remains responsible for file edits, shell commands, tests and Git mutation. ChatGPT is planner/reviewer/researcher and consumes evidence through MCP.

## Execution loop

The v0.2.2 listener processes only unacknowledged `PLAN` and `REVIEW` mailbox
records for explicitly selected registered workspaces. It fixes each Codex run
to that workspace root, records bounded stdout/stderr and a schema-constrained
test status in `executions.jsonl`, and acknowledges the source result only after
the execution record is durable. One process may poll several selected
workspaces, but dispatch remains serial.

Each selected workspace has an OS-backed ownership lock held for the complete
listener lifetime. Overlapping listeners fail before mailbox processing, while
listeners for different workspaces can coexist. Lock release belongs to the OS,
so crash recovery does not depend on deleting a PID file.

The single-listener execution lifecycle is:

```text
durable claim -> run Codex -> durable terminal execution -> clear claim -> ack source
```

Claims are internal JSONL machine state keyed by
`(workspace_id, task_id, iteration)`. A claim that survives without a terminal
execution means the prior listener may have stopped after Codex began mutating
the workspace. Recovery therefore fails closed: it does not rerun Codex, but
persists terminal `EXECUTED` evidence with an unknown outcome, clears the claim,
and acknowledges the source. A terminal execution plus a leftover claim is
cleaned up and acknowledged without rerunning. This deliberately prefers a
possible false interruption over duplicate workspace mutation.

Each listener pass sweeps claims for its registered workspace before reading
the unacknowledged mailbox queue. Claim recovery therefore does not depend on
the source row still being unacknowledged; a prematurely acknowledged source
cannot strand the logical iteration in `PENDING`. Manual acknowledgement is
reserved for non-executable messages because the listener owns acknowledgement
of `PLAN` and `REVIEW` rows.

Before running Codex, the worker also checks for an existing execution with the
same logical key. This prevents pre-v0.2.1 duplicate mailbox rows from causing
a second execution; it is defense-in-depth behind enforced listener ownership,
not a multi-worker lease. The execution store also reuses an existing terminal row
for that logical key even when a legacy duplicate has a different source ID.

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

## Runtime observability

`c2c status` reads the gateway health endpoint, observes the local tunnel
process, probes listener ownership, summarizes per-workspace pending work,
claims, and the latest execution, and validates each JSONL file. `c2c doctor`
adds diagnostics for missing runtime components, invalid roots or state, stale
gateway processes, and claims that require recovery. Neither command mutates
mailbox or execution state.
