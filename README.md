# telegram-agent-notify

Run a local coding-agent command and send a Telegram message when it finishes.

## What It Does

This project keeps the first version simple:

- you provide a Telegram bot token and chat ID
- you run your coding agent through the wrapper
- for Codex, the wrapper can notify when the app returns to `Ready.`
- it sends a Telegram message with the command, host, and elapsed time

## Setup

1. Create a Telegram bot with BotFather.
2. Get your bot token.
3. Start a chat with the bot.
4. Find your chat ID.
5. Copy `.env.example` to `.env` or export these environment variables:

```bash
export TELEGRAM_BOT_TOKEN=123456789:replace_me
export TELEGRAM_CHAT_ID=123456789
```

6. Send yourself a test notification before debugging the agent integration:

```bash
PYTHONPATH=src python3 -m telegram_agent_notify --test-telegram
```

## Usage

Run any terminal command through the notifier:

```bash
PYTHONPATH=src python3 -m telegram_agent_notify --name codex -- codex
```

For a longer command:

```bash
PYTHONPATH=src python3 -m telegram_agent_notify --name agent -- your-agent-command --arg1 --arg2
```

If your agent is normally started through a shell alias or shell pipeline, use
the helper script:

```bash
./bin/agent-notify codex
./bin/agent-notify "codex --help"
```

When the helper sees a Codex command, it automatically enables `Ready.` watching.

## Codex Example

If you run Codex as follows:

```bash
codex --dangerously-bypass-approvals-and-sandbox
```

then your new shell command should be:

```bash
./bin/agent-notify "codex --dangerously-bypass-approvals-and-sandbox"
```

You can also run the Python entrypoint directly:

```bash
PYTHONPATH=src python3 -m telegram_agent_notify --watch-ready --name codex -- codex --dangerously-bypass-approvals-and-sandbox
```

## Claude Code Example

If you run Claude Code as follows:

```bash
claude --dangerously-skip-permissions
```

then your new shell command should be:

```bash
./bin/agent-notify "claude --dangerously-skip-permissions"
```

You can also run the Python entrypoint directly:

```bash
PYTHONPATH=src python3 -m telegram_agent_notify --name claude -- claude --dangerously-skip-permissions
```

## Message Example

```text
codex is ready for the next task
elapsed: 00:03:12
command: codex --dangerously-bypass-approvals-and-sandbox
host: my-laptop
```

## Notes

- Telegram config is required before the wrapper starts the agent.
- Codex notifications are triggered when the UI returns to `Ready.` after you submit a prompt.
- Other commands still notify on process exit.
- The command's stdout and stderr still stream in your terminal as normal.
