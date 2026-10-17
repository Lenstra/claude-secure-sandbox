"""Scan of the whole computer for protected repositories, at each launch.

Every protected repository other than the session's is hidden entirely: the
scan must be complete and current, so it runs before each session.
"""

from __future__ import annotations

import glob
import os
import platform
from typing import List

from .common import path_within, read_lines, realpath, volume_roots
from .protect import PROTECT_FILE, find_protect_files

# macOS reaches the data volume through firmlinks; fd reports it under this prefix.
MACOS_DATA = "/System/Volumes/Data"
# Linux filesystems on local disks; virtual and network ones are not scanned.
LOCAL_FILESYSTEMS = {"ext2", "ext3", "ext4", "xfs", "btrfs", "zfs", "f2fs", "jfs", "reiserfs",
                     "vfat", "exfat", "ntfs", "ntfs3", "fuseblk"}


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
    """Directories that hold a .claudeprotected, under roots, each scanned
    without leaving its filesystem; the wrapper's state is left out (its
    copies hold one too)."""
    found, seen = set(), set()
    for root in roots:
        real = realpath(root)
        if real in seen or not os.path.isdir(real):
            continue
        seen.add(real)
        for f in find_protect_files(real, one_file_system=True, timeout=600):
            directory = os.path.dirname(f)
            if platform.system() == "Darwin" and directory.startswith(MACOS_DATA + "/"):
                directory = directory[len(MACOS_DATA):]
            if not path_within(state_root, directory):
                found.add(directory)
    return sorted(found)


def protected_repositories(state_root: str, roots: List[str]) -> List[str]:
    """Every protected repository: found under roots now, or used with the
    wrapper before (in case it sits where the scan cannot go)."""
    found = set(scan(state_root, roots))
    for marker in glob.glob(os.path.join(state_root, "*", "state", "repository")):
        try:
            found.update(read_lines(marker))
        except OSError:
            pass
    return sorted(r for r in found if os.path.isfile(os.path.join(r, PROTECT_FILE)))
