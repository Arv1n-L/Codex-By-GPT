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
