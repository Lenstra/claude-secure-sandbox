"""What the developer sees: the progress of a long step, the start screen
and the session label."""

from __future__ import annotations

import os
import shutil
import sys
import threading
import time
from typing import Optional

from .common import NAME, SANDBOX_FILE, path_within, read_lines, write_text
from .context import Context


def estimate(elapsed: float, previous: float) -> int:
    """Percentage done, from the previous duration of the same step: never
    100 before the end, since this run may be slower."""
    return min(99, int(100 * elapsed / previous))


class Progress:
    """While a long step runs, one animated line on the terminal: a spinner,
    a percentage estimated from the step's previous duration (kept in
    duration_file), and the elapsed time. The line is erased at the end.
    Nothing is shown without a terminal."""

    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, text: str, duration_file: str):
        self.text = text
        self.duration_file = duration_file
        self.done = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.start = 0.0
        self.previous = 0.0

    def __enter__(self) -> "Progress":
        self.start = time.monotonic()
        try:
            self.previous = float("".join(read_lines(self.duration_file)) or 0)
        except ValueError:
            self.previous = 0.0
        if sys.stderr.isatty():
            self.thread = threading.Thread(target=self._draw, daemon=True)
            self.thread.start()
        return self

    def __exit__(self, kind, *_) -> None:
        elapsed = time.monotonic() - self.start
        if self.thread:
            self.done.set()
            self.thread.join()
            print("\r\033[K", end="", file=sys.stderr, flush=True)
        # Only a complete run says how long the next one will take.
        if kind is None:
            try:
                write_text(self.duration_file, f"{elapsed:.1f}\n")
            except OSError:
                pass

    def _draw(self) -> None:
        frame = 0
        while True:
            elapsed = time.monotonic() - self.start
            percent = f" {estimate(elapsed, self.previous)}%" if self.previous > 0 else ""
            line = f"{NAME}: {self.FRAMES[frame % len(self.FRAMES)]} {self.text}{percent} ({elapsed:.0f} s)"
            print(f"\r{line}\033[K", end="", file=sys.stderr, flush=True)
            frame += 1
            if self.done.wait(0.1):
                return


def session_label(ctx: Context) -> str:
    if not ctx.protected:
        name = ctx.work if ctx.home_session else os.path.basename(ctx.work)
        return f"directory {name}" + (" (read only)" if ctx.home_session else "")
    branch = ctx.git_copy.value("symbolic-ref", "-q", "--short", "HEAD") or "detached HEAD"
    return f"protected repository {os.path.basename(ctx.dev_root)} · {branch}"


def display_path(path: str) -> str:
    home = os.environ["HOME"]
    return "~" + path[len(home):] if path_within(home, path) and path != home else path


def confirm_session(ctx: Context) -> bool:
    """In a terminal, says what Claude can see and edit, then waits for Enter.
    Returns False if the developer cancels."""
    if not (sys.stdin.isatty() and sys.stderr.isatty()):
        return True
    green, bold, reset = "\033[1;32m", "\033[1m", "\033[0m"
    others = [display_path(r) for r in ctx.protected_elsewhere]
    others_text = ", ".join(others[:3]) + (f" and {len(others) - 3} more" if len(others) > 3 else "")
    if ctx.protected:
        branch = ctx.git_copy.value("symbolic-ref", "-q", "--short", "HEAD") or "detached HEAD"
        hidden = ", ".join(ctx.hidden[:5]) + (f" and {len(ctx.hidden) - 5} more"
                                              if len(ctx.hidden) > 5 else "")
        intro = f"This repository is protected: Claude only sees what its {SANDBOX_FILE} lists."
        lines = [
            f"Hidden from Claude:  {hidden}",
            f"Claude works on:     a copy of this repository, branch {branch}",
            "Your files:          untouched; get Claude's commits with: git fetch claude",
        ]
    elif ctx.home_session:
        intro = f"You started Claude in {display_path(ctx.work)}, which holds your home folder."
        lines = [
            "Claude can edit:     nothing there; it will tell you the changes to make",
        ]
    else:
        intro = "Nothing is protected here: Claude works directly in this folder."
        lines = [
            f"Claude can edit:     {display_path(ctx.work)}",
        ]
    if not ctx.home_session:
        lines.append("Claude can read:     the system and the tools on your PATH, not your other files")
        if ctx.granted_paths:
            granted = [display_path(p) for p in ctx.granted_paths]
            lines.append(f"Also readable:       {', '.join(granted)} ({SANDBOX_FILE})")
    else:
        lines.append("Claude can read:     the rest of your computer, except what follows")
        if others:
            lines.append(f"Protected, hidden:   {others_text}")
        if ctx.protected_code_exists:
            lines.append("Also hidden:         your credentials (SSH keys, keychain, tokens), temp files,")
            lines.append("                     and your old Claude conversations")
        else:
            lines.append("Hidden:              your old Claude conversations; nothing on this computer is protected")
    title = NAME
    prompt = "Press Enter to start Claude, or Ctrl-C to cancel: "
    texts = ["", f"🔒 {title}", "", intro, ""] + [f"   {line}" for line in lines] + ["", prompt]
    print(f"\n{green}🔒 {title}{reset}\n\n{bold}{intro}{reset}\n", file=sys.stderr)
    for line in lines:
        print(f"   {line}", file=sys.stderr)
    print(f"\n{prompt}", end="", file=sys.stderr, flush=True)
    try:
        started = bool(sys.stdin.readline())
    except KeyboardInterrupt:
        print(file=sys.stderr)
        return False
    if started:
        # Erase the screen before Claude takes the terminal: up as many rows
        # as were printed (long lines wrap), then clear to the end.
        columns = max(shutil.get_terminal_size().columns, 1)
        widths = [len(t) + t.count("🔒") for t in texts]  # the emoji takes two cells
        rows = sum(max(1, -(-w // columns)) for w in widths)
        print(f"\033[{rows}A\r\033[J", end="", file=sys.stderr, flush=True)
    return started


