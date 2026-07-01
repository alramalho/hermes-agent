# hermes-cli-bridge

Hermes gateway plugin for routing a chat thread into an interactive tmux-backed CLI session.

It registers `/codex` and `/claude` and uses `pre_gateway_dispatch` to handle chat-aware routing. The hook checks Hermes gateway authorization before it starts or writes to a tmux session.

## Install

```bash
pip install -e extra-plugins/hermes-cli-bridge
hermes plugins enable cli-bridge
```

The host must have `tmux` and the target CLI installed and authenticated.

## Commands

```text
/codex init [cwd]      Start Codex in cwd, or the configured default cwd.
/codex send <text>     Send text exactly, including CLI slash commands.
/codex status          Show the active session for this chat/user.
/codex end             Kill the tmux session.

/claude init [cwd]
/claude send <text>
/claude status
/claude end
```

While a bridge is active, ordinary non-slash chat messages are sent to the CLI. Hermes slash commands stay available. To send a slash command to the CLI, use `/codex send /command` or `/claude send /command`.

## Configuration

Environment variables:

```text
HERMES_CLI_BRIDGE_DEFAULT_CWD=/path/to/workspace
HERMES_CLI_BRIDGE_CODEX_CMD=codex
HERMES_CLI_BRIDGE_CODEX_BACKEND=tmux
HERMES_CLI_BRIDGE_CODEX_EXEC_CMD=codex exec
HERMES_CLI_BRIDGE_CLAUDE_CMD=claude
HERMES_CLI_BRIDGE_OUTPUT_INTERVAL=1.0
HERMES_CLI_BRIDGE_CHUNK_CHARS=3500
HERMES_CLI_BRIDGE_MAX_OUTPUT_CHARS=12000
HERMES_CLI_BRIDGE_EXEC_TIMEOUT=1800
HERMES_CLI_BRIDGE_OUTPUT_SOURCE=capture
HERMES_CLI_BRIDGE_RAW_LOG=0
HERMES_CLI_BRIDGE_LOG_SNIPPET_CHARS=50
HERMES_CLI_BRIDGE_AUDIT_LOG=1
HERMES_CLI_BRIDGE_AUDIT_LOG_PATH=~/.hermes/cli-bridge/events.jsonl
HERMES_CLI_BRIDGE_AUDIT_SNIPPET_CHARS=500
```

For a pinned Codex model, set the command explicitly, for example:

```text
HERMES_CLI_BRIDGE_CODEX_CMD=codex --model gpt-5.5 --no-alt-screen -c check_for_update_on_startup=false
```

`HERMES_CLI_BRIDGE_OUTPUT_SOURCE=capture` reads the rendered tmux pane, which avoids most TUI redraw noise. Set it to `pipe` only when you explicitly want raw appended terminal output.

Set `HERMES_CLI_BRIDGE_CODEX_BACKEND=exec` to route Codex prompts through
`codex exec` / `codex exec resume` instead of the interactive TUI. This is more
reliable for chat bridges because it does not depend on terminal keybindings to
submit the composer. `HERMES_CLI_BRIDGE_CODEX_CMD` is reused for model/config
flags; TUI-only flags such as `--no-alt-screen` are ignored for exec calls. Set
`HERMES_CLI_BRIDGE_CODEX_EXEC_CMD` if you want to provide the exact exec command.

Gateway INFO logs include redacted `input='first 50 chars...'` and
`output='first 50 chars...'` snippets. The JSONL audit log keeps capped, redacted
snapshots for debugging routing decisions without turning on raw terminal logging.
