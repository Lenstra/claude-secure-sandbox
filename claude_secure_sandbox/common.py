"""Shared helpers: logging, paths, configuration, binaries, git, lock."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import time
from typing import Iterable, List, NoReturn, Optional

NAME = "claude-secure-sandbox"
# File at the repository root: protected paths, and what the sandbox may
# reach beyond the defaults (paths to read, network domains).
SANDBOX_FILE = ".claudesandbox"
# Protection file of earlier versions: still marks a protected repository.
LEGACY_PROTECT_FILE = ".claudeprotected"
PLACEHOLDER_TEXT = f"[{NAME}] hidden by {SANDBOX_FILE}"
# Empty blob: its content is known to everyone, so it is never hidden.
EMPTY_BLOB = "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"
NULL_OID = "0" * 40


def log(msg: str) -> None:
    print(f"{NAME}: {msg}", file=sys.stderr, flush=True)


def warn(msg: str) -> None:
    print(f"{NAME}: warning: {msg}", file=sys.stderr, flush=True)


def die(msg: str) -> NoReturn:
    print(f"{NAME}: error: {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


def ask(question: str) -> Optional[str]:
    """Asks the developer on the terminal, before Claude starts: Claude cannot
    answer. Returns None when there is no terminal (claude -p in a script)."""
    if not sys.stdin.isatty():
        return None
    print(f"{NAME}: {question}", end="", file=sys.stderr, flush=True)
    line = sys.stdin.readline()
    if not line:
        raise KeyboardInterrupt
    return line.strip()


# --- paths ---------------------------------------------------------------------


def realpath(path: str) -> str:
    return os.path.realpath(path)


def path_within(parent: str, path: str) -> bool:
    """True if path is parent or lies under parent."""
    return path == parent or path.startswith(parent + "/")


def overlaps(a: str, b: str) -> bool:
    """True if one path lies within the other."""
    return path_within(a, b) or path_within(b, a)


def expand_home(path: str) -> str:
    home = os.environ["HOME"]
    if path == "~":
        return home
    if path.startswith("~/"):
        return home + "/" + path[2:]
    return path


def stamp() -> str:
    return time.strftime("%Y-%m-%d-%H%M")


def sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "surrogateescape")).hexdigest()


def regular_file(path: str) -> bool:
    """True for a regular file that is neither a symlink nor a hard link."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode) and st.st_nlink == 1


def remove_entry(path: str) -> None:
    """Removes path itself: a symlink is removed, never followed."""
    if os.path.islink(path) or (os.path.lexists(path) and not os.path.isdir(path)):
        os.unlink(path)
    elif os.path.isdir(path):
        shutil.rmtree(path)


def install_file(src: str, dst: str) -> None:
    """Installs src as dst by replacing the entry itself: a symlink planted at
    dst is replaced, never followed."""
    remove_entry(dst)
    os.rename(src, dst)


def ensure_real_dir(path: str) -> None:
    """Directory shared with the sandbox: never a symlink (srt would follow it
    and open its target in the next session)."""
    if os.path.islink(path) or (os.path.lexists(path) and not os.path.isdir(path)):
        warn(f"{path} was replaced by a link or a file: recreated")
        try:
            os.unlink(path)
        except OSError:
            die(f"cannot remove {path}")
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        die(f"cannot create {path}")
    if realpath(path) != path:
        die(f"{path} is not a real directory")


