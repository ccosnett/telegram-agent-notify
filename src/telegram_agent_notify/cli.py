from __future__ import annotations

import argparse
import errno
import fcntl
import os
import pty
import re
import shlex
import shutil
import signal
import socket
import struct
import subprocess
import sys
import termios
import time
import tty
import urllib.error
import urllib.parse
import urllib.request
from select import select


ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
OSC_ESCAPE_RE = re.compile(r"\x1b\][^\x07]*(?:\x07|\x1b\\)")
PROMPT_LINE_RE = re.compile(r"(?:^|\n)[›>]\s([^\n\r]*)")


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
        "--watch-ready",
        action="store_true",
        help="For interactive agents, notify when output returns to Ready.",
    )
    parser.add_argument(
        "--test-telegram",
        action="store_true",
        help="Send a test message and exit.",
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
    returncode: int | None,
    elapsed_seconds: float,
    event: str,
) -> str:
    host = socket.gethostname()
    lines = [
        f"{name} {event}",
        f"elapsed: {format_duration(elapsed_seconds)}",
        f"command: {command_text}",
        f"host: {host}",
    ]

    if returncode is not None:
        status = "success" if returncode == 0 else "failure"
        lines.insert(1, f"status: {status}")
        lines.insert(2, f"exit code: {returncode}")

    return "\n".join(lines)


def get_telegram_config() -> tuple[str, str]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID") or os.environ.get("CHAT_ID")

    if not token or not chat_id:
        raise RuntimeError(
            "Telegram is not configured. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID, or BOT_TOKEN and CHAT_ID, in .env or your shell."
        )

    return token, chat_id


def send_telegram_message(message: str) -> None:
    token, chat_id = get_telegram_config()
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


def normalize_command(args: argparse.Namespace) -> tuple[str | list[str], str]:
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

    return command, command_text


def run_command(args: argparse.Namespace) -> int:
    command, command_text = normalize_command(args)
    get_telegram_config()

    start = time.monotonic()
    completed = subprocess.run(command, shell=args.shell, check=False)
    elapsed = time.monotonic() - start

    message = build_message(
        name=args.name,
        command_text=command_text,
        returncode=completed.returncode,
        elapsed_seconds=elapsed,
        event="finished",
    )
    send_telegram_message(message)
    return completed.returncode


def strip_ansi(text: str) -> str:
    return OSC_ESCAPE_RE.sub("", ANSI_ESCAPE_RE.sub("", text))


def infer_name_from_command(command_text: str, current_name: str) -> str:
    if current_name != "agent":
        return current_name

    for candidate in ("codex", "claude"):
        if command_text == candidate or command_text.startswith(f"{candidate} "):
            return candidate

    return current_name


