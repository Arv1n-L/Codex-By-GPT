# Codex-By-GPT

> **One machine → one OpenAI Secure MCP Tunnel → one C2C Gateway → N workspaces**

This repository is a machine-wide C2C foundation for using ChatGPT as the planner/reviewer while Codex remains the executor.

## Architecture

```text
ChatGPT
   │
   │ one developer-mode app / connector
   ▼
OpenAI Secure MCP Tunnel
   │ outbound-only HTTPS
   ▼
C2C Gateway (127.0.0.1:8765/mcp)
   │
   ├── workspace A (read-only evidence)
   ├── workspace B (read-only evidence)
   └── workspace N (read-only evidence)

ChatGPT --submit_result--> bounded local mailbox --> Codex
Codex   --files/shell/git/tests--> workspace
Codex   --EXECUTED evidence--> local execution store --> ChatGPT
```

The Gateway never exposes arbitrary shell or filesystem writes to ChatGPT. `submit_result` can only append a typed result (`PLAN`, `REVIEW`, `DONE`, `BLOCKED`, `RESEARCH`) to the local C2C mailbox.

## Why this shape

- One connector per machine, not one connector per repository.
- One Secure MCP Tunnel per machine, not Cloudflare Quick Tunnels per workspace.
- Every MCP workspace operation requires an explicit `workspace_id`.
- Workspace roots are registered locally; model-supplied absolute paths do not choose authority.
- Sensitive files and path traversal are blocked.
- Codex remains the only component allowed to edit code, execute commands/tests, or mutate Git.

## Install

Requires Python 3.11+ and Git.

```sh
python -m pip install -e .
c2c doctor
```

## Register workspaces

```sh
c2c workspace add ~/src/factory-Agent --name factory-Agent
c2c workspace add ~/src/another-project --name another-project
c2c workspace list
```

The returned `workspace_id` is stable for a resolved local root.

## Run the machine gateway

```sh
c2c gateway serve
```

It binds to loopback only:

```text
http://127.0.0.1:8765/mcp
```

Health check:

```sh
curl http://127.0.0.1:8765/healthz
```

## Connect one OpenAI Secure MCP Tunnel

Create/associate a tunnel in OpenAI Platform first and obtain a runtime key through your organization's credential flow. Keep the runtime key private.

The official HTTP mode points the tunnel client to the local MCP URL. Generate the exact local command skeleton with:

```sh
c2c tunnel init-command --tunnel-id tunnel_xxx
```

Equivalent shape:

```sh
export CONTROL_PLANE_API_KEY="sk-..."
tunnel-client init \
  --sample sample_mcp_stdio_local \
  --profile codex-by-gpt \
  --tunnel-id tunnel_xxx \
  --mcp-server-url http://127.0.0.1:8765/mcp

tunnel-client doctor --profile codex-by-gpt --explain
tunnel-client run --profile codex-by-gpt
```

Then create **one** ChatGPT developer-mode app/connector using **Tunnel**, select that tunnel, and refresh its tool metadata while the local Gateway and tunnel client are healthy.

## MCP tools

Read-only data plane:

- `workspace_list`
- `workspace_info`
- `list_directory`
- `read_file`
- `search_workspace`
- `git_status`
- `git_diff`

Bounded control plane:

- `submit_result`

Read-only execution evidence:

- `wait_execution`

Example mailbox workflow:

```text
ChatGPT: submit_result(workspace_id, task_id, iteration, kind="PLAN", payload="...")
Codex:   c2c mailbox list --workspace <workspace_id> --task <task_id>
Codex:   c2c codex listen --workspace <workspace_id>
```

The listener owns acknowledgement for executable `PLAN` and `REVIEW` rows.
Use manual `c2c mailbox ack` only to dismiss a non-executable mailbox message,
not while an execution claim is outstanding.

## Run the C2C execution loop

Start one listener for the workspace that Codex is allowed to execute in:

```sh
c2c codex listen --workspace factory-Agent
```

The listener processes unacknowledged `PLAN` and `REVIEW` results in order. It
runs Codex with the registered workspace root as its fixed working directory,
stores bounded, secret-redacted JSONL execution evidence under the machine state directory, and
only then acknowledges the source mailbox result. `DONE`, `BLOCKED`, and
`RESEARCH` remain non-executable messages.

Before Codex starts, the listener durably claims the logical task iteration.
The normal order is claim, run Codex, persist terminal execution evidence,
clear the claim, then acknowledge the mailbox result. If a listener restarts
with a claim but no terminal execution, it does not automatically rerun
potentially mutating instructions. Instead it records `EXECUTED` evidence with
an unknown outcome so ChatGPT or the operator can inspect the workspace and
continue with a new iteration. This conservative recovery may also suppress a
safe retry if the listener stopped after claiming but before Codex started.
The listener sweeps claims before the unacknowledged mailbox queue, so recovery
still produces terminal evidence if the source row was already acknowledged.

`task_id` plus `iteration` identifies one logical C2C step. An exact network
retry reuses the original mailbox result, including after acknowledgement;
changed instructions must use the next iteration and otherwise fail closed.
The worker also skips legacy duplicate rows after one logical execution exists.
The execution store independently enforces the same logical task-iteration
uniqueness, even when source mailbox IDs differ.
This idempotency guarantee assumes the documented single-listener model; it is
not multi-worker coordination.

Starting the listener explicitly authorizes submitted `PLAN` and `REVIEW`
messages to trigger Codex execution inside that registered workspace. Mailbox
mutations are serialized with a machine-local cross-process lock.

ChatGPT can wait for the corresponding result with the read-only MCP tool:

```text
wait_execution(workspace_id, task_id, iteration, timeout_seconds=30)
```

The tool returns `PENDING` or an `EXECUTED` record containing the Codex exit
code, bounded and redacted execution output, and a bounded, redacted structured
test status. ChatGPT should then
independently inspect `git_diff`, `git_status`, and relevant files before
submitting `REVIEW` or `DONE`.

This is bounded long-polling, not an active callback that wakes an ended
ChatGPT turn. The listener is an explicit foreground process in v0.2.0; service
installation and multi-worker leases remain outside this release.

## Security boundary

ChatGPT does **not** receive tools for:

- writing/deleting files
- running shell commands
- installing packages
- committing/pushing Git
- arbitrary path access

The Gateway rejects `..` path escapes, skips high-noise/private directories, and blocks common secret/key file names and suffixes.

## Status

v0.2.1 adds task-iteration idempotency and a durable execution claim so
transport retries or listener restarts cannot automatically execute the same
logical step twice under the single-listener model. The next production-hardening layer
may add OS service installation, short-lived per-session capabilities, and
multi-worker leases.
