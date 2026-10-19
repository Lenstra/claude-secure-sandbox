"""Which directories are protected, and the scan of the whole computer for
them at each launch.

A directory is protected if it holds a .claudesandbox, or if it is a git
repository that ever committed one at its root, on any branch or tag; its
git directory too, wherever it lives. This is read from the disk and from
git at each launch, never from the wrapper's state. Every protected
repository other than the session's is hidden entirely: the scan must be
complete and current, so it runs before each session.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from typing import List

from .common import LEGACY_PROTECT_FILE, SANDBOX_FILE, Git, die, path_within, realpath, volume_roots

# macOS reaches the data volume through firmlinks; fd reports it under this prefix.
MACOS_DATA = "/System/Volumes/Data"
# A scan that does not finish in this time stops the launch; a repository
# whose history does not answer in this time counts as protected.
SCAN_TIMEOUT, HISTORY_TIMEOUT = 600, 30
# Linux filesystems on local disks; virtual and network ones are not scanned.
LOCAL_FILESYSTEMS = {"ext2", "ext3", "ext4", "xfs", "btrfs", "zfs", "f2fs", "jfs", "reiserfs",
                     "vfat", "exfat", "ntfs", "ntfs3", "fuseblk"}


def find_markers(root: str) -> List[str]:
    """Under root: every .claudesandbox (and earlier .claudeprotected), a
    file or a link; every .git, whose repository may have been protected on
    any branch; every HEAD file, which may belong to a bare repository or to
    a git directory kept apart from its work tree (fd, else find), without
    leaving root's filesystem. Never inside .git or node_modules, and with fd
    not in ~/Library when root is the home directory (only for speed). Stops
    the launch if the scan does not finish in time: a repository it did not
    reach would not be hidden."""
    # fd is installed as fdfind on Debian and Ubuntu.
    fd = shutil.which("fd") or shutil.which("fdfind")
    if fd:
        cmd = [fd, "-H", "-I", "-t", "f", "-t", "l", "-t", "d", "--prune", "--exclude", "node_modules"]
        if root == os.environ["HOME"]:
            # Anchored: "Library" alone would skip every directory of that
            # name, and the protected repositories inside.
            cmd += ["--exclude", "/Library"]
        cmd += ["--one-file-system", f"^(\\.({SANDBOX_FILE[1:]}|{LEGACY_PROTECT_FILE[1:]}|git)|HEAD)$", root]
    else:
        cmd = ["find", root, "-xdev",
            "-name", "node_modules", "-prune", "-o", "-name", ".git", "-print", "-prune", "-o",
            "-type", "f", "-name", "HEAD", "-print", "-o",
            "(", "-type", "f", "-o", "-type", "l", ")",
            "(", "-name", SANDBOX_FILE, "-o", "-name", LEGACY_PROTECT_FILE, ")", "-print"]
    try:
        output = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, encoding="utf-8",
                                errors="surrogateescape", timeout=SCAN_TIMEOUT).stdout
    except subprocess.TimeoutExpired:
        die(f"the scan of {root} for protected repositories did not finish in {SCAN_TIMEOUT} s: "
            "a repository it did not reach would not be hidden")
    found = []
    for f in output.splitlines():
        f = f.rstrip("/")
        if f:
            found.append(f if f.startswith("/") else f"{root}/{f[2:] if f.startswith('./') else f}")
    return found


def protects(directory: str) -> bool:
    """True if the directory holds a .claudesandbox, or an earlier
    .claudeprotected."""
    return any(os.path.isfile(os.path.join(directory, f)) for f in (SANDBOX_FILE, LEGACY_PROTECT_FILE))


def _git(repo: str) -> Git:
    """git in a repository found on the computer, maybe another user's: read
    only, nothing of its configuration run."""
    return Git(["-C", repo, "-c", "safe.directory=*", "-c", "core.fsmonitor=false", "-c", "log.showSignature=false"],
               {})


def ever_protected(repo: str) -> bool:
    """True if a .claudesandbox (or earlier .claudeprotected) was ever
    committed at the root of the repository, on any branch or tag: once
    protected, it stays protected for every other session, whatever is
    checked out. A repository git cannot read (a broken ref) or that does not
    answer in time counts as protected."""
    try:
        proc = _git(repo).run("log", "-1", "--format=%H", "--full-history", "--all", "--",
                              f":(top,literal){SANDBOX_FILE}", f":(top,literal){LEGACY_PROTECT_FILE}",
                              quiet=True, timeout=HISTORY_TIMEOUT)
    except subprocess.TimeoutExpired:
        return True
    return proc.returncode != 0 or bool(proc.stdout.strip())


def scan_roots() -> List[str]:
    """The system disk, the home directory and every other local disk."""
    roots = ["/", os.environ["HOME"]]
    if platform.system() == "Linux":
        try:
            with open("/proc/mounts", encoding="utf-8") as f:
                for line in f:
                    fields = line.split()
                    if len(fields) > 2 and fields[2] in LOCAL_FILESYSTEMS:
                        roots.append(fields[1].replace("\\040", " "))
        except OSError:
            pass
    for volumes in volume_roots():
        try:
            roots += [os.path.join(volumes, e) for e in sorted(os.listdir(volumes))]
        except OSError:
            pass
    return roots


def is_git_dir(path: str) -> bool:
    """A git directory on its own: a bare repository, or one kept apart from
    its work tree (git init --separate-git-dir)."""
    return all(os.path.exists(os.path.join(path, p)) for p in ("HEAD", "objects", "refs"))


def git_dir_outside(repo: str) -> str:
    """The git directory of a repository when it lies outside its work tree
    (a submodule's, in the parent's .git/modules), else an empty string."""
    git_dir = _git(repo).value("rev-parse", "--path-format=absolute", "--git-common-dir")
    git_dir = realpath(git_dir) if git_dir else ""
    return git_dir if git_dir and not path_within(repo, git_dir) else ""


def protected_repositories(state_root: str, roots: List[str]) -> List[str]:
    """Every protected directory under roots, each scanned without leaving
    its filesystem: one that holds a .claudesandbox, and every git
    repository that ever committed one, on any branch, with its git
    directory when it lies elsewhere, bare repositories included. Read from
    the disk and from git at each launch, never from the wrapper's state.
    The state itself is left out (its copies hold a .claudesandbox too)."""
    files, repos, seen = set(), set(), set()
    for root in roots:
        real = realpath(root)
        if real in seen or not os.path.isdir(real):
            continue
        seen.add(real)
        for f in find_markers(real):
            directory = os.path.dirname(f)
            if platform.system() == "Darwin" and directory.startswith(MACOS_DATA + "/"):
                directory = directory[len(MACOS_DATA):]
            if path_within(state_root, directory):
                continue
            name = os.path.basename(f)
            if name == ".git" or (name == "HEAD" and is_git_dir(directory)):
                repos.add(directory)
            elif name != "HEAD":
                files.add(directory)
    found = {d for d in files if protects(d)}
    others = sorted(repos - found)
    with ThreadPoolExecutor(max_workers=8) as pool:
        found |= {r for r, protected in zip(others, pool.map(ever_protected, others)) if protected}
        found |= {g for g in pool.map(git_dir_outside, sorted(found)) if g}
    return sorted(found)
