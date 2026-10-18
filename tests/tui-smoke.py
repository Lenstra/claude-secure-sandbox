#!/usr/bin/env python3
"""Smoke test of the real Claude Code in the sandbox, in a terminal.

Starts the wrapper in a pseudo-terminal with an empty HOME, so that Claude
Code shows its first-run screens, and checks that its interface reads keys:
Enter on the start screen, then Enter on the theme screen, which must lead to
the login screen. No login is needed. Requires Claude Code on the PATH.
"""

import os
import pty
import re
import select
import shutil
import signal
import struct
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ESCAPE = re.compile(rb"\x1b(\[[0-9;?<>=]*[ -/]*[@-~]|\][^\x07\x1b]*(\x07|\x1b\\)|[@-Z\\-_])")


def main() -> int:
    if not shutil.which("claude"):
        print("tui-smoke: Claude Code not found on the PATH", file=sys.stderr)
        return 1
    base = tempfile.mkdtemp(prefix="cs-tui.", dir=os.environ.get("TMPDIR"))
    base = os.path.realpath(base)
    try:
        return run(base)
    finally:
        shutil.rmtree(base, ignore_errors=True)


def run(base: str) -> int:
    home, work = os.path.join(base, "home"), os.path.join(base, "work")
    os.makedirs(home)
    os.makedirs(work)
    env = dict(os.environ, HOME=home, TERM="xterm-256color", CLAUDE_SECURE_SANDBOX_SCAN="limited",
               CLAUDE_SECURE_SANDBOX_HOME=os.path.join(home, ".cache", "claude-secure-sandbox"))
    for var in ("CLAUDE_CODE_CHILD_SESSION", "CLAUDECODE", "CLAUDE_SECURE_SANDBOX_ACTIVE"):
        env.pop(var, None)

    pid, fd = pty.fork()
    if pid == 0:
        os.chdir(work)
        os.execve(os.path.join(ROOT, "bin", "claude"), ["claude"], env)
    import fcntl
    import termios
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))

    seen = bytearray()

    def wait_for(text: str, timeout: float) -> bool:
        """Reads the terminal until text appears (escape sequences and spaces
        removed) in what was printed since the last call."""
        seen.clear()
        needle = text.replace(" ", "").encode()
        end = time.time() + timeout
        while time.time() < end:
            ready, _, _ = select.select([fd], [], [], 0.3)
            if ready:
                try:
                    data = os.read(fd, 65536)
                except OSError:
                    return False
                if not data:
                    return False
                seen.extend(data)
                if needle in ESCAPE.sub(b"", bytes(seen)).replace(b" ", b""):
                    return True
        return False

    steps = [("Press Enter to start Claude", 120), ("text style", 120), ("login method", 60)]
    ok = True
    for i, (text, timeout) in enumerate(steps):
        if not wait_for(text, timeout):
            print(f"tui-smoke: FAIL, \"{text}\" not shown", file=sys.stderr)
            tail = ESCAPE.sub(b" ", bytes(seen)).decode("utf-8", "replace")
            print(re.sub(r"\s+", " ", tail)[-1500:], file=sys.stderr)
            ok = False
            break
        print(f"tui-smoke: ok, \"{text}\" shown")
        if i < len(steps) - 1:
            time.sleep(1.5)
            os.write(fd, b"\r")
    try:
        os.kill(pid, signal.SIGTERM)
        time.sleep(1)
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    os.waitpid(pid, 0)
    if ok:
        print("tui-smoke: Claude Code reads keys in the sandbox")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