def write_text(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8", errors="surrogateescape") as f:
        f.write(text)


def read_text(path: str) -> str:
    with open(path, encoding="utf-8", errors="surrogateescape") as f:
        return f.read()


def read_lines(path: str) -> List[str]:
    try:
        return [line for line in read_text(path).splitlines() if line]
    except FileNotFoundError:
        return []


def load_json(path: str):
    """The JSON content of path, or None if it is missing or invalid."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def install_json(data, dst: str, tmp_dir: str) -> None:
    """Writes data to dst through a file in tmp_dir: a link planted at dst is
    replaced, never followed."""
    tmp = os.path.join(tmp_dir, os.path.basename(dst))
    write_text(tmp, json.dumps(data, indent=2) + "\n")
    install_file(tmp, dst)


def write_lines(path: str, lines: Iterable[str]) -> None:
    lines = list(lines)
    write_text(path, "".join(line + "\n" for line in lines))


# --- state ---------------------------------------------------------------------


def state_root() -> str:
    """The wrapper's state: ~/.cache/claude-secure-sandbox, or
    CLAUDE_SECURE_SANDBOX_HOME (tests). There is no configuration file: what
    the sandbox may reach is decided by the tool and by each repository's
    .claudesandbox."""
    root = os.environ.get("CLAUDE_SECURE_SANDBOX_HOME") or ""
    root = expand_home(root) if root else os.path.join(os.environ["HOME"], ".cache", NAME)
    try:
        os.makedirs(root, exist_ok=True)
    except OSError:
        die(f"cannot create {root}")
    return realpath(root)


# --- binaries ------------------------------------------------------------------


def is_executable_file(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


def find_real_claude(root: str) -> Optional[str]:
    """The first "claude" on the PATH that is not this wrapper."""
    me = realpath(os.path.join(root, "bin", "claude"))
    for d in os.environ.get("PATH", "").split(":"):
        candidate = os.path.join(d or ".", "claude")
        if is_executable_file(candidate) and realpath(candidate) != me:
            return candidate
    return None


def find_srt(root: str) -> Optional[str]:
    """The srt installed with the tool (npm ci), never another one."""
    local = os.path.join(root, "node_modules", ".bin", "srt")
    return local if os.access(local, os.X_OK) else None


# Mount points of other disks: never readable, and never a working directory.
VOLUME_ROOTS = {"Darwin": ["/Volumes"], "Linux": ["/mnt", "/media", "/run/media"]}


def volume_roots() -> List[str]:
    return VOLUME_ROOTS.get(platform.system(), [])


def check_deps() -> None:
    system = platform.system()
    if system not in ("Darwin", "Linux"):
        die("only macOS and Linux are supported")
    tools = {"git": "git", "rg": "ripgrep"}
    if system == "Linux":
        tools.update({"bwrap": "bubblewrap", "socat": "socat"})
    missing = [package for tool, package in tools.items() if not shutil.which(tool)]
    if missing:
        installer = "brew install" if system == "Darwin" else "install them with your package manager:"
        die(f"required tools not found ({installer} {' '.join(missing)})")


# --- git -----------------------------------------------------------------------


class GitError(Exception):
    pass


class Git:
    """A git command line with fixed options and environment."""

    def __init__(self, args: List[str], env: dict):
        self.args = args
        self.env = env

    def environment(self, extra: Optional[dict]) -> dict:
        env = dict(os.environ)
        env.update(self.env)
        if extra:
            env.update(extra)
        return env

    def run(self, *args: str, input: Optional[str] = None, quiet: bool = False,
            env: Optional[dict] = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *self.args, *args],
            input=input,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL if quiet else None,
            env=self.environment(env),
            encoding="utf-8",
            errors="surrogateescape",
        )

    def out(self, *args: str, **kw) -> str:
        """stdout without the trailing newline; raises GitError on failure."""
        proc = self.run(*args, **kw)
        if proc.returncode != 0:
            raise GitError(" ".join(args))
        return proc.stdout[:-1] if proc.stdout.endswith("\n") else proc.stdout

    def ok(self, *args: str, **kw) -> bool:
        return self.run(*args, **kw).returncode == 0

    def value(self, *args: str, **kw) -> str:
        """stdout, or an empty string on failure (quiet)."""
        kw.setdefault("quiet", True)
        proc = self.run(*args, **kw)
        if proc.returncode != 0:
            return ""
        return proc.stdout.strip("\n")


# Options forced on every git command the wrapper runs on the copy. Claude can
# write to the copy, so its hooks, fsmonitor, replace refs and commit-graph are
# never trusted, and missing objects are never fetched.
_HARDENED = [
    "core.hooksPath=/dev/null", "core.fsmonitor=false", "core.untrackedCache=false",
    "core.commitGraph=false", "core.sshCommand=false", "core.pager=cat", "core.askPass=false",
    "diff.external=", "core.alternateRefsCommand=", "uploadpack.packObjectsHook=",
    "credential.helper=", "protocol.allow=never", "protocol.file.allow=always",
    "fetch.writeCommitGraph=false", "gc.auto=0", "maintenance.auto=false",
    "advice.detachedHead=false", "advice.graftFileDeprecated=false",
]


def git_copy_raw(copy_git: str, work: str) -> Git:
    """For working tree operations (stash, checkout): placeholders stay
    active, they only write into Claude's copy."""
    args: List[str] = []
    for option in _HARDENED:
        args += ["-c", option]
    return Git(args, {
        "GIT_DIR": copy_git, "GIT_WORK_TREE": work,
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_NO_LAZY_FETCH": "1", "GIT_GRAFT_FILE": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0", "GIT_ATTR_NOSYSTEM": "1",
    })


def git_copy(copy_git: str, work: str) -> Git:
    """For checks and reads of Claude's commits: without replace refs."""
    g = git_copy_raw(copy_git, work)
    g.env["GIT_NO_REPLACE_OBJECTS"] = "1"
    return g


def git_dev(dev_root: str) -> Git:
    """Git commands on the developer's repository (trusted)."""
    return Git(["-C", dev_root], {"GIT_TERMINAL_PROMPT": "0"})


# --- lock ----------------------------------------------------------------------


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class Lock:
    """One session per repository: a directory holding the owner's PID."""

    def __init__(self, state: str):
        self.path = os.path.join(state, "lock")
        self.held = False

    def acquire(self) -> None:
        try:
            os.mkdir(self.path)
        except FileExistsError:
            try:
                pid = int(read_text(os.path.join(self.path, "pid")).strip())
            except (OSError, ValueError):
                pid = 0
            if pid and _alive(pid):
                die(f"a {NAME} session is already running for this repository (pid {pid})")
            shutil.rmtree(self.path, ignore_errors=True)
            try:
                os.mkdir(self.path)
            except OSError:
                die(f"cannot take the lock {self.path}")
        write_text(os.path.join(self.path, "pid"), f"{os.getpid()}\n")
        self.held = True

    def release(self) -> None:
        if self.held:
            shutil.rmtree(self.path, ignore_errors=True)
            self.held = False
