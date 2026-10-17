"""Detection and validation of the protection file (.claudeprotected)."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from typing import List, Optional, Set

from .common import (NAME, PROTECT_FILE, Git, ask, die, log, path_within, read_lines, read_text,
                     sha1, warn, write_lines)

_PATTERN_CHARS = set("*?[\\")


def find_nested_protect_files(root: str, one_file_system: bool = False) -> List[str]:
    """Protection files under root, other than root/.claudeprotected."""
    home = os.environ["HOME"]
    # fd is installed as fdfind on Debian and Ubuntu.
    fd = shutil.which("fd") or shutil.which("fdfind")
    if fd:
        cmd = [fd, "-H", "-I", "-t", "f", "--exclude", ".git", "--exclude", "node_modules"]
        if root == home:
            cmd += ["--exclude", "Library"]
        if one_file_system:
            cmd += ["--one-file-system"]
        cmd += ["^\\." + PROTECT_FILE[1:] + "$", root]
    elif shutil.which("rg"):
        cmd = ["rg", "--files", "--hidden", "--no-ignore", "--no-messages",
               "-g", PROTECT_FILE, "-g", "!.git", "-g", "!node_modules"]
        if root == home:
            cmd += ["-g", f"!{home}/Library"]
        cmd += [root]
    else:
        cmd = ["find", root, "(", "-name", ".git", "-o", "-name", "node_modules", ")", "-prune",
               "-o", "-type", "f", "-name", PROTECT_FILE, "-print"]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          encoding="utf-8", errors="surrogateescape")
    found = []
    for f in proc.stdout.splitlines():
        f = f.rstrip("/")
        if not f or f in (f"{root}/{PROTECT_FILE}", f"./{PROTECT_FILE}", PROTECT_FILE):
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
    if path == PROTECT_FILE:
        raise ValueError(f"{PROTECT_FILE} cannot protect itself: {line}")
    return path


def _paths_in(content: str) -> Set[str]:
    """Valid paths of a protection file; invalid lines are ignored."""
    paths = set()
    for line in content.splitlines():
        try:
            norm = normalize(line)
        except ValueError:
            continue
        if norm:
            paths.add(norm)
    return paths


def read_protect_file(dev_root: str, git_dev: Git) -> List[str]:
    """Reads the protection file (the version on disk and the one committed at
    HEAD, union of both). Returns the sorted paths."""
    try:
        content = read_text(os.path.join(dev_root, PROTECT_FILE))
    except OSError:
        content = ""
    content += "\n" + git_dev.value("cat-file", "blob", f"HEAD:{PROTECT_FILE}")
    errors = []
    for line in content.splitlines():
        try:
            normalize(line)
        except ValueError as e:
            errors.append(str(e))
    if errors:
        die(f"invalid {PROTECT_FILE}:" + "".join(f"\n  {e}" for e in errors))
    paths = _paths_in(content)
    if not paths:
        die(f"{PROTECT_FILE} lists no path")
    return sorted(paths)


def protect_hash(protected: List[str]) -> str:
    return sha1("\n".join(protected) + "\n")


def _history_paths(git_dev: Git) -> Set[str]:
    """Every path listed by a version of the protection file in HEAD's history."""
    revs = git_dev.value("-c", "log.showSignature=false", "log", "--format=%H", "--full-history",
                         "HEAD", "--", PROTECT_FILE).split()
    paths: Set[str] = set()
    for rev in revs:
        paths |= _paths_in(git_dev.value("cat-file", "blob", f"{rev}:{PROTECT_FILE}"))
    return paths


def keep_removed_protections(git_dev: Git, git_common: str, protected: List[str]) -> List[str]:
    """Paths protected by an earlier version of the protection file stay
    protected until the developer confirms their removal here, outside the
    sandbox. Claude's commits carry the developer's identity: a commit that
    drops a line from the file looks like any other change once merged.
    Returns the paths to protect."""
    path = os.path.join(git_common, NAME, "unprotected")
    # A confirmation ends when the path is protected again.
    confirmed = sorted(p for p in read_lines(path) if not is_protected_path(protected, p))
    removed = sorted(p for p in _history_paths(git_dev)
                     if not is_protected_path(protected, p) and p not in confirmed)
    if removed:
        log(f"paths no longer listed in {PROTECT_FILE}, Claude would be able to read them:"
            + "".join(f"\n  {p}" for p in removed))
        answer = ask("remove their protection? [y/N] ")
        if answer is not None and answer.lower() in ("y", "yes"):
            confirmed = sorted(set(confirmed) | set(removed))
            log("protection removed")
            removed = []
        elif answer is None:
            warn("no terminal to confirm: they stay protected")
        else:
            log(f"they stay protected; restore them in {PROTECT_FILE} to stop this question")
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
