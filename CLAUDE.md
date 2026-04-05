# CLAUDE.md

## Purpose

`telegram-agent-notify` is a small local wrapper around terminal-based coding
agents. Its job is to launch an agent command, watch for the agent to finish a
task, and send a Telegram message to a configured chat.

The project is intentionally narrow:

- it is designed for a developer laptop, not a server
- it assumes the agent runs interactively in a terminal
- it does not integrate with Codex, Claude Code, or Telegram through any SDKs
- it uses only the Python standard library plus a small shell helper

The core use case is:

1. Start a local coding agent through this wrapper.
2. Submit a prompt in the agent UI.
3. Leave the terminal alone.
4. Receive a Telegram notification when the agent appears to be done and ready
   for the next prompt.

## High-Level Design

There are two operating modes.

### 1. Process-exit mode

For ordinary commands, the wrapper simply runs the command with
`subprocess.run(...)`, waits for the process to exit, and then sends a Telegram
message.

This is used for:

- non-interactive commands
- one-shot scripts
- agent commands where "finished" means "the process exited"

### 2. Interactive ready-state mode

For interactive agents like Codex, process exit is the wrong signal because the
agent often stays open for multiple tasks. In that case the wrapper launches the
 agent inside a pseudo-terminal and watches the terminal stream.

Instead of notifying on process exit, it tries to infer:

- the user submitted a prompt
- the agent processed it
- the UI returned to an input-ready state

When that happens, the wrapper sends a Telegram message even though the agent
process is still running.

## Entry Points

### Shell helper

The main user-facing entrypoint is:

[bin/agent-notify](/Users/johncosnett/PycharmProjects/telegram-agent-notify/bin/agent-notify)

This script:

- accepts a single quoted command string
- chooses a display name based on the command
- enables interactive ready-state watching automatically for Codex
- delegates execution to the Python module

Current command routing rules:

- commands starting with `codex`:
  - name is set to `codex`
  - `--watch-ready` is enabled
- commands starting with `claude`:
  - name is set to `claude`
  - no special interactive watcher is enabled yet
- anything else:
  - name defaults to `agent`
  - the command is run in plain process-exit mode

### Python module

The actual implementation lives in:

[cli.py](/Users/johncosnett/PycharmProjects/telegram-agent-notify/src/telegram_agent_notify/cli.py)

The module entrypoint is:

[__main__.py](/Users/johncosnett/PycharmProjects/telegram-agent-notify/src/telegram_agent_notify/__main__.py)

It supports:

- `--test-telegram`
- `--shell`
- `--name`
- `--watch-ready`

## Configuration

The notifier reads Telegram credentials from either shell environment variables
or a local `.env` file in the project root.

Accepted variable names:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `BOT_TOKEN`
- `CHAT_ID`

The loader is intentionally simple:

- it reads `.env` line by line
- it ignores blank lines and comment lines
- it splits on the first `=`
- it does not override variables that are already present in the shell
- it does not implement full dotenv parsing semantics

This means the `.env` file should stay simple. Plain `KEY=value` lines are the
safest format.

The example config lives in:

[.env.example](/Users/johncosnett/PycharmProjects/telegram-agent-notify/.env.example)

## Telegram Delivery

Telegram messages are sent by direct HTTP POST to the Bot API endpoint:

`https://api.telegram.org/bot<TOKEN>/sendMessage`

There is no Telegram client library in the project. The implementation uses:

- `urllib.request`
- `urllib.parse`

The message body currently includes:

- the agent name
- the event text
- elapsed time
- command text
- host name

If the wrapper is notifying on process exit, it also includes:

- success or failure status
- numeric exit code

## How Codex Detection Works

The Codex-specific behavior is the most important part of the project.

When `--watch-ready` is enabled, the wrapper:

1. launches the target command inside a PTY using `pty.fork()`
2. forwards stdin from the user terminal to the child PTY
3. forwards stdout from the child PTY back to the user terminal
4. tracks what the user typed before pressing Enter
5. tracks the output that appears after that submission
6. decides whether the agent has returned to a ready-for-input state

### Why a PTY is required

