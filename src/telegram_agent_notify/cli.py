from __future__ import annotations

import argparse
import os
import shlex
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


def load_dotenv(path: str = ".env") -> None:
    if not os.path.exists(path):
        return

    with open(path, "r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")

            if key and key not in os.environ:
                os.environ[key] = value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a command and send a Telegram message when it exits."
    )
    parser.add_argument(
        "--name",
        default="agent",
        help="Display name used in the Telegram message.",
    )
    parser.add_argument(
        "--shell",
        action="store_true",
        help="Run the command through the shell.",
    )
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="Command to run. Separate it from options using --.",
    )
    return parser.parse_args()


def format_duration(seconds: float) -> str:
    total = int(round(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def build_message(
    *,
    name: str,
    command_text: str,
    returncode: int,
    elapsed_seconds: float,
) -> str:
    status = "success" if returncode == 0 else "failure"
    host = socket.gethostname()
    lines = [
        f"{name} finished",
        f"status: {status}",
        f"exit code: {returncode}",
        f"elapsed: {format_duration(elapsed_seconds)}",
        f"command: {command_text}",
        f"host: {host}",
    ]
    return "\n".join(lines)


def send_telegram_message(message: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set in the environment."
        )

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = urllib.parse.urlencode(
        {
            "chat_id": chat_id,
            "text": message,
        }
    ).encode("utf-8")
    request = urllib.request.Request(url, data=payload, method="POST")

    with urllib.request.urlopen(request, timeout=15) as response:
        if response.status != 200:
            raise RuntimeError(f"Telegram API returned HTTP {response.status}.")


def run_command(args: argparse.Namespace) -> int:
    if not args.command:
        raise RuntimeError("No command provided. Use -- before the command.")

    raw_command = args.command
    if raw_command and raw_command[0] == "--":
        raw_command = raw_command[1:]

    if not raw_command:
        raise RuntimeError("No command provided after --.")

    if args.shell:
        command: str | list[str] = " ".join(raw_command)
        command_text = command
    else:
        command = raw_command
        command_text = shlex.join(raw_command)

    start = time.monotonic()
    completed = subprocess.run(command, shell=args.shell, check=False)
    elapsed = time.monotonic() - start

    message = build_message(
        name=args.name,
        command_text=command_text,
        returncode=completed.returncode,
        elapsed_seconds=elapsed,
    )

    try:
        send_telegram_message(message)
    except (RuntimeError, urllib.error.URLError) as exc:
        print(f"warning: failed to send Telegram notification: {exc}", file=sys.stderr)

    return completed.returncode


def main() -> None:
    load_dotenv()
    args = parse_args()
    try:
        raise SystemExit(run_command(args))
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
