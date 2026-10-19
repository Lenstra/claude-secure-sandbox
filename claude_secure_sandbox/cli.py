"""Entry point of the `claude` wrapper.

Every session runs Claude Code inside a sandbox (srt), where every protected
repository of the computer is hidden, neither readable nor writable. Started
in a protected repository (one with a .claudesandbox), Claude works on a
filtered copy of it, with only the paths the file lists. Anywhere else,
Claude works in the directory itself.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
from typing import List, NoReturn

from . import access, claude_config, filtered_copy, sandbox, scan, session
from .common import (LEGACY_PROTECT_FILE, SANDBOX_FILE, Lock, check_deps, check_seccomp, die, find_real_claude,
                     find_srt, log, path_within, realpath, state_root, volume_roots, warn)
from .context import Context
from .sandbox_file import (confirm_access, confirm_visible, ever_protected, hidden_roots, is_visible,
                           leads_to_visible, read_sandbox_file, visible_hash)

# Commands that do not open a session: no sandbox.
SESSIONLESS = {"-v", "--version", "-h", "--help", "update", "doctor", "mcp", "config", "plugin",
               "setup-token", "install", "migrate-installer"}
RESUME = {"-c", "--continue", "-r", "--resume"}


def exec_real(ctx: Context, args: List[str]) -> NoReturn:
    os.execv(ctx.real_claude, [ctx.real_claude, *args])


def _git_toplevel(cwd: str) -> str:
    proc = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"], stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, encoding="utf-8", errors="surrogateescape")
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _sigterm(*_) -> NoReturn:
    raise SystemExit(130)


def launch(ctx: Context, cwd: str, args: List[str]) -> int:
    """Prepares the sandbox and runs Claude Code in it."""
    paths, domains = sorted(ctx.sandbox_file.paths), sorted(ctx.sandbox_file.domains)
    access.check_access(ctx, paths, domains)
    ctx.granted_paths, ctx.granted_domains = confirm_access(ctx.state, paths, domains)
    claude_config.sync_claude_config(ctx)
    claude_config.sync_claude_credentials(ctx)
    sandbox.write_gitconfig(ctx)
    sandbox.write_tool_links(ctx)
    sandbox.make_srt_tmp(ctx)
    try:
        sandbox.write_srt_settings(ctx)
        sandbox.write_system_prompt(ctx)
        git_files = sandbox.git_files_present(ctx)
        status = sandbox.run_sandboxed(ctx, cwd, args)
        sandbox.neutralize_git_files(ctx, git_files)
    finally:
        shutil.rmtree(ctx.srt_tmp, ignore_errors=True)
    return status


def run_in_place(ctx: Context, cwd: str, args: List[str]) -> int:
    """Nothing protected here: Claude works directly in the directory (the
    repository root, or the current directory outside a repository)."""
    # A directory that holds the home directory (or other disks) is readable,
    # but not writable: programs outside the sandbox run what lives there
    # (shell startup files, LaunchAgents, tool configurations).
    home = os.environ["HOME"]
    ctx.home_session = ctx.dev_root == "/" or any(
        path_within(ctx.dev_root, s) for s in [home, os.path.dirname(home)] + volume_roots())
    ctx.setup_paths()
    ctx.work = ctx.dev_root
    ctx.copy_git = os.path.join(ctx.dev_root, ".git")
    signal.signal(signal.SIGTERM, _sigterm)
    try:
        return launch(ctx, cwd, args)
    except KeyboardInterrupt:
        return 130


def find_protected_elsewhere(ctx: Context, protected: bool) -> None:
    """Every other protected repository is hidden entirely: the whole computer
    is scanned at each launch. CLAUDE_SECURE_SANDBOX_SCAN=limited (tests)
    scans only the home directory and the session's directory."""
    home = os.environ["HOME"]
    if os.environ.get("CLAUDE_SECURE_SANDBOX_SCAN") == "limited":
        roots = [home] if path_within(home, ctx.dev_root) else [home, ctx.dev_root]
    else:
        if sys.stderr.isatty():
            log("looking for protected repositories on this computer...")
        roots = scan.scan_roots()
    repos = scan.protected_repositories(ctx.state_root, roots)
    if protected:
        nested = [r for r in repos if r != ctx.dev_root and path_within(ctx.dev_root, r)]
        if nested:
            die(f"protected code in {nested[0]}, inside this repository: run claude from that directory")
    ctx.protected_elsewhere = [r for r in repos if r != ctx.dev_root]


