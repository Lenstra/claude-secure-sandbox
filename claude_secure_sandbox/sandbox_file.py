"""The .claudesandbox file at a repository's root: reading and validation,
what Claude may see in the repository, and the developer's confirmations.

Every section is an allowlist: [visible] (repository paths Claude may see;
the section, even empty, makes the repository protected), [path] (paths
Claude may read beyond the defaults) and [domain] (network domains). Claude
can change the file, so what it opens only widens with the developer's
confirmation in the terminal, before Claude starts.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from typing import List, Set, Tuple

from .common import (LEGACY_PROTECT_FILE, NAME, SANDBOX_FILE, Git, ask, die, expand_home, log, path_within,
                     read_lines, read_text, sha1, warn, write_lines)

SECTIONS = ("visible", "path", "domain")
_PATTERN_CHARS = set("*?[\\")
_DOMAIN = re.compile(r"^(\*\.)?[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*(:[0-9]{1,5})?$")
# Stored among the [visible] confirmations when the developer stops
# protecting a repository: never a valid path (patterns are refused).
UNPROTECTED = "*unprotected*"


# --- reading -------------------------------------------------------------------


class SandboxFile:
    """Content of a .claudesandbox. Lines outside a section, unknown sections
    and invalid entries are listed in errors."""

    def __init__(self, content: str = ""):
        self.has_visible = False
        self.visible: Set[str] = set()
        self.paths: Set[str] = set()
        self.domains: Set[str] = set()
        self.errors: List[str] = []
        section = None
        for number, raw in enumerate(content.splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1].strip().lower()
                self.has_visible = self.has_visible or section == "visible"
                if section == "protected":
                    self.errors.append(f"line {number}: [protected] is replaced by [visible]: "
                                       "list what Claude may see, everything else is hidden")
                elif section not in SECTIONS:
                    self.errors.append(f"line {number}: unknown section {line} "
                                       "(sections: [visible], [path], [domain])")
                continue
            if section is None:
                self.errors.append(f"line {number}: outside a section: {line}")
                continue
            try:
                if section == "visible":
                    self.visible.add(normalize_visible(line))
                elif section == "path":
                    self.paths.add(normalize_read_path(line))
                elif section == "domain":
                    self.domains.add(normalize_domain(line))
            except ValueError as e:
                self.errors.append(f"line {number}: {e}")


def read_sandbox_file(directory: str) -> SandboxFile:
    """The .claudesandbox of directory, on disk; empty if there is none. An
    invalid or unreadable file stops the launch. A version committed at HEAD
    but not on disk is not read: what Claude may see only widens with the
    developer's confirmation."""
    path = os.path.join(directory, SANDBOX_FILE)
    try:
        content = SandboxFile(read_text(path))
    except FileNotFoundError:
        return SandboxFile()
    except (OSError, UnicodeDecodeError):
        die(f"cannot read {path}")
    if content.errors:
        die(f"invalid {SANDBOX_FILE}:" + "".join(f"\n  {e}" for e in content.errors))
    return content


def normalize_visible(line: str) -> str:
    """A [visible] entry: a literal path relative to the repository root.
    Raises ValueError."""
    if line.startswith("/"):
        raise ValueError(f"absolute path not allowed: {line}")
    if any(c in _PATTERN_CHARS for c in line) or line.startswith("!"):
        raise ValueError(f"pattern not supported (literal paths only): {line}")
    path = line
    while path.startswith("./"):
        path = path[2:]
    path = re.sub(r"/{2,}", "/", path.rstrip("/"))
    while "/./" in path:
        path = path.replace("/./", "/")
    if path.endswith("/."):
        path = path[:-2]
    wrapped = f"/{path}/"
    if "/../" in wrapped or "/./" in wrapped:
        raise ValueError(f"path outside the repository root not allowed: {line}")
    if "/.git/" in wrapped:
        raise ValueError(f".git path not allowed: {line}")
    if path in ("", "."):
        raise ValueError(f"the whole repository: remove the [visible] section instead: {line}")
    if path == SANDBOX_FILE:
        raise ValueError(f"{SANDBOX_FILE} is always visible: {line}")
    return path


