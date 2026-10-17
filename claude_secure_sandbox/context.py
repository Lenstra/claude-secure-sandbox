"""State of one wrapper run: configuration, paths and git command lines."""

from __future__ import annotations

import os
import re
from typing import List, Optional

from .common import (Config, Git, Lock, die, ensure_real_dir, git_copy, git_copy_raw,
                     git_dev, sha1, warn, write_text)


class Context:
    def __init__(self, root: str, config: Config, state_root: str):
        self.root = root
        self.config = config
        self.state_root = state_root
        self.claude_home = os.path.join(state_root, "claude-home")
        self.real_claude = ""
        self.srt = ""
        self.dev_root = ""
        self.dev_git_common = ""
        self.protected: List[str] = []
        self.protect_hash = ""
        self.placeholder = ""
        self.spec = ""
        self.fresh_copy = False
        # Directory mode: no .claudeprotected; Claude works in dev_root itself,
        # and nothing else of the user's is readable.
        self.directory_mode = False
        # Protected repositories other than the session's, entirely hidden.
        self.protected_elsewhere: List[str] = []
        # Directory mode in a directory that holds the home directory: read only.
        self.read_only_root = False
        # Real paths of the ~/.claude items passed through to the sandbox.
        self.config_read: List[str] = []
        self.lock: Optional[Lock] = None

    @property
    def git_dev(self) -> Git:
        return git_dev(self.dev_root)

    @property
    def git_copy(self) -> Git:
        return git_copy(self.copy_git, self.work)

    @property
    def git_copy_wt(self) -> Git:
        return git_copy_raw(self.copy_git, self.work)

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
