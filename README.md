# telegram-agent-notify

Run a local coding-agent command and send a Telegram message when it finishes.

## What It Does

This project keeps the first version simple:

- you provide a Telegram bot token and chat ID
- you run your coding agent through the wrapper
- the wrapper waits for the command to finish
- it sends a Telegram message with success or failure, exit code, and elapsed time

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

## Message Example

```text
codex finished
status: success
exit code: 0
elapsed: 00:03:12
command: codex
host: my-laptop
```

## Notes

- This version wraps command execution directly.
- It does not yet detect task completion from arbitrary already-running agents.
- The command's stdout and stderr still stream in your terminal as normal.
