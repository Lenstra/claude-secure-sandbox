"""What the developer sees: the start screen and the session label."""

from __future__ import annotations

import os
import shutil
import sys

from .common import NAME, SANDBOX_FILE, path_within
from .context import Context


def protected_code_exists(ctx: Context) -> bool:
    """True when protected code exists on the computer: this repository, or
    another protected repository."""
    return not ctx.directory_mode or bool(ctx.protected_elsewhere)


def session_label(ctx: Context) -> str:
    if ctx.directory_mode:
        name = ctx.work if ctx.read_only_root else os.path.basename(ctx.work)
        return f"directory {name}" + (" (read only)" if ctx.read_only_root else "")
    branch = ctx.git_copy.value("symbolic-ref", "-q", "--short", "HEAD") or "detached HEAD"
    return f"protected repository {os.path.basename(ctx.dev_root)} · {branch}"


def display_path(path: str) -> str:
    home = os.environ["HOME"]
    return "~" + path[len(home):] if path_within(home, path) and path != home else path


def confirm_session(ctx: Context) -> bool:
    """In a terminal, says which mode starts and why, then waits for Enter.
    Returns False if the developer cancels."""
    if not (sys.stdin.isatty() and sys.stderr.isatty()):
        return True
    green, bold, reset = "\033[1;32m", "\033[1m", "\033[0m"
    others = [display_path(r) for r in ctx.protected_elsewhere]
    others_text = ", ".join(others[:3]) + (f" and {len(others) - 3} more" if len(others) > 3 else "")
    if not ctx.directory_mode:
        branch = ctx.git_copy.value("symbolic-ref", "-q", "--short", "HEAD") or "detached HEAD"
        hidden = ", ".join(ctx.protected[:5]) + (f" and {len(ctx.protected) - 5} more"
                                                 if len(ctx.protected) > 5 else "")
        intro = f"This repository is protected: its {SANDBOX_FILE} lists protected paths."
        lines = [
            f"Hidden from Claude:  {hidden}",
            f"Claude works on:     a copy of this repository, branch {branch}",
            "Your files:          untouched; get Claude's commits with: git fetch claude",
        ]
    elif ctx.read_only_root:
        intro = f"You started Claude in {display_path(ctx.work)}, which holds your home folder."
        lines = [
            "Claude can edit:     nothing there; it will tell you the changes to make",
        ]
    else:
        intro = "Nothing is protected here: Claude works directly in this folder."
        lines = [
            f"Claude can edit:     {display_path(ctx.work)}",
        ]
    lines.append("Claude can read:     the rest of your computer, except what follows")
    if others:
        lines.append(f"Protected, hidden:   {others_text}")
    if protected_code_exists(ctx):
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