def interactive_ready_mode(args: argparse.Namespace) -> int:
    command, command_text = normalize_command(args)
    name = infer_name_from_command(command_text, args.name)
    get_telegram_config()

    stdin_fd = sys.stdin.fileno()
    stdout_fd = sys.stdout.fileno()
    old_tty_settings = termios.tcgetattr(stdin_fd)
    start = time.monotonic()
    last_notification_monotonic = 0.0
    pending_user_task = False
    typed_chars_since_submit = 0
    output_buffer = ""
    post_submit_output = ""
    submitted_text = ""
    current_input = ""

    pid, master_fd = pty.fork()
    if pid == 0:
        if args.shell:
            shell = os.environ.get("SHELL") or shutil.which("sh") or "/bin/sh"
            os.execvp(shell, [shell, "-lc", command])

        os.execvp(command[0], command)

    def forward_signal(signum: int, _frame: object) -> None:
        try:
            os.kill(pid, signum)
        except ProcessLookupError:
            pass

    previous_sigwinch = signal.getsignal(signal.SIGWINCH)
    previous_sigint = signal.getsignal(signal.SIGINT)
    previous_sigterm = signal.getsignal(signal.SIGTERM)

    def sync_winsize() -> None:
        try:
            size = shutil.get_terminal_size()
            if hasattr(termios, "tcgetwinsize"):
                packed = termios.tcgetwinsize(stdout_fd)
                rows = packed[0] if packed[0] else size.lines
                cols = packed[1] if packed[1] else size.columns
            else:
                packed = fcntl.ioctl(stdout_fd, termios.TIOCGWINSZ, b"\0" * 8)
                rows, cols, _, _ = struct.unpack("HHHH", packed)
                rows = rows or size.lines
                cols = cols or size.columns
        except OSError:
            size = shutil.get_terminal_size()
            rows = size.lines
            cols = size.columns

        try:
            if hasattr(termios, "tcsetwinsize"):
                termios.tcsetwinsize(master_fd, (rows, cols))
            else:
                packed = struct.pack("HHHH", rows, cols, 0, 0)
                fcntl.ioctl(master_fd, termios.TIOCSWINSZ, packed)
        except OSError:
            pass

    def on_sigwinch(signum: int, frame: object) -> None:
        sync_winsize()
        if callable(previous_sigwinch):
            previous_sigwinch(signum, frame)

    try:
        sync_winsize()
        tty.setraw(stdin_fd)
        signal.signal(signal.SIGWINCH, on_sigwinch)
        signal.signal(signal.SIGINT, forward_signal)
        signal.signal(signal.SIGTERM, forward_signal)

        while True:
            try:
                ready, _, _ = select([stdin_fd, master_fd], [], [])
            except OSError as exc:
                if exc.errno == errno.EINTR:
                    continue
                raise

            if stdin_fd in ready:
                data = os.read(stdin_fd, 1024)
                if not data:
                    os.close(master_fd)
                    break

                os.write(master_fd, data)

                for byte in data:
                    if byte in (10, 13):
                        if typed_chars_since_submit > 0:
                            pending_user_task = True
                            post_submit_output = ""
                            submitted_text = current_input.strip()
                        typed_chars_since_submit = 0
                        current_input = ""
                    elif byte in (8, 127):
                        typed_chars_since_submit = max(0, typed_chars_since_submit - 1)
                        current_input = current_input[:-1]
                    elif 32 <= byte <= 126:
                        typed_chars_since_submit += 1
                        current_input += chr(byte)

            if master_fd in ready:
                try:
                    data = os.read(master_fd, 4096)
                except OSError:
                    break

                if not data:
                    break

                os.write(stdout_fd, data)

                cleaned = strip_ansi(data.decode("utf-8", errors="ignore"))
                output_buffer = (output_buffer + cleaned)[-4000:]
                if pending_user_task:
                    post_submit_output = (post_submit_output + cleaned)[-4000:]

                prompt_returned = False
                if pending_user_task:
                    prompt_lines = PROMPT_LINE_RE.findall(post_submit_output)
                    for prompt_text in prompt_lines:
                        normalized_prompt = prompt_text.strip()
                        if normalized_prompt and normalized_prompt == submitted_text:
                            continue
                        prompt_returned = True
                        break

                task_complete_markers = (
                    "Ready." in post_submit_output
                    or "Token usage:" in post_submit_output
                    or "To continue this session, run codex resume" in post_submit_output
                    or prompt_returned
                )

                if pending_user_task and task_complete_markers:
                    now = time.monotonic()
                    if now - last_notification_monotonic > 2:
                        elapsed = now - start
                        message = build_message(
                            name=name,
                            command_text=command_text,
                            returncode=None,
                            elapsed_seconds=elapsed,
                            event="is ready for the next task",
                        )
                        try:
                            send_telegram_message(message)
                        except (RuntimeError, urllib.error.URLError) as exc:
                            print(
                                f"\nwarning: failed to send Telegram notification: {exc}",
                                file=sys.stderr,
                            )

                        last_notification_monotonic = now
                        pending_user_task = False
                        post_submit_output = ""
                        submitted_text = ""

            try:
                child_pid, status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                break

            if child_pid == pid:
                if os.WIFEXITED(status):
                    returncode = os.WEXITSTATUS(status)
                elif os.WIFSIGNALED(status):
                    returncode = 128 + os.WTERMSIG(status)
                else:
                    returncode = 1

                elapsed = time.monotonic() - start
                message = build_message(
                    name=name,
                    command_text=command_text,
                    returncode=returncode,
                    elapsed_seconds=elapsed,
                    event="finished",
                )
                try:
                    send_telegram_message(message)
                except (RuntimeError, urllib.error.URLError) as exc:
                    print(
                        f"\nwarning: failed to send Telegram notification: {exc}",
                        file=sys.stderr,
                    )
                return returncode

        _, status = os.waitpid(pid, 0)
        if os.WIFEXITED(status):
            return os.WEXITSTATUS(status)
        if os.WIFSIGNALED(status):
            return 128 + os.WTERMSIG(status)
        return 1
    finally:
        termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_tty_settings)
        signal.signal(signal.SIGWINCH, previous_sigwinch)
        signal.signal(signal.SIGINT, previous_sigint)
        signal.signal(signal.SIGTERM, previous_sigterm)


def send_test_message(name: str) -> None:
    get_telegram_config()
    message = build_message(
        name=name,
        command_text="telegram test",
        returncode=None,
        elapsed_seconds=0,
        event="test notification",
    )
    send_telegram_message(message)


def main() -> None:
    load_dotenv()
    args = parse_args()

    try:
        if args.test_telegram:
            send_test_message(args.name)
            raise SystemExit(0)

        if args.watch_ready:
            raise SystemExit(interactive_ready_mode(args))

        raise SystemExit(run_command(args))
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
