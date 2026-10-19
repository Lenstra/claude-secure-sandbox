"""State of one wrapper run: paths, access and git command lines."""

from __future__ import annotations

import os
import re
from typing import List, Optional

from .common import Git, Lock, die, ensure_real_dir, git_copy, git_copy_worktree, git_dev, sha1, warn, write_text


class Context:
    def __init__(self, root: str, state_root: str):
        self.root = root
        self.state_root = state_root
        # The session's Claude configuration, one per repository (setup_paths).
        self.claude_home = ""
        self.real_claude = ""
        self.srt = ""
        self.dev_root = ""
        self.dev_git_common = ""
        # True when dev_root holds a .claudesandbox: Claude works on a filtered
        # copy of it. Otherwise nothing is protected here, and Claude works in
        # dev_root itself.
        self.protected = False
        # In a protected repository: the confirmed [allowed-git] paths, the
        # only ones Claude sees; the hidden paths of HEAD, each as high in the
        # tree as possible (write-denied, and listed in the system prompt),
        # and those of them that are directories.
        self.visible: List[str] = []
        self.hidden: List[str] = []
        self.hidden_dirs: List[str] = []
        self.visible_hash = ""
        self.spec = ""
        self.fresh_copy = False
        # Protected repositories other than the session's, entirely hidden.
        self.protected_elsewhere: List[str] = []
        # Not protected, and dev_root holds the home directory: read only.
        self.home_session = False
        # Real paths of the ~/.claude items passed through to the sandbox.
        self.config_read: List[str] = []
        # The .claudesandbox of dev_root, and what it asks for, once confirmed.
        self.sandbox_file = None
        self.granted_paths: List[str] = []
        self.granted_domains: List[str] = []
        # srt's own temp directory, private to the session (sandbox.make_srt_tmp).
        self.srt_tmp = ""
        self.lock: Optional[Lock] = None

    @property
    def protected_code(self) -> List[str]:
        """Every protected path of the computer: the other protected
        repositories, and this one with its git directory when it is
        protected (Claude then works on the copy)."""
        own = [self.dev_root, self.dev_git_common] if self.protected else []
        return list(self.protected_elsewhere) + own

    @property
    def protected_code_exists(self) -> bool:
        return bool(self.protected_code)

    @property
    def git_dev(self) -> Git:
        return git_dev(self.dev_root)

    @property
    def git_copy(self) -> Git:
        return git_copy(self.copy_git, self.work)

    @property
    def git_copy_worktree(self) -> Git:
        return git_copy_worktree(self.copy_git, self.work)

    def state_file(self, name: str) -> str:
        return os.path.join(self.state, name)

    def setup_paths(self) -> None:
        name = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(self.dev_root))
        self.repo_dir = os.path.join(self.state_root, f"{name}-{sha1(self.dev_root)[:10]}")
        self.work = os.path.join(self.repo_dir, "work")
        self.copy_git = os.path.join(self.work, ".git")
        self.state = os.path.join(self.repo_dir, "state")
        self.run_dir = os.path.join(self.repo_dir, "run")
        self.tmp = os.path.join(self.repo_dir, "tmp")
        # Never shared between repositories: what a session writes there would
        # reach another repository's sessions, which allow other domains.
        self.claude_home = os.path.join(self.repo_dir, "claude-home")
        # Wrapper temp files: never in tmp/, which Claude can modify (a symlink
        # there would make the wrapper write anywhere).
        self.wtmp = os.path.join(self.state, "tmp")
        self.archive = os.path.join(self.repo_dir, "archive")
        # The upload-pack command given to git goes through a shell.
        if "'" in self.repo_dir or '"' in self.repo_dir:
            die(f"path {self.repo_dir} contains quotes, not supported")
        try:
            for d in (self.state, self.wtmp, self.run_dir, self.archive):
                os.makedirs(d, exist_ok=True)
        except OSError:
            die(f"cannot create {self.repo_dir}")
        # The repository this state belongs to, for uninstall.sh.
        write_text(self.state_file("repository"), self.dev_root + "\n")
        ensure_real_dir(self.tmp)
        if os.path.islink(self.work):
            warn(f"{self.work} was replaced by a link: removed")
            os.unlink(self.work)
