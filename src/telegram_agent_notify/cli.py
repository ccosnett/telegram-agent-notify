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
from dataclasses import dataclass
from select import select


ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
OSC_ESCAPE_RE = re.compile(r"\x1b\][^\x07]*(?:\x07|\x1b\\)")
READY_MARKERS = (
    "Ready.",
    "Token usage:",
    "To continue this session, run codex resume",
)
PROMPT_PREFIXES = ("› ", "> ")
STATUS_PREFIXES = ("Ran ", "Received.", "(no output)")
IDLE_NOTIFY_SECONDS = 1.5


@dataclass
class CommandSpec:
    command: str | list[str]
    command_text: str
    name: str


@dataclass
class ReadyTracker:
    current_input: str = ""
    post_submit_output: str = ""
    pending: bool = False
    last_notification_monotonic: float = 0.0
    last_output_monotonic: float = 0.0
    completion_candidate_monotonic: float | None = None
    task_started_monotonic: float | None = None
    submitted_prompt: str | None = None
    last_meaningful_line: str | None = None
    input_escape_state: str | None = None
    osc_escape_pending: bool = False

    def record_input(self, data: bytes, now: float) -> None:
        for byte in data:
            if self.input_escape_state == "esc":
                if byte == ord("["):
                    self.input_escape_state = "csi"
                elif byte == ord("]"):
                    self.input_escape_state = "osc"
                    self.osc_escape_pending = False
                else:
                    self.input_escape_state = None
                continue

            if self.input_escape_state == "csi":
                if 64 <= byte <= 126:
                    self.input_escape_state = None
                continue

            if self.input_escape_state == "osc":
                if byte == 7:
                    self.input_escape_state = None
                    self.osc_escape_pending = False
                    continue
                if self.osc_escape_pending and byte == 92:
                    self.input_escape_state = None
                    self.osc_escape_pending = False
                    continue
                self.osc_escape_pending = byte == 27
                continue

            if byte == 27:
                self.input_escape_state = "esc"
                continue

            if byte in (10, 13):
                if self.current_input.strip():
                    self.pending = True
                    self.post_submit_output = ""
                    self.completion_candidate_monotonic = None
                    self.task_started_monotonic = now
                    self.submitted_prompt = self.current_input.strip()
                    self.last_meaningful_line = None
                self.current_input = ""
            elif byte in (8, 127):
                self.current_input = self.current_input[:-1]
            elif 32 <= byte <= 126:
                self.current_input += chr(byte)

    def record_output(self, text: str, now: float) -> bool:
        if not self.pending:
            return False

        self.last_output_monotonic = now
        self.post_submit_output = (self.post_submit_output + text)[-4000:]
        self._update_completion_candidate(text, now)
        return self._saw_ready_marker()

    def _saw_ready_marker(self) -> bool:
        return any(marker in self.post_submit_output for marker in READY_MARKERS)

    def _update_completion_candidate(self, text: str, now: float) -> None:
        for raw_line in text.splitlines():
            line = normalize_line(raw_line)
            if not line:
                continue
            if is_prompt_line(line):
                continue
            if is_status_line(line):
                self.completion_candidate_monotonic = None
                continue
            self.completion_candidate_monotonic = now
            self.last_meaningful_line = line

    def should_notify(self, now: float) -> bool:
        return now - self.last_notification_monotonic > 2.0

    def idle_completion_reached(self, now: float) -> bool:
        if self.completion_candidate_monotonic is None:
            return False
        return now - self.last_output_monotonic >= IDLE_NOTIFY_SECONDS

    def mark_notified(self, now: float) -> None:
        self.pending = False
        self.post_submit_output = ""
        self.completion_candidate_monotonic = None
        self.last_notification_monotonic = now

    def task_elapsed(self, now: float, default_start: float) -> float:
        return now - (self.task_started_monotonic or default_start)


def _load_dotenv_file(path: str) -> None:
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


def load_dotenv(path: str = ".env") -> None:
    home = os.environ.get("TELEGRAM_AGENT_NOTIFY_HOME")
    paths = [path]
    if home:
        paths.append(os.path.join(home, ".env"))

    seen: set[str] = set()
    for candidate in paths:
        resolved = os.path.abspath(candidate)
        if resolved in seen:
            continue
        seen.add(resolved)
        _load_dotenv_file(candidate)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a command and send a Telegram message when it exits."
    )
    parser.add_argument("--name", default="agent", help="Display name for Telegram.")
    parser.add_argument("--shell", action="store_true", help="Run through the shell.")
    parser.add_argument(
        "--watch-ready",
        action="store_true",
        help="For interactive agents, notify when the UI returns to input-ready.",
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
    elapsed_seconds: float,
    event: str,
    returncode: int | None = None,
    submitted_prompt: str | None = None,
    latest_output: str | None = None,
) -> str:
    lines = [
        f"{name} {event}",
        f"elapsed: {format_duration(elapsed_seconds)}",
        f"command: {command_text}",
        f"host: {socket.gethostname()}",
    ]

    if returncode is not None:
        status = "success" if returncode == 0 else "failure"
        lines.insert(1, f"status: {status}")
        lines.insert(2, f"exit code: {returncode}")

    if submitted_prompt:
        lines.append(f"prompt: {submitted_prompt}")

    if latest_output:
        lines.append(f"latest output: {latest_output}")

    return "\n".join(lines)


