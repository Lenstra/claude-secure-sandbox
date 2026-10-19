"""Scan of the whole computer for protected repositories, at each launch.

Every protected repository other than the session's is hidden entirely: the
scan must be complete and current, so it runs before each session.
"""

from __future__ import annotations

import glob
import os
import platform
import shutil
import subprocess
from typing import List

from .common import LEGACY_PROTECT_FILE, SANDBOX_FILE, die, path_within, read_lines, realpath, volume_roots
from .sandbox_file import Grants

# macOS reaches the data volume through firmlinks; fd reports it under this prefix.
MACOS_DATA = "/System/Volumes/Data"
# Linux filesystems on local disks; virtual and network ones are not scanned.
LOCAL_FILESYSTEMS = {"ext2", "ext3", "ext4", "xfs", "btrfs", "zfs", "f2fs", "jfs", "reiserfs",
                     "vfat", "exfat", "ntfs", "ntfs3", "fuseblk"}


def find_sandbox_files(root: str, one_file_system: bool = False, timeout: float = None) -> List[str]:
    """Every .claudesandbox (and earlier .claudeprotected) under root, a file
    or a link (fd, else find), skipping .git and node_modules, and with fd
    ~/Library when root is the home directory (only for speed). Stops the
    launch if the scan does not finish in time: a repository it did not
    reach would not be hidden."""
    # fd is installed as fdfind on Debian and Ubuntu.
    fd = shutil.which("fd") or shutil.which("fdfind")
    if fd:
        cmd = [fd, "-H", "-I", "-t", "f", "-t", "l", "--exclude", ".git", "--exclude", "node_modules"]
        if root == os.environ["HOME"]:
            # Anchored: "Library" alone would skip every directory of that
            # name, and the protected repositories inside.
            cmd += ["--exclude", "/Library"]
        if one_file_system:
            cmd += ["--one-file-system"]
        cmd += [f"^\\.({SANDBOX_FILE[1:]}|{LEGACY_PROTECT_FILE[1:]})$", root]
    else:
        cmd = ["find", root] + (["-xdev"] if one_file_system else []) + [
            "(", "-name", ".git", "-o", "-name", "node_modules", ")", "-prune", "-o",
            "(", "-type", "f", "-o", "-type", "l", ")",
            "(", "-name", SANDBOX_FILE, "-o", "-name", LEGACY_PROTECT_FILE, ")", "-print"]
    try:
        output = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, encoding="utf-8",
                                errors="surrogateescape", timeout=timeout).stdout
    except subprocess.TimeoutExpired:
        die(f"the scan of {root} for protected repositories did not finish in {timeout:.0f} s: "
            "a repository it did not reach would not be hidden")
    found = []
    for f in output.splitlines():
        f = f.rstrip("/")
        if f:
            found.append(f if f.startswith("/") else f"{root}/{f[2:] if f.startswith('./') else f}")
    return found


def protects(directory: str) -> bool:
    """True if the directory is protected: it holds a .claudesandbox, or an
    earlier .claudeprotected."""
    return any(os.path.isfile(os.path.join(directory, f)) for f in (SANDBOX_FILE, LEGACY_PROTECT_FILE))


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


def scan(state_root: str, roots: List[str]) -> List[str]:
    """Directories that hold a .claudesandbox (or .claudeprotected), under roots, each scanned
    without leaving its filesystem; the wrapper's state is left out (its
    copies hold one too)."""
    found, seen = set(), set()
    for root in roots:
        real = realpath(root)
        if real in seen or not os.path.isdir(real):
            continue
        seen.add(real)
        for f in find_sandbox_files(real, one_file_system=True, timeout=600):
            directory = os.path.dirname(f)
            if platform.system() == "Darwin" and directory.startswith(MACOS_DATA + "/"):
                directory = directory[len(MACOS_DATA):]
            if not path_within(state_root, directory):
                found.add(directory)
    return sorted(found)


def protected_repositories(state_root: str, roots: List[str]) -> List[str]:
    """Every protected repository: found under roots now, or used with the
    wrapper before, which covers one where the scan cannot go, and one
    protected with a branch checked out that has no .claudesandbox."""
    found = {r for r in scan(state_root, roots) if protects(r)}
    for marker in glob.glob(os.path.join(state_root, "*", "state", "repository")):
        state = os.path.dirname(marker)
        for repo in read_lines(marker):
            if protects(repo) or (os.path.isdir(repo) and Grants(state).get("protected")):
                found.add(repo)
    return sorted(found)
