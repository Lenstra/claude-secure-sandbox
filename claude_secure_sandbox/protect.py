"""The .claudesandbox file: reading, validation, protected paths and their
removal, and the access a repository asks for."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from typing import List, Optional, Set

from .common import (LEGACY_PROTECT_FILE, NAME, SANDBOX_FILE, Git, ask, die, expand_home, log, path_within,
                     read_lines, read_text, sha1, warn, write_lines)

_PATTERN_CHARS = set("*?[\\")
SECTIONS = ("protected", "path", "domain")
_DOMAIN = re.compile(r"^(\*\.)?[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*(:[0-9]{1,5})?$")


class SandboxFile:
    """Content of a .claudesandbox: [protected] paths relative to the
    repository root, [path] absolute paths Claude may read, [domain] network
    domains it may reach. Lines outside a section, unknown sections and
    invalid entries are errors."""

    def __init__(self, content: str = ""):
        self.protected: Set[str] = set()
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
                                       "(sections: [protected], [path], [domain])")
                continue
            if section is None:
                self.errors.append(f"line {number}: outside a section: {line}")
                continue
            try:
                if section == "protected":
                    self.protected.add(normalize(line))
                elif section == "path":
                    self.paths.add(normalize_read_path(line))
                elif section == "domain":
                    self.domains.add(normalize_domain(line))
            except ValueError as e:
                self.errors.append(f"line {number}: {e}")


def load_sandbox_file(path: str) -> SandboxFile:
    try:
        return SandboxFile(read_text(path))
    except FileNotFoundError:
        return SandboxFile()
    except (OSError, UnicodeDecodeError):
        die(f"cannot read {path}")


def normalize_read_path(line: str) -> str:
    """A [path] entry: absolute or under ~, literal, never / or the home
    directory itself."""
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
    """A [domain] entry: a name, *.name, or an address, with an optional port."""
    domain = line.lower()
    if domain == "*" or domain.startswith("*:"):
        raise ValueError('"*" not allowed')
    if not _DOMAIN.match(domain) and not domain.startswith("["):
        raise ValueError(f"invalid domain: {line}")
    return domain


def find_protect_files(root: str, one_file_system: bool = False, timeout: float = None) -> List[str]:
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
        if not f:
            continue
        if not f.startswith("/"):
            f = f"{root}/{f[2:] if f.startswith('./') else f}"
        found.append(f)
    return found


def normalize(line: str) -> Optional[str]:
    """Normalizes and validates one line of the protection file. Returns the
    normalized path, or None for an empty line or a comment. Raises
    ValueError with a message."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("/"):
        raise ValueError(f"absolute path not allowed: {line}")
    if any(c in _PATTERN_CHARS for c in line) or line.startswith("!"):
        raise ValueError(f"pattern not supported (literal paths only): {line}")
    path = line
    while path.startswith("./"):
        path = path[2:]
    path = path.rstrip("/")
    path = re.sub(r"/{2,}", "/", path)
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
        raise ValueError(f"protecting the whole repository makes no sense: {line}")
    if path == SANDBOX_FILE:
        raise ValueError(f"{SANDBOX_FILE} cannot protect itself: {line}")
    return path


def _committed(git_dev: Git, rev: str) -> SandboxFile:
    return SandboxFile(git_dev.value("cat-file", "blob", f"{rev}:{SANDBOX_FILE}"))


def read_protected(dev_root: str, git_dev: Git) -> List[str]:
    """The protected paths: those of the file on disk and of the version
    committed at HEAD, union of both. An invalid file stops the launch."""
    on_disk = load_sandbox_file(os.path.join(dev_root, SANDBOX_FILE))
    committed = _committed(git_dev, "HEAD")
    errors = [f"{SANDBOX_FILE}: {e}" for e in on_disk.errors]
    errors += [f"{SANDBOX_FILE} at HEAD: {e}" for e in committed.errors]
    if errors:
        die("invalid file:" + "".join(f"\n  {e}" for e in errors))
    return sorted(on_disk.protected | committed.protected)


def protect_hash(protected: List[str]) -> str:
    return sha1("\n".join(protected) + "\n")