def get_telegram_config() -> tuple[str, str]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID") or os.environ.get("CHAT_ID")
    if token and chat_id:
        return token, chat_id

    raise RuntimeError(
        "Telegram is not configured. Set TELEGRAM_BOT_TOKEN and "
        "TELEGRAM_CHAT_ID, or BOT_TOKEN and CHAT_ID, in .env or your shell."
    )


def send_telegram_message(message: str) -> None:
    token, chat_id = get_telegram_config()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = urllib.parse.urlencode({"chat_id": chat_id, "text": message}).encode(
        "utf-8"
    )
    request = urllib.request.Request(url, data=payload, method="POST")
    with urllib.request.urlopen(request, timeout=15) as response:
        if response.status != 200:
            raise RuntimeError(f"Telegram API returned HTTP {response.status}.")


def notify_or_warn(message: str) -> None:
    try:
        send_telegram_message(message)
    except (RuntimeError, urllib.error.URLError) as exc:
        print(f"\nwarning: failed to send Telegram notification: {exc}", file=sys.stderr)


def parse_command(args: argparse.Namespace) -> CommandSpec:
    raw_command = list(args.command)
    if raw_command and raw_command[0] == "--":
        raw_command = raw_command[1:]
    if not raw_command:
        raise RuntimeError("No command provided. Use -- before the command.")

    if args.shell:
        command: str | list[str] = " ".join(raw_command)
        command_text = command
    else:
        command = raw_command
        command_text = shlex.join(raw_command)

    return CommandSpec(
        command=command,
        command_text=command_text,
        name=infer_name(command_text, args.name),
    )


def infer_name(command_text: str, current_name: str) -> str:
    if current_name != "agent":
        return current_name

    for candidate in ("codex", "claude"):
        if command_text == candidate or command_text.startswith(f"{candidate} "):
            return candidate
    return current_name


def run_command(spec: CommandSpec) -> int:
    get_telegram_config()
    start = time.monotonic()
    completed = subprocess.run(spec.command, shell=isinstance(spec.command, str), check=False)
    message = build_message(
        name=spec.name,
        command_text=spec.command_text,
        elapsed_seconds=time.monotonic() - start,
        event="finished",
        returncode=completed.returncode,
    )
    send_telegram_message(message)
    return completed.returncode


def strip_ansi(text: str) -> str:
    return OSC_ESCAPE_RE.sub("", ANSI_ESCAPE_RE.sub("", text))


def normalize_line(raw_line: str) -> str:
    return raw_line.strip().lstrip("•·*└│ ").strip()


def is_prompt_line(line: str) -> bool:
    return line.startswith(PROMPT_PREFIXES)


def is_status_line(line: str) -> bool:
    return line.startswith(STATUS_PREFIXES)


def get_terminal_size(fd: int) -> tuple[int, int]:
    fallback = shutil.get_terminal_size()
    try:
        if hasattr(termios, "tcgetwinsize"):
            rows, cols = termios.tcgetwinsize(fd)
            return rows or fallback.lines, cols or fallback.columns

        packed = fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8)
        rows, cols, _, _ = struct.unpack("HHHH", packed)
        return rows or fallback.lines, cols or fallback.columns
    except OSError:
        return fallback.lines, fallback.columns


def set_terminal_size(fd: int, rows: int, cols: int) -> None:
    try:
        if hasattr(termios, "tcsetwinsize"):
            termios.tcsetwinsize(fd, (rows, cols))
            return

        packed = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, packed)
    except OSError:
        pass


def exec_child(spec: CommandSpec) -> None:
    if isinstance(spec.command, str):
        shell = os.environ.get("SHELL") or shutil.which("sh") or "/bin/sh"
        os.execvp(shell, [shell, "-lc", spec.command])

    os.execvp(spec.command[0], spec.command)


def child_returncode(status: int) -> int:
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    if os.WIFSIGNALED(status):
        return 128 + os.WTERMSIG(status)
    return 1