Interactive terminal apps usually behave differently when not attached to a
real terminal. A PTY keeps the child process in interactive mode and preserves
the normal terminal UX.

The code also synchronizes terminal size changes between the outer terminal and
the child PTY so interactive rendering works properly on resize.

### What counts as a submitted task

The wrapper marks a task as pending when:

- the user types at least one printable character
- then presses Enter

It does not try to understand agent slash commands or multiline editors. It only
tracks the terminal stream at a basic level.

### What counts as task completion

After a task submission, the wrapper currently treats any of the following as a
completion signal:

- the output contains `Ready.`
- the output contains `Token usage:`
- the output contains `To continue this session, run codex resume`
- the output shows the prompt returning after the task output

The prompt-return heuristic exists because Codex does not always print the same
final marker. In some sessions it visibly returns to a new prompt without
printing `Ready.`.

### Prompt-return heuristic

The code strips ANSI escape sequences from the terminal stream and then looks
for prompt lines using this pattern:

- a line starting with `› `
- or a line starting with `> `

That is implemented with `PROMPT_LINE_RE`.

After the user submits a prompt, the wrapper watches for a new prompt line in
the subsequent output. If a prompt comes back and it is not just the same echoed
prompt text the user typed, the wrapper treats that as "the agent is ready for
the next task."

This is heuristic, not protocol-level integration. It can break if Codex
changes its terminal UI.

## Claude Code Support

Claude Code currently works only in process-exit mode through the helper
script's naming shortcut.

That means:

- the wrapper labels the notification as `claude`
- the wrapper does not yet have a Claude-specific interactive completion
  detector
- a notification is sent when the `claude` process exits

If Claude Code should support long-lived interactive notifications similar to
Codex, the next step would be to study Claude's terminal output and add a
separate ready-state heuristic.

## Main Files

### [bin/agent-notify](/Users/johncosnett/PycharmProjects/telegram-agent-notify/bin/agent-notify)

Small shell wrapper used by humans. It keeps the common invocation short.

### [src/telegram_agent_notify/cli.py](/Users/johncosnett/PycharmProjects/telegram-agent-notify/src/telegram_agent_notify/cli.py)

Contains almost all runtime logic:

- config loading
- argument parsing
- Telegram sending
- process-exit mode
- interactive PTY mode
- task completion heuristics

### [src/telegram_agent_notify/__main__.py](/Users/johncosnett/PycharmProjects/telegram-agent-notify/src/telegram_agent_notify/__main__.py)

Makes `python3 -m telegram_agent_notify` work.

### [README.md](/Users/johncosnett/PycharmProjects/telegram-agent-notify/README.md)

Short user-facing instructions.

### [INTENTION.md](/Users/johncosnett/PycharmProjects/telegram-agent-notify/INTENTION.md)

Original short statement of project scope.

### [pyproject.toml](/Users/johncosnett/PycharmProjects/telegram-agent-notify/pyproject.toml)

Minimal package metadata and console script definition.

## Known Limitations

- Completion detection is heuristic and specific to current Codex terminal
  behavior.
- The current `.env` parser is intentionally basic and not fully dotenv
  compatible.
- The helper script still relies on `PYTHONPATH=src` rather than an installed
  package.
- There is no persistent logging or debug trace of raw terminal output.
- Telegram delivery failures are only surfaced as stderr warnings during runtime.
- There is no retry logic for transient Telegram API failures.
- There are no automated tests yet.

## Suggested Next Improvements

- install the package in editable mode and remove the `PYTHONPATH=src`
  requirement from user-facing commands
- add a `--debug` mode that writes sanitized terminal output and matched
  completion markers to a log file
- add tests around:
  - dotenv loading
  - message formatting
  - completion-marker detection
- add a Claude-specific interactive detector if Claude stays alive across tasks
- document the exact Telegram bot setup flow in more detail

## Mental Model For Future Editors

If you need to modify this project, the key design assumption is:

`finished` does not necessarily mean `process exited`

For interactive coding agents, the important problem is detecting when the
terminal UI returns to a state where the human can submit the next task. Nearly
all of the complexity in this project exists because of that distinction.