def history_protected(git_dev: Git) -> Set[str]:
    """Every path protected by a version of the file in HEAD's history."""
    revs = git_dev.value("-c", "log.showSignature=false", "log", "--format=%H", "--full-history",
                         "HEAD", "--", SANDBOX_FILE).split()
    paths: Set[str] = set()
    for rev in revs:
        paths |= _committed(git_dev, rev).protected
    return paths


def keep_removed_protections(git_dev: Git, git_common: str, protected: List[str],
                             history: Set[str]) -> List[str]:
    """Paths protected by an earlier version of the file stay
    protected until the developer confirms their removal here, outside the
    sandbox. Claude's commits carry the developer's identity: a commit that
    drops a line from the file looks like any other change once merged.
    Returns the paths to protect."""
    path = os.path.join(git_common, NAME, "unprotected")
    # A confirmation ends when the path is protected again.
    confirmed = sorted(p for p in read_lines(path) if not is_protected_path(protected, p))
    removed = sorted(p for p in history if not is_protected_path(protected, p) and p not in confirmed)
    if removed:
        log(f"paths no longer under [protected] in {SANDBOX_FILE}, Claude would be able to read them:"
            + "".join(f"\n  {p}" for p in removed))
        answer = ask("remove their protection? [y/N] ")
        if answer is not None and answer.lower() in ("y", "yes"):
            confirmed = sorted(set(confirmed) | set(removed))
            log("protection removed")
            removed = []
        elif answer is None:
            warn("no terminal to confirm: they stay protected")
        else:
            log(f"they stay protected; restore them in {SANDBOX_FILE} to stop this question")
    if confirmed != read_lines(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        write_lines(path, confirmed)
    return sorted(set(protected) | set(removed))


def sparse_escape(path: str) -> str:
    """Escapes a path for a sparse-checkout pattern (gitignore syntax)."""
    path = re.sub(r"([][*?\\])", r"\\\1", path)
    if path[:1] in ("!", "#"):
        path = "\\" + path
    if path.endswith(" "):
        path = path[:-1] + "\\ "
    return path


def is_protected_path(protected: List[str], rel: str) -> bool:
    """True if rel (relative to the root) is protected or under a protected path."""
    return any(path_within(p, rel) for p in protected)


def protects(directory: str) -> bool:
    """True if the repository at directory holds protected code: a
    .claudesandbox with [protected] paths, or one that cannot be read (when in
    doubt, it is protected), or an earlier .claudeprotected."""
    if os.path.isfile(os.path.join(directory, LEGACY_PROTECT_FILE)):
        return True
    path = os.path.join(directory, SANDBOX_FILE)
    if not os.path.isfile(path):
        return False
    try:
        content = SandboxFile(read_text(path))
    except (OSError, UnicodeDecodeError):
        return True
    return bool(content.protected) or bool(content.errors)


def confirm_access(state: str, paths: List[str], domains: List[str]) -> tuple:
    """What a .claudesandbox asks for beyond the defaults is granted once the
    developer has confirmed it here, outside the sandbox: Claude can write the
    file (in directory mode directly, in protected mode through a commit the
    developer merges). Confirmations live in the wrapper's state, out of
    Claude's reach, and end when the entry leaves the file. Returns the
    granted (paths, domains)."""
    store = os.path.join(state, "granted")
    asked = [f"read {p}" for p in paths] + [f"domain {d}" for d in domains]
    confirmed = [e for e in read_lines(store) if e in asked]
    new = [e for e in asked if e not in confirmed]
    if new:
        log(f"{SANDBOX_FILE} asks for access Claude does not have by default:"
            + "".join(f"\n  {e}" for e in new))
        answer = ask("allow it? [y/N] ")
        if answer is not None and answer.lower() in ("y", "yes"):
            confirmed += new
            log("access allowed")
        elif answer is None:
            warn("no terminal to confirm: not allowed for this session")
        else:
            log("not allowed for this session; the question comes back at the next launch")
    if confirmed != read_lines(store):
        write_lines(store, confirmed)
    return ([e[len("read "):] for e in confirmed if e.startswith("read ")],
            [e[len("domain "):] for e in confirmed if e.startswith("domain ")])
