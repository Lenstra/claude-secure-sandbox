"""The .claudesandbox file at a repository's root: reading and validation,
what Claude may see in the repository, and the developer's confirmations.

The file's presence protects the repository: Claude sees nothing in it but
what the file lists. Every section is an allowlist: [allowed-git]
(repository paths Claude sees, files and history, "." for all of them),
[allowed-external] (paths outside the repository Claude may read) and
[allowed-domain] (network domains). Claude can change the file, so what it
opens only widens with the developer's confirmation in the terminal, before
Claude starts.
"""

from __future__ import annotations

import ipaddress
import os
import re
from typing import List, Set, Tuple

from .common import (SANDBOX_FILE, Git, ask, die, expand_home, log, path_within, read_lines, read_text, sha1,
                     warn, write_lines)

SECTIONS = ("allowed-git", "allowed-external", "allowed-domain")
_PATTERN_CHARS = set("*?[\\")
_DOMAIN = re.compile(r"^(\*\.)?[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*(:[0-9]{1,5})?$")
_IPV6 = re.compile(r"^\[([0-9a-f:.]+)\](:[0-9]{1,5})?$")


# --- reading -------------------------------------------------------------------


class SandboxFile:
    """Content of a .claudesandbox. Lines outside a section, unknown sections
    and invalid entries are listed in errors."""

    def __init__(self, content: str = ""):
        # True when the file exists: the repository is then protected.
        self.exists = False
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
                if section not in SECTIONS:
                    self.errors.append(f"line {number}: unknown section {line} "
                                       "(sections: [allowed-git], [allowed-external], [allowed-domain])")
                continue
            if section is None:
                self.errors.append(f"line {number}: outside a section: {line}")
                continue
            try:
                if section == "allowed-git":
                    self.visible.add(normalize_visible(line))
                elif section == "allowed-external":
                    self.paths.add(normalize_read_path(line))
                elif section == "allowed-domain":
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
    content.exists = True
    if content.errors:
        die(f"invalid {SANDBOX_FILE}:" + "".join(f"\n  {e}" for e in content.errors))
    return content


def normalize_visible(line: str) -> str:
    """An [allowed-git] entry: a literal path relative to the repository root,
    or "." for the whole repository. Raises ValueError."""
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
    if path in ("", "."):
        return "."
    wrapped = f"/{path}/"
    if "/../" in wrapped or "/./" in wrapped:
        raise ValueError(f"path outside the repository root not allowed: {line}")
    if "/.git/" in wrapped:
        raise ValueError(f".git path not allowed: {line}")
    if path == SANDBOX_FILE:
        raise ValueError(f"{SANDBOX_FILE} is always visible: {line}")
    return path


def normalize_read_path(line: str) -> str:
    """An [allowed-external] entry: absolute or under ~, literal, never / or the home
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
    """An [allowed-domain] entry: a name, *.name or an address, with an optional
    port. Raises ValueError."""
    domain = line.lower()
    if domain == "*" or domain.startswith("*:"):
        raise ValueError('"*" not allowed')
    if domain.startswith("["):
        bracketed = _IPV6.match(domain)
        try:
            ipaddress.IPv6Address(bracketed.group(1) if bracketed else "")
        except ValueError:
            raise ValueError(f"invalid address: {line}") from None
    elif not _DOMAIN.match(domain):
        raise ValueError(f"invalid domain: {line}")
    return domain


# --- what Claude sees in a protected repository --------------------------------


def is_visible(visible: List[str], rel: str) -> bool:
    """True if rel (relative to the root) is an [allowed-git] path or lies under one."""
    return rel == SANDBOX_FILE or any(v == "." or path_within(v, rel) for v in visible)


def leads_to_visible(visible: List[str], rel: str) -> bool:
    """True if an [allowed-git] path lies under the directory rel."""
    return rel == "" or any(v == "." or path_within(rel, v) for v in visible)


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


def ever_protected(git_dev: Git) -> bool:
    """True if the file was ever committed in HEAD's history."""
    return bool(git_dev.value("-c", "log.showSignature=false", "log", "-1", "--format=%H", "--full-history",
                              "HEAD", "--", SANDBOX_FILE))


# --- confirmations -------------------------------------------------------------


class Grants:
    """What the developer confirmed for a repository, one "<kind> <value>" line
    each in state/granted: "see" ([allowed-git]), "read" ([allowed-external]),
    "domain" ([allowed-domain]), and "unprotected" once the developer stopped
    protecting it. The wrapper's state is out of every sandbox's reach."""

    def __init__(self, state: str):
        self.path = os.path.join(state, "granted")
        self.lines = read_lines(self.path)

    def get(self, kind: str) -> List[str]:
        return [value for k, _, value in (line.partition(" ") for line in self.lines) if k == kind]

    def set(self, kind: str, values: List[str]) -> None:
        lines = [line for line in self.lines if line.partition(" ")[0] != kind]
        lines += [f"{kind} {v}".rstrip() for v in values]
        if lines != self.lines:
            self.lines = lines
            write_lines(self.path, lines)


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


def confirm_visible(state: str, current: SandboxFile, was_protected: bool) -> Tuple[bool, List[str]]:
    """Each [allowed-git] entry is shown to Claude once the developer has
    confirmed it, unless it lies under a confirmed one (narrowing never asks);
    a confirmation ends when its entry leaves the file. A repository that was
    protected stays protected until the developer confirms it no longer is.
    Returns (protected, the paths Claude may see)."""
    grants = Grants(state)
    seen = grants.get("see")
    if not current.exists:
        if not was_protected or grants.get("unprotected"):
            return False, []
        if _ask(f"this repository was protected, and its {SANDBOX_FILE} is gone: Claude would read all of it",
                [], "stop protecting it? [y/N] ", "protection removed",
                f"it stays protected; restore {SANDBOX_FILE} to stop this question",
                "no terminal to confirm: it stays protected"):
            grants.set("see", [])
            grants.set("unprotected", [""])
            return False, []
        return True, seen
    confirmed = sorted(e for e in current.visible if any(c == "." or path_within(c, e) for c in seen))
    new = sorted(current.visible - set(confirmed))
    if new and _ask(f"{SANDBOX_FILE} shows Claude paths it could not see before:", new,
                    "show them to Claude? [y/N] ", "paths shown",
                    "they stay hidden; the question comes back at the next launch",
                    "no terminal to confirm: they stay hidden"):
        confirmed = sorted(confirmed + new)
    grants.set("unprotected", [])
    grants.set("see", confirmed)
    return True, confirmed


def confirm_access(state: str, paths: List[str], domains: List[str]) -> Tuple[List[str], List[str]]:
    """Each [allowed-external] and [allowed-domain] entry is granted once the
    developer has confirmed it; a confirmation ends when its entry leaves the
    file. Returns the granted (paths, domains)."""
    grants = Grants(state)
    granted = {"read": [p for p in paths if p in grants.get("read")],
               "domain": [d for d in domains if d in grants.get("domain")]}
    new = [f"read {p}" for p in paths if p not in granted["read"]]
    new += [f"domain {d}" for d in domains if d not in granted["domain"]]
    if new and _ask(f"{SANDBOX_FILE} asks for access Claude does not have by default:", new,
                    "allow it? [y/N] ", "access allowed",
                    "not allowed for this session; the question comes back at the next launch",
                    "no terminal to confirm: not allowed for this session"):
        granted = {"read": paths, "domain": domains}
    for kind, values in granted.items():
        grants.set(kind, values)
    return granted["read"], granted["domain"]
