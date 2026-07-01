# Hermes Terminal Relay Plugin

Route messages from a Hermes gateway chat (Telegram, Discord, etc.) into a persistent `tmux` session running Codex or Claude Code.

## Commands

- `/relay_start codex [cwd]` — start a Codex relay in `cwd`.
- `/relay_start claude [cwd]` — start a Claude Code relay in `cwd`.
- `/codex_init [cwd]` / `/claude_init [cwd]` — aliases.
- `/relay_status` — show active relay state for the chat/topic.
- `/relay_capture` — capture the latest tmux pane output.
- `/relay_end` — kill the tmux session and restore normal Hermes chat.
- `/codex_end` / `/claude_end` — backend-specific end commands.

While a relay is active, normal non-slash messages in that same chat/topic are sent directly to the tmux session and Hermes does not run its normal agent loop. Unknown slash commands are not hijacked.

Relay output is captured from the terminal screen because interactive Codex/Claude Code TUIs do not expose a structured "latest assistant reply" protocol. The plugin captures the pane before and after each sent message and returns only the changed lines; `/relay_capture` still shows the current pane tail when you need more context.

## Environment overrides

- `HERMES_RELAY_DEFAULT_CWD` — default working directory; defaults to `$HOME`.
- `HERMES_RELAY_CODEX_CMD` — command used for Codex; defaults to `codex`.
- `HERMES_RELAY_CLAUDE_CMD` — command used for Claude Code; defaults to `claude`.
- `HERMES_RELAY_CAPTURE_LINES` — tmux lines to capture; default `90`.
- `HERMES_RELAY_CAPTURE_DELAY` — seconds to wait after sending input before capture; default `1.5`.
- `HERMES_RELAY_MAX_REPLY_CHARS` — max output returned to chat; default `3500`.

## Notes

This is a user plugin, not a fork of Hermes core. It uses supported extension surfaces:

- `ctx.register_command()` for discoverable slash commands.
- `pre_gateway_dispatch` to intercept gateway messages before normal Hermes dispatch.
- `tmux` for PTY session control.

After enabling or changing the plugin, restart the gateway:

```bash
hermes gateway restart
```

## Development

This directory is a standalone git repo so it can be pushed to GitHub separately or vendored into a Hermes fork later.