def install_signal_handlers(pid: int, master_fd: int, stdout_fd: int) -> tuple[object, object, object]:
    def forward_signal(signum: int, _frame: object) -> None:
        try:
            os.kill(pid, signum)
        except ProcessLookupError:
            pass

    previous_sigwinch = signal.getsignal(signal.SIGWINCH)
    previous_sigint = signal.getsignal(signal.SIGINT)
    previous_sigterm = signal.getsignal(signal.SIGTERM)

    def on_sigwinch(signum: int, frame: object) -> None:
        rows, cols = get_terminal_size(stdout_fd)
        set_terminal_size(master_fd, rows, cols)
        if callable(previous_sigwinch):
            previous_sigwinch(signum, frame)

    signal.signal(signal.SIGWINCH, on_sigwinch)
    signal.signal(signal.SIGINT, forward_signal)
    signal.signal(signal.SIGTERM, forward_signal)
    return previous_sigwinch, previous_sigint, previous_sigterm


def restore_signal_handlers(previous_handlers: tuple[object, object, object]) -> None:
    previous_sigwinch, previous_sigint, previous_sigterm = previous_handlers
    signal.signal(signal.SIGWINCH, previous_sigwinch)
    signal.signal(signal.SIGINT, previous_sigint)
    signal.signal(signal.SIGTERM, previous_sigterm)


def handle_child_output(
    master_fd: int,
    stdout_fd: int,
    tracker: ReadyTracker,
    spec: CommandSpec,
    start: float,
) -> None:
    try:
        data = os.read(master_fd, 4096)
    except OSError as exc:
        if exc.errno == errno.EIO:
            raise EOFError from exc
        raise

    if not data:
        raise EOFError

    os.write(stdout_fd, data)
    now = time.monotonic()
    if not tracker.record_output(strip_ansi(data.decode("utf-8", errors="ignore")), now):
        return

    if not tracker.should_notify(now):
        return

    notify_or_warn(
        build_message(
            name=spec.name,
            command_text=spec.command_text,
            elapsed_seconds=tracker.task_elapsed(now, start),
            event="is ready for the next task",
            submitted_prompt=tracker.submitted_prompt,
            latest_output=tracker.last_meaningful_line,
        )
    )
    tracker.mark_notified(now)


def interactive_ready_mode(spec: CommandSpec) -> int:
    get_telegram_config()
    stdin_fd = sys.stdin.fileno()
    stdout_fd = sys.stdout.fileno()
    tracker = ReadyTracker()
    start = time.monotonic()
    old_tty_settings = termios.tcgetattr(stdin_fd)

    pid, master_fd = pty.fork()
    if pid == 0:
        exec_child(spec)

    previous_handlers = install_signal_handlers(pid, master_fd, stdout_fd)
    rows, cols = get_terminal_size(stdout_fd)
    set_terminal_size(master_fd, rows, cols)

    try:
        tty.setraw(stdin_fd)

        while True:
            try:
                ready, _, _ = select([stdin_fd, master_fd], [], [], 0.25)
            except OSError as exc:
                if exc.errno == errno.EINTR:
                    continue
                raise

            now = time.monotonic()
            if (
                tracker.pending
                and tracker.idle_completion_reached(now)
                and tracker.should_notify(now)
            ):
                notify_or_warn(
                    build_message(
                        name=spec.name,
                        command_text=spec.command_text,
                        elapsed_seconds=tracker.task_elapsed(now, start),
                        event="is ready for the next task",
                        submitted_prompt=tracker.submitted_prompt,
                        latest_output=tracker.last_meaningful_line,
                    )
                )
                tracker.mark_notified(now)

            if stdin_fd in ready:
                data = os.read(stdin_fd, 1024)
                if not data:
                    os.close(master_fd)
                    break
                os.write(master_fd, data)
                tracker.record_input(data, now)

            if master_fd in ready:
                try:
                    handle_child_output(master_fd, stdout_fd, tracker, spec, start)
                except EOFError:
                    break

            try:
                child_pid, status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                break

            if child_pid == pid:
                returncode = child_returncode(status)
                notify_or_warn(
                    build_message(
                        name=spec.name,
                        command_text=spec.command_text,
                        elapsed_seconds=time.monotonic() - start,
                        event="finished",
                        returncode=returncode,
                    )
                )
                return returncode

        _, status = os.waitpid(pid, 0)
        returncode = child_returncode(status)
        notify_or_warn(
            build_message(
                name=spec.name,
                command_text=spec.command_text,
                elapsed_seconds=time.monotonic() - start,
                event="finished",
                returncode=returncode,
            )
        )
        return returncode
    finally:
        termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_tty_settings)
        restore_signal_handlers(previous_handlers)


def send_test_message(name: str) -> None:
    get_telegram_config()
    send_telegram_message(
        build_message(
            name=name,
            command_text="telegram test",
            elapsed_seconds=0,
            event="test notification",
        )
    )


def main() -> None:
    load_dotenv()
    args = parse_args()

    try:
        if args.test_telegram:
            send_test_message(args.name)
            raise SystemExit(0)

        spec = parse_command(args)
        if args.watch_ready:
            raise SystemExit(interactive_ready_mode(spec))
        raise SystemExit(run_command(spec))
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