def run_protected(ctx: Context, cwd: str, args: List[str], was_protected: bool) -> int:
    """A .claudesandbox: Claude works in a filtered copy of the repository,
    with only the confirmed [allowed-git] paths."""
    git_dir = realpath(ctx.git_dev.out("rev-parse", "--absolute-git-dir"))
    common_dir = realpath(ctx.git_dev.out("rev-parse", "--path-format=absolute", "--git-common-dir"))
    if git_dir != common_dir:
        die("git worktrees are not supported (v1); run claude from the main repository")
    if os.path.isfile(os.path.join(ctx.dev_root, ".gitmodules")):
        warn("submodules are not supported (v1): their content will be missing from the copy")
    ctx.dev_git_common = common_dir

    protected, ctx.visible = confirm_visible(common_dir, ctx.sandbox_file, was_protected)
    if not protected:
        return run_in_place(ctx, cwd, args)
    ctx.protected = True
    ctx.visible_hash = visible_hash(ctx.visible)
    files = ctx.git_dev.value("ls-tree", "-r", "-z", "--name-only", "HEAD").split("\0")
    ctx.hidden, ctx.hidden_dirs = hidden_roots(ctx.visible, [f for f in files if f])

    rel = os.path.relpath(cwd, ctx.dev_root)
    rel = "" if rel == "." else rel
    if not (is_visible(ctx.visible, rel) or leads_to_visible(ctx.visible, rel)):
        die(f"{rel} is hidden from Claude (not under [allowed-git] in {SANDBOX_FILE}): "
            "run claude from a directory Claude can see")
    resume = any(a in RESUME or a.startswith("--resume=") for a in args)

    ctx.setup_paths()
    ctx.lock = Lock(ctx.state)
    signal.signal(signal.SIGTERM, _sigterm)
    try:
        ctx.lock.acquire()
        if ctx.git_dev.value("status", "--porcelain"):
            warn("the repository has uncommitted changes: Claude will not see them")
        filtered_copy.prepare_copy(ctx)
        session.ensure_dev_remote(ctx)
        if not session.sync_branches(ctx, new_session=not resume or ctx.fresh_copy):
            return 0
        launch_dir = ctx.work
        if rel and os.path.isdir(os.path.join(ctx.work, rel)):
            launch_dir = os.path.join(ctx.work, rel)
        before = session.branch_tips(ctx)
        status = launch(ctx, launch_dir, args)
        session.end_of_session(ctx, before)
        return status
    except KeyboardInterrupt:
        return 130
    finally:
        ctx.lock.release()


def main(root: str, args: List[str]) -> int:
    ctx = Context(root, state_root())
    ctx.real_claude = find_real_claude(root) or die("claude binary not found on the PATH")
    if args and args[0] in SESSIONLESS:
        exec_real(ctx, args)
    cwd = realpath(os.getcwd())
    # Already inside a sandbox, or inside a copy: nothing more to filter.
    if os.environ.get("CLAUDE_SECURE_SANDBOX_ACTIVE") or path_within(ctx.state_root, cwd):
        exec_real(ctx, args)

    toplevel = _git_toplevel(cwd)
    ctx.dev_root = realpath(toplevel) if toplevel else cwd
    if os.path.isfile(os.path.join(ctx.dev_root, LEGACY_PROTECT_FILE)):
        die(f"{LEGACY_PROTECT_FILE} is replaced by {SANDBOX_FILE}: list under an [allowed-git] line "
            "what Claude may see, everything else is hidden")
    ctx.sandbox_file = read_sandbox_file(ctx.dev_root)
    if not toplevel and ctx.sandbox_file.exists:
        die(f"{SANDBOX_FILE} protects a git repository, and this directory is not one: "
            "run git init, or remove the file")
    # A repository that was ever protected stays protected until the
    # developer confirms otherwise (confirm_visible).
    was_protected = bool(toplevel) and ever_protected(ctx.git_dev)
    protected = ctx.sandbox_file.exists or was_protected

    check_deps()
    ctx.srt = find_srt(root) or die(f'srt not found: run "npm ci" in {root}')
    check_seccomp(ctx.srt)
    find_protected_elsewhere(ctx, protected)
    if protected:
        return run_protected(ctx, cwd, args, was_protected)
    return run_in_place(ctx, cwd, args)
