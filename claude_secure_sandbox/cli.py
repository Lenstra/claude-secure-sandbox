"""Entry point of the `claude` wrapper.

In a repository with a .claudeprotected file at its root, Claude Code runs
inside a sandbox (srt) on a filtered copy of the repository, without the
protected code. Anywhere else, the real claude runs unchanged.
"""

from __future__ import annotations

import os
import platform
import signal
import subprocess
from typing import List, NoReturn

from . import filtered_copy, sandbox, session
from .common import (PROTECT_FILE, Lock, check_deps, die, find_real_claude, find_srt, log,
                     load_config, path_within, realpath, state_root, volume_roots, warn)
from .context import Context
from .protect import (find_nested_protect_files, is_protected_path, keep_removed_protections,
                      protect_hash, read_protect_file)

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


def run_directory_mode(ctx: Context, cwd: str, args: List[str]) -> int:
    """No .claudeprotected here: Claude works directly in the directory (the
    repository root, or the current directory outside a repository), in the
    sandbox, where nothing else of the user's is readable."""
    ctx.directory_mode = True
    ctx.setup_paths()
    ctx.work = ctx.dev_root
    ctx.copy_git = os.path.join(ctx.dev_root, ".git")
    signal.signal(signal.SIGTERM, _sigterm)
    try:
        sandbox.sync_claude_config(ctx)
        sandbox.sync_claude_credentials(ctx)
        sandbox.write_gitconfig(ctx)
        sandbox.write_tool_links(ctx)
        sandbox.write_srt_settings(ctx)
        sandbox.write_system_prompt(ctx)
        git_files = sandbox.git_files_present(ctx)
        status = sandbox.run_sandboxed(ctx, cwd, args)
        sandbox.neutralize_git_files(ctx, git_files)
        return status
    except KeyboardInterrupt:
        return 130


def main(root: str, args: List[str]) -> int:
    config = load_config()
    ctx = Context(root, config, state_root(config))
    ctx.real_claude = find_real_claude(config, root) or die(
        "claude binary not found on the PATH (or set claude_bin)")

    if args and args[0] in SESSIONLESS:
        exec_real(ctx, args)

    cwd = realpath(os.getcwd())

    # Already inside a sandbox, or inside a copy: nothing more to filter.
    if os.environ.get("CLAUDE_SECURE_SANDBOX_ACTIVE") or path_within(ctx.state_root, cwd):
        exec_real(ctx, args)

    toplevel = _git_toplevel(cwd)
    ctx.dev_root = realpath(toplevel) if toplevel else cwd

    # A directory that holds the home directory (or other volumes) is readable
    # in full, but not writable: programs outside the sandbox run what lives
    # there (shell startup files, LaunchAgents, tool configurations).
    home = os.environ["HOME"]
    ctx.read_only_root = ctx.dev_root == "/" or any(
        path_within(ctx.dev_root, s) for s in [home, os.path.dirname(home)] + volume_roots())
    if ctx.dev_root == "/":
        log("looking for protected repositories on the system disk (a few seconds)")

    # From /, other disks are not scanned; they stay unreadable.
    nested = [f for f in find_nested_protect_files(ctx.dev_root, one_file_system=ctx.dev_root == "/")
              if not path_within(ctx.state_root, f)]
    if nested:
        die(f"protected code in {os.path.dirname(nested[0])}: run claude from that directory")

    check_deps()
    ctx.srt = find_srt(config, root) or die(f'srt not found: run "npm ci" in {root}')

    if not toplevel or not os.path.isfile(os.path.join(ctx.dev_root, PROTECT_FILE)):
        return run_directory_mode(ctx, cwd, args)

    # --- protected repository ----------------------------------------------------

    git_dir = realpath(ctx.git_dev.out("rev-parse", "--absolute-git-dir"))
    common_dir = realpath(ctx.git_dev.out("rev-parse", "--path-format=absolute", "--git-common-dir"))
    if git_dir != common_dir:
        die("git worktrees are not supported (v1); run claude from the main repository")
    if os.path.isfile(os.path.join(ctx.dev_root, ".gitmodules")):
        warn("submodules are not supported (v1): their content will be missing from the copy")
    ctx.dev_git_common = common_dir

    current = read_protect_file(ctx.dev_root, ctx.git_dev)
    ctx.protected = keep_removed_protections(ctx.git_dev, common_dir, current)
    ctx.protect_hash = protect_hash(ctx.protected)

    rel = os.path.relpath(cwd, ctx.dev_root)
    rel = "" if rel == "." else rel
    if rel and is_protected_path(ctx.protected, rel):
        die(f"{rel} is protected by {PROTECT_FILE}: run claude from a directory that is not protected")

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

        sandbox.sync_claude_config(ctx)
        sandbox.sync_claude_credentials(ctx)
        sandbox.copy_dependency_dirs(ctx)
        sandbox.write_gitconfig(ctx)
        sandbox.write_tool_links(ctx)
        sandbox.write_srt_settings(ctx)
        sandbox.write_system_prompt(ctx)

        launch_dir = ctx.work
        if rel and os.path.isdir(os.path.join(ctx.work, rel)):
            launch_dir = os.path.join(ctx.work, rel)

        before = session.branch_tips(ctx)
        git_files = sandbox.git_files_present(ctx)
        status = sandbox.run_sandboxed(ctx, launch_dir, args)
        sandbox.neutralize_git_files(ctx, git_files)
        session.end_of_session(ctx, before)
        return status
    except KeyboardInterrupt:
        return 130
    finally:
        ctx.lock.release()