def normalize_read_path(line: str) -> str:
    """A [path] entry: absolute or under ~, literal, never / or the home
    directory itself. Raises ValueError."""
    path = expand_home(line)
    if not path.startswith("/"):
        raise ValueError(f"absolute path or ~/... expected: {line}")
    if any(c in _PATTERN_CHARS for c in path):
        raise ValueError(f"pattern not supported (literal paths only): {line}")
    if "/../" in path + "/" or "/./" in path + "/":
        raise ValueError(f". and .. not allowed: {line}")
    path = re.sub(r"/{2,}", "/", path).rstrip("/") or "/"
    if path in ("/", os.environ["HOME"]):
        raise ValueError(f"too broad: {line}")
    return path


def normalize_domain(line: str) -> str:
    """A [domain] entry: a name, *.name or an address, with an optional
    port. Raises ValueError."""
    domain = line.lower()
    if domain == "*" or domain.startswith("*:"):
        raise ValueError('"*" not allowed')
    if not _DOMAIN.match(domain) and not domain.startswith("["):
        raise ValueError(f"invalid domain: {line}")
    return domain


# --- what Claude sees in a protected repository ---------------------------------


def is_visible(visible: List[str], rel: str) -> bool:
    """True if rel (relative to the root) is a [visible] path or lies under one."""
    return rel == SANDBOX_FILE or any(path_within(v, rel) for v in visible)


def leads_to_visible(visible: List[str], rel: str) -> bool:
    """True if a [visible] path lies under the directory rel."""
    return rel == "" or any(path_within(rel, v) for v in visible)


def hidden_roots(visible: List[str], files: List[str]) -> Tuple[List[str], List[str]]:
    """The hidden entries among files (paths relative to the root), each as
    high in the tree as possible. Returns them all, and those that are
    directories."""
    roots, dirs = set(), set()
    for f in files:
        if is_visible(visible, f):
            continue
        parts = f.split("/")
        for i in range(1, len(parts) + 1):
            prefix = "/".join(parts[:i])
            if not leads_to_visible(visible, prefix):
                roots.add(prefix)
                if i < len(parts):
                    dirs.add(prefix)
                break
    return sorted(roots), sorted(dirs)


def visible_hash(visible: List[str]) -> str:
    return sha1("\n".join(visible) + "\n")


def sparse_escape(path: str) -> str:
    """Escapes a path for a sparse-checkout pattern (gitignore syntax)."""
    path = re.sub(r"([][*?\\])", r"\\\1", path)
    if path[:1] in ("!", "#"):
        path = "\\" + path
    if path.endswith(" "):
        path = path[:-1] + "\\ "
    return path


# --- protected repositories on the computer --------------------------------------


def find_sandbox_files(root: str, one_file_system: bool = False, timeout: float = None) -> List[str]:
    """Every .claudesandbox (and earlier .claudeprotected) under root (fd,
    else rg, else find), skipping .git and node_modules, and ~/Library when
    root is the home directory."""
    home = os.environ["HOME"]
    # fd is installed as fdfind on Debian and Ubuntu.
    fd = shutil.which("fd") or shutil.which("fdfind")
    if fd:
        cmd = [fd, "-H", "-I", "-t", "f", "--exclude", ".git", "--exclude", "node_modules"]
        if root == home:
            cmd += ["--exclude", "Library"]
        if one_file_system:
            cmd += ["--one-file-system"]
        cmd += [f"^\\.({SANDBOX_FILE[1:]}|{LEGACY_PROTECT_FILE[1:]})$", root]
    elif shutil.which("rg"):
        cmd = ["rg", "--files", "--hidden", "--no-ignore", "--no-messages",
               "-g", SANDBOX_FILE, "-g", LEGACY_PROTECT_FILE, "-g", "!.git", "-g", "!node_modules"]
        if root == home:
            cmd += ["-g", f"!{home}/Library"]
        if one_file_system:
            cmd += ["--one-file-system"]
        cmd += [root]
    else:
        cmd = ["find", root] + (["-xdev"] if one_file_system else []) + [
            "(", "-name", ".git", "-o", "-name", "node_modules", ")", "-prune",
            "-o", "-type", "f", "(", "-name", SANDBOX_FILE, "-o", "-name", LEGACY_PROTECT_FILE, ")", "-print"]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              encoding="utf-8", errors="surrogateescape", timeout=timeout)
        output = proc.stdout
    except subprocess.TimeoutExpired as e:
        output = e.stdout.decode("utf-8", "surrogateescape") if isinstance(e.stdout, bytes) else (e.stdout or "")
    found = []
    for f in output.splitlines():
        f = f.rstrip("/")
        if f:
            found.append(f if f.startswith("/") else f"{root}/{f[2:] if f.startswith('./') else f}")
    return found


