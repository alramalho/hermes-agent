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
/codex init [name] [--cwd <cwd>]  Start or attach a named Codex session.
/codex list [--all]               Show sessions for this chat/user.
/codex select <name|none>         Choose the current session, or clear it.
/codex rename <new-name>          Rename the current bridge session.
/codex send <text>                Send text exactly, including CLI slash commands.
/codex status [name|current]      Show session status.
/codex exit                       Exit the current bridge without killing it.
/codex kill [name|current]        Kill a session. `/codex end` aliases this.

/claude init [name] [--cwd <cwd>]
/claude list [--all]
/claude select <name|none>
/claude rename <new-name>
/claude send <text>
/claude status [name|current]
/claude exit
/claude kill [name|current]
```

If you omit `name`, the bridge uses `default`. `current` is reserved as a
hotword for the selected session. `--cwd` replaces the old positional cwd style,
though `/codex init /path/to/repo` still works for compatibility.

Bridge sessions are persisted in `~/.hermes/cli-bridge/sessions.json` so a
gateway/plugin restart can reattach to still-running tmux sessions and continue
exec-backed provider threads. Dead tmux sessions are pruned the next time they
are listed or selected. `/codex list --all` and `/claude list --all` show Codex
and Claude bridge sessions for the same chat/user scope, including sessions in
other Telegram topics. Unicode dash variants such as `/codex list —all` are
accepted too, because some Telegram clients autocorrect `--`.

In Telegram DMs, `/codex init api --cwd <cwd>` or `/claude init docs --cwd <cwd>`
from the main chat automatically creates/reuses a DM topic named `codex: api` or
`claude: docs`, starts the bridge there, and sends bridge output back to that
topic. Running `init` inside an existing Telegram topic keeps using that topic.
Set `HERMES_CLI_BRIDGE_TELEGRAM_TOPICS=0` to disable automatic topic creation.

When a current bridge is selected, ordinary non-slash chat messages are sent to
the CLI. Use `/codex select none` or `/claude select none` to keep sessions alive
while sending ordinary messages back to Hermes. Hermes slash commands stay
available. To send a slash command to the CLI, use `/codex send /command` or
`/claude send /command`.

## Slash command forwarding

Hermes keeps ownership of top-level slash commands. That means `/model` still
opens Hermes model selection, even when a Codex bridge is selected. Prefix CLI
slash commands with `/codex send`:

```text
/codex init api --cwd /path/to/repo
/codex send /model
/codex send /rename backend-session
```

Use `/codex rename backend-session` when you want to rename Hermes' bridge route;
use `/codex send /rename backend-session` when you want Codex to rename its own
conversation.

## Configuration

Environment variables:

```text
HERMES_CLI_BRIDGE_DEFAULT_CWD=/path/to/workspace
HERMES_CLI_BRIDGE_CODEX_CMD=codex
HERMES_CLI_BRIDGE_CODEX_BACKEND=tmux
HERMES_CLI_BRIDGE_CODEX_EXEC_CMD=codex exec
HERMES_CLI_BRIDGE_CLAUDE_CMD=claude
HERMES_CLI_BRIDGE_CLAUDE_BACKEND=tmux
HERMES_CLI_BRIDGE_CLAUDE_EXEC_CMD=claude
HERMES_CLI_BRIDGE_OUTPUT_INTERVAL=1.0
HERMES_CLI_BRIDGE_CHUNK_CHARS=3500
HERMES_CLI_BRIDGE_MAX_OUTPUT_CHARS=12000
HERMES_CLI_BRIDGE_EXEC_TIMEOUT=1800
HERMES_CLI_BRIDGE_STARTUP_READY_TIMEOUT=20
HERMES_CLI_BRIDGE_TRANSCRIBE_VOICE=1
HERMES_CLI_BRIDGE_TRANSCRIBE_TIMEOUT=120
HERMES_CLI_BRIDGE_TMUX_SUBMIT_KEYS=Escape,Enter
HERMES_CLI_BRIDGE_CODEX_SUBMIT_KEYS=Escape,Enter
HERMES_CLI_BRIDGE_TMUX_KEY_DELAY=0.15
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

Codex tmux submission defaults to `Escape,Enter`. That normalizes the composer
state before submitting and works across Codex terminal modes that otherwise
treat a lone `Enter` or `C-m` as an inserted newline. Claude Code submits with
a plain `Enter` (an `Escape` there would clear the composer or interrupt a
running turn). Override with `HERMES_CLI_BRIDGE_<AGENT>_SUBMIT_KEYS` or the
generic `HERMES_CLI_BRIDGE_TMUX_SUBMIT_KEYS` when a target CLI needs different
keys.
`HERMES_CLI_BRIDGE_TMUX_KEY_DELAY` adds a small pause between keys so terminal
UIs can process mode changes such as `Escape` before the submit key arrives.
`HERMES_CLI_BRIDGE_STARTUP_READY_TIMEOUT` makes `/codex init` wait for the
initial Codex prompt to be ready, so the next Telegram message is not typed
while the TUI is still loading.

When the Codex or Claude tmux UI asks for permission, the bridge reuses Hermes'
existing Telegram approval buttons. For Codex, `Allow Once` sends `y`; `Session`
and `Always` send the key shown by Codex's "don't ask again" option, such as `a`
for file edits or `p` for command-pattern approvals. For Claude Code, `Allow
Once` presses `1` and `Session`/`Always` press `2` when the dialog offers a
persistent "Yes, …" option (falling back to `1` when it does not, e.g. the
folder-trust dialog). `Deny` sends `Escape` back to the tmux pane for both.

First-run trust prompts ("Do you trust the contents of this directory?" for
Codex, the "Quick safety check" workspace prompt for Claude) are surfaced
through the same approval buttons, so `/codex init` and `/claude init` in a new
directory no longer hang until the startup timeout.

Set `HERMES_CLI_BRIDGE_CODEX_BACKEND=exec` to route Codex prompts through
`codex exec` / `codex exec resume` instead of the interactive TUI. This is more
reliable for chat bridges because it does not depend on terminal keybindings to
submit the composer. `HERMES_CLI_BRIDGE_CODEX_CMD` is reused for model/config
flags; TUI-only flags such as `--no-alt-screen` are ignored for exec calls. Set
`HERMES_CLI_BRIDGE_CODEX_EXEC_CMD` if you want to provide the exact exec command.

Set `HERMES_CLI_BRIDGE_CLAUDE_BACKEND=exec` for the Claude equivalent: prompts
run through `claude --print --output-format json`, threads continue via
`--resume <session-id>`, and the reply text comes from the JSON `result` field.
`HERMES_CLI_BRIDGE_CLAUDE_CMD` is reused for flags (`-p`/`--print`/`--continue`
are stripped); set `HERMES_CLI_BRIDGE_CLAUDE_EXEC_CMD` to override the exact
command.

Telegram voice messages are transcribed through Hermes's normal STT pipeline
before the prompt is sent to the CLI — on the exec backends and on tmux
sessions alike (tmux voice sends happen on a worker thread so the gateway loop
is never blocked). Uploaded audio files remain attachment path notes so the CLI
can decide how to process them.

Gateway INFO logs include redacted `input='first 50 chars...'` and
`output='first 50 chars...'` snippets. The JSONL audit log keeps capped, redacted
snapshots for debugging routing decisions without turning on raw terminal logging.
