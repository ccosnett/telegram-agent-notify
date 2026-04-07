# telegram-agent-notify

Send yourself a Telegram message when a local Codex task finishes.

## What It Does

This project wraps Codex in your terminal and sends a Telegram message when
Codex appears to be done with the task you just submitted.

The notification includes:

- elapsed time for that task
- the Codex command that was run
- the host name
- the prompt you submitted
- the latest model output line

## Setup

From the project root:

```bash
uv sync
cp .env.example .env
```

Put your Telegram credentials in `.env`:

```bash
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

Send a test message:

```bash
uv run telegram-agent-notify --test-telegram
```

## Main Usage

If you normally run Codex like this:

```bash
codex --dangerously-bypass-approvals-and-sandbox
```

run it through the wrapper like this:

```bash
./bin/agent-notify "codex --dangerously-bypass-approvals-and-sandbox"
```

## Use From Any Directory

You can run the notifier from any project directory by calling the helper with
its absolute path:

```bash
/Users/johncosnett/PycharmProjects/telegram-agent-notify/bin/agent-notify "codex --dangerously-bypass-approvals-and-sandbox"
```

That starts Codex in your current working directory, not in the notifier repo.

If you want a shorter command, add this to `~/.zshrc`:

```bash
alias codex-notify='/Users/johncosnett/PycharmProjects/telegram-agent-notify/bin/agent-notify'
```

Then reload your shell:

```bash
source ~/.zshrc
```

And use:

```bash
codex-notify "codex --dangerously-bypass-approvals-and-sandbox"
```

## Direct CLI

You can also run the installed CLI directly:

```bash
uv run --project /Users/johncosnett/PycharmProjects/telegram-agent-notify telegram-agent-notify --watch-ready --name codex --shell -- "codex --dangerously-bypass-approvals-and-sandbox"
```

## Example Notification

```text
codex is ready for the next task
elapsed: 00:00:07
command: codex --dangerously-bypass-approvals-and-sandbox
host: my-laptop
prompt: please sleep for 5seconds and then give me a quote
latest output: “Do what you can, with what you have, where you are.” — Theodore Roosevelt
```

## Notes

- This project is focused on OpenAI Codex.
- Run `uv sync` once before first use.
- The helper script uses `uv run --project ...` under the hood.
- Telegram config must be present before the wrapper starts Codex.