def protects(directory: str) -> bool:
    """True if the repository at directory holds protected code: a
    .claudesandbox with a [visible] section, one that cannot be read or is
    invalid (when in doubt, it is protected), or an earlier .claudeprotected."""
    if os.path.isfile(os.path.join(directory, LEGACY_PROTECT_FILE)):
        return True
    path = os.path.join(directory, SANDBOX_FILE)
    if not os.path.isfile(path):
        return False
    try:
        content = SandboxFile(read_text(path))
    except (OSError, UnicodeDecodeError):
        return True
    return content.has_visible or bool(content.errors)


def ever_protected(git_dev: Git) -> bool:
    """True if a version of the file in HEAD's history has a [visible] section."""
    revs = git_dev.value("-c", "log.showSignature=false", "log", "--format=%H", "--full-history",
                         "HEAD", "--", SANDBOX_FILE).split()
    return any(SandboxFile(git_dev.value("cat-file", "blob", f"{rev}:{SANDBOX_FILE}")).has_visible
               for rev in revs)


# --- confirmations -------------------------------------------------------------


def _ask(title: str, items: List[str], question: str, yes: str, no: str, no_terminal: str) -> bool:
    """Asks the developer in the terminal, before Claude starts."""
    log(title + "".join(f"\n  {i}" for i in items))
    answer = ask(question)
    if answer is not None and answer.lower() in ("y", "yes"):
        log(yes)
        return True
    if answer is None:
        warn(no_terminal)
    else:
        log(no)
    return False


def _store(path: str, before: List[str], after: List[str]) -> None:
    if after != before:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        write_lines(path, after)


def confirm_visible(git_common: str, current: SandboxFile, was_protected: bool) -> Tuple[bool, List[str]]:
    """Each [visible] entry is shown to Claude once the developer has
    confirmed it, unless it lies under a confirmed one (narrowing never asks);
    a confirmation ends when its entry leaves the file. A repository that was
    protected stays protected until the developer confirms it no longer is.
    Confirmations live in the developer's .git, out of the sandbox's reach.
    Returns (protected, the paths Claude may see)."""
    store = os.path.join(git_common, NAME, "visible")
    stored = read_lines(store)
    if not current.has_visible:
        if not was_protected or UNPROTECTED in stored:
            return False, []
        if _ask(f"this repository was protected, and its {SANDBOX_FILE} has no [visible] section any more: "
                "Claude would read all of it", [], "stop protecting it? [y/N] ", "protection removed",
                f"it stays protected; restore the [visible] section of {SANDBOX_FILE} to stop this question",
                "no terminal to confirm: it stays protected"):
            _store(store, stored, [UNPROTECTED])
            return False, []
        return True, [e for e in stored if e != UNPROTECTED]
    confirmed = sorted(e for e in current.visible if any(path_within(c, e) for c in stored if c != UNPROTECTED))
    new = sorted(current.visible - set(confirmed))
    if new and _ask(f"{SANDBOX_FILE} shows Claude paths it could not see before:", new,
                    "show them to Claude? [y/N] ", "paths shown",
                    "they stay hidden; the question comes back at the next launch",
                    "no terminal to confirm: they stay hidden"):
        confirmed = sorted(confirmed + new)
    _store(store, stored, confirmed)
    return True, confirmed


def confirm_access(state: str, paths: List[str], domains: List[str]) -> Tuple[List[str], List[str]]:
    """Each [path] and [domain] entry is granted once the developer has
    confirmed it; a confirmation ends when its entry leaves the file.
    Confirmations live in the wrapper's state, out of the sandbox's reach.
    Returns the granted (paths, domains)."""
    store = os.path.join(state, "granted")
    stored = read_lines(store)
    asked = [f"read {p}" for p in paths] + [f"domain {d}" for d in domains]
    confirmed = [e for e in asked if e in stored]
    new = [e for e in asked if e not in stored]
    if new and _ask(f"{SANDBOX_FILE} asks for access Claude does not have by default:", new,
                    "allow it? [y/N] ", "access allowed",
                    "not allowed for this session; the question comes back at the next launch",
                    "no terminal to confirm: not allowed for this session"):
        confirmed += new
    _store(store, stored, confirmed)
    return ([e[len("read "):] for e in confirmed if e.startswith("read ")],
            [e[len("domain "):] for e in confirmed if e.startswith("domain ")])
