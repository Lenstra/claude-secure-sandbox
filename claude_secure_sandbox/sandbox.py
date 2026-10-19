"""The sandbox: srt settings, git configuration, system prompt, launch."""

from __future__ import annotations

import json
import os
import platform
import shlex
import shutil
import signal
import subprocess
import tempfile
from typing import List, Tuple

from .access import (CREDENTIALS, TEMP_DIRS, USER_AREAS, allowed_domains, denied_domains, path_tools,
                     user_temp_dirs)
from .claude_config import PRIVATE_CLAUDE_ITEMS, claude_settings
from .common import (NAME, PLACEHOLDER_TEXT, SANDBOX_FILE, die, ensure_real_dir, log, overlaps, path_within,
                     read_text, realpath, stamp, volume_roots, warn, write_text)
from .context import Context
from .screen import confirm_session

# Paths of the copy's .git that Claude must not write: the wrapper's git or
# the developer's would read configuration, hooks or objects from there.
LOCKED_GIT_DIRS = ["hooks", "info", "objects/info", "modules", "worktrees"]
LOCKED_GIT_FILES = ["config", "config.worktree", "commondir", "shallow"]
LOCKED_GIT_PATHS = LOCKED_GIT_FILES + LOCKED_GIT_DIRS
# Claude Code's settings in the working directory: permissions and hooks
# that any Claude Code started there applies, one outside the sandbox too.
# srt already locks .claude/commands and .claude/agents.
LOCKED_CLAUDE_FILES = [".claude/settings.json", ".claude/settings.local.json"]

# Files srt always write-protects; on Linux it covers each absent one with
# /dev/null in every writable directory for the session.
SRT_PROTECTED_FILES = [".gitconfig", ".gitmodules", ".bashrc", ".bash_profile", ".zshrc", ".zprofile",
                       ".profile", ".ripgreprc", ".mcp.json", ".vscode", ".idea", ".claude/commands",
                       ".claude/agents"]


# --- files of the session ------------------------------------------------------


def _gitconfig_value(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_gitconfig(ctx: Context) -> None:
    name = ctx.git_dev.value("config", "--get", "user.name")
    email = ctx.git_dev.value("config", "--get", "user.email")
    # The default excludes file lives under $XDG_CONFIG_HOME or ~/.config,
    # unreadable. On Linux it lists srt's /dev/null stand-ins, which git would
    # otherwise show as untracked files that cannot be added.
    excludes = "/dev/null"
    if platform.system() == "Linux":
        excludes = os.path.join(ctx.run_dir, "gitignore")
        write_text(excludes, "".join(f"/{f}\n" for f in SRT_PROTECTED_FILES))
    write_text(os.path.join(ctx.run_dir, "gitconfig"),
               f"[user]\n\tname = {_gitconfig_value(name)}\n\temail = {_gitconfig_value(email)}\n"
               "[init]\n\tdefaultBranch = main\n"
               f"[core]\n\texcludesFile = {_gitconfig_value(excludes)}\n\tattributesFile = /dev/null\n"
               # No signing key in the sandbox: the developer signs when integrating.
               "[commit]\n\tgpgsign = false\n[tag]\n\tgpgsign = false\n"
               "[advice]\n\tdetachedHead = false\n")


def write_tool_links(ctx: Context) -> None:
    """macOS: git on the PATH is usually the /usr/bin shim, which looks the
    real binary up through xcrun and its cache, unwritable from the sandbox
    (every call would then run xcodebuild). run/bin/git calls the real one."""
    bin_dir = os.path.join(ctx.run_dir, "bin")
    ensure_real_dir(bin_dir)
    target = os.path.join(bin_dir, "git")
    if os.path.lexists(target):
        os.unlink(target)
    if platform.system() != "Darwin" or realpath(shutil.which("git") or "") != "/usr/bin/git":
        return
    proc = subprocess.run(["xcrun", "-f", "git"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          encoding="utf-8")
    real = proc.stdout.strip() if proc.returncode == 0 else ""
    if real and os.access(real, os.X_OK) and real != "/usr/bin/git":
        write_text(target, f"#!/bin/sh\nexec {shlex.quote(real)} \"$@\"\n")
        os.chmod(target, 0o755)


def write_system_prompt(ctx: Context) -> None:
    name = "protected-repository.md" if ctx.protected else "directory.md"
    template = read_text(os.path.join(ctx.root, "share", "prompts", name))
    if ctx.home_session:
        access = ("You work in this directory, which holds the developer's home directory. You can read it, "
                  "but not write it: write files in the temporary directory ($TMPDIR), and give the developer "
                  "the changes or commands to apply them:")
        read = ("You can read the rest of the computer, except, by design: protected repositories, old Claude "
                "conversations, and, when protected repositories exist, credentials (SSH keys, keychain, tokens) "
                "and temp directories.")
    else:
        access = "You work directly in this directory, and can read and write it:"
        read = ("Outside this directory, you can read the system and the developer's tools (the directories on "
                "the PATH), and the paths the developer opened in `.claudesandbox`. The developer's other files, "
                "other disks and temp directories are hidden, by design.")
    template = (template.replace("@PLACEHOLDER@", PLACEHOLDER_TEXT).replace("@SANDBOX_FILE@", SANDBOX_FILE)
                .replace("@ROOT@", ctx.work).replace("@ACCESS@", access).replace("@READ@", read))
    hidden = "\n".join(f"- `{p}`" for p in ctx.hidden)
    lines = [hidden if line == "@HIDDEN_PATHS@" else line for line in template.split("\n")]
    write_text(os.path.join(ctx.run_dir, "system-prompt.md"), "\n".join(lines))


def make_srt_tmp(ctx: Context) -> None:
    """srt's own temp directory: on Linux it creates there the proxy sockets
    it binds into the sandbox, which must stay readable. The default temp
    directory is hidden, and a socket path is limited to 108 bytes: a short
    directory in /tmp, private to the session, removed after it."""
    try:
        ctx.srt_tmp = realpath(tempfile.mkdtemp(prefix="csb-", dir="/tmp"))
    except OSError:
        ctx.srt_tmp = os.path.join(ctx.run_dir, "srt")
        ensure_real_dir(ctx.srt_tmp)


# --- srt settings --------------------------------------------------------------


def _claude_install_dir(ctx: Context) -> str:
    """Claude Code's installation, readable by the session. The local install
    (~/.claude/local) only holds the program; any other location inside a
    sensitive area is refused."""
    home = os.environ["HOME"]
    claude_dir = os.path.dirname(realpath(ctx.real_claude))
    if path_within(f"{home}/.claude/local", claude_dir):
        return f"{home}/.claude/local"
    sensitive = [ctx.state_root, f"{home}/.ssh", f"{home}/.claude", f"{home}/.claude.json"]
    if ctx.protected:
        sensitive += [ctx.dev_root, ctx.dev_git_common]
    for path in sensitive:
        if overlaps(path, claude_dir):
            die(f"the claude binary ({claude_dir}) is inside a sensitive area ({path})")
    return claude_dir


def _read_rules(ctx: Context) -> Tuple[List[str], List[str]]:
    """(denyRead, allowRead). Where users' files live (the home directories,
    the other disks, the temp directories) nothing is readable but what the
    session needs; the system and the tools installed elsewhere are. Inside,
    these stay hidden even where an allowance covers them (in srt, a more
    specific deny beats an allow): the developer's repository when it is
    protected, every other protected repository, the wrapper's state except
    the session's own (other repositories' sessions included), old Claude
    transcripts, credentials.

    A session in the home directory works in it, so the user areas stay
    readable; credentials and temp directories are then hidden only when
    protected code exists on the computer."""
    home = os.environ["HOME"]
    deny = [ctx.state, ctx.archive] + ctx.protected_elsewhere
    deny += [os.path.join(ctx.state_root, e) for e in sorted(os.listdir(ctx.state_root))
             if os.path.join(ctx.state_root, e) != ctx.repo_dir]
    deny += [os.path.join(home, ".claude", item) for item in PRIVATE_CLAUDE_ITEMS]
    # Claude Code's state (account, MCP servers and their tokens): the
    # session has its own copy of the parts it needs.
    deny.append(os.path.join(home, ".claude.json"))
    if ctx.protected:
        deny += [ctx.dev_root, ctx.dev_git_common]
    if not ctx.home_session or ctx.protected_code_exists:
        deny += [os.path.join(home, c) for c in CREDENTIALS] + TEMP_DIRS + user_temp_dirs()
    if not ctx.home_session:
        deny += [home] + USER_AREAS.get(platform.system(), []) + volume_roots()
    deny = [d for d in deny if os.path.lexists(d)]
    needed = [ctx.work, ctx.tmp, ctx.run_dir, ctx.claude_home, ctx.srt_tmp, _claude_install_dir(ctx),
              os.path.join(ctx.root, "libexec"),
              os.path.join(os.path.dirname(os.path.dirname(realpath(ctx.srt))), "vendor")]
    needed += ctx.config_read + ctx.granted_paths + path_tools(ctx)
    # Listed only where a deny covers it: on Linux, a read-only bind of a
    # directory would cover the writable mounts inside it.
    allow = [p for p in needed if any(path_within(d, p) and d != p for d in deny)]
    return deny, allow


def _write_rules(ctx: Context) -> Tuple[List[str], List[str]]:
    """(allowWrite, denyWrite). The working directory (unless it holds the
    home directory), the session's temp directory and its Claude
    configuration; inside, never the .git configuration and hooks, Claude
    Code's settings (the directory's and the developer's copy in
    claude-home), .claudesandbox, the hidden paths, nor a protected
    repository."""
    allow = ([] if ctx.home_session else [ctx.work]) + [ctx.tmp, ctx.claude_home]
    deny = [] if ctx.home_session else [os.path.join(ctx.copy_git, p) for p in LOCKED_GIT_PATHS]
    deny += [os.path.join(ctx.work, f) for f in LOCKED_CLAUDE_FILES]
    deny.append(os.path.join(ctx.claude_home, "settings.json"))
    deny.append(os.path.join(ctx.work, SANDBOX_FILE))
    deny += [os.path.join(ctx.work, p) for p in ctx.hidden]
    deny += [r for r in ctx.protected_elsewhere if path_within(ctx.work, r)]
    if platform.system() == "Linux":
        # bubblewrap denies a path by mounting over it: an absent path would
        # appear, on the host too, as an empty file during the session (git
        # then fails on .git/commondir). The locked directories and the hidden
        # directories are created empty beforehand (git ignores empty
        # directories); the locked files that are absent are checked after the
        # session instead (set_aside_new_locked_files). A hidden file cannot be
        # created (git would see a directory in its place): it stays unlocked,
        # and git refuses to add it. A mount point cannot be renamed or
        # removed, so writable directories need no anchor.
        if not ctx.home_session and os.path.isdir(ctx.copy_git) and not os.path.islink(ctx.copy_git):
            targets = [os.path.join(ctx.copy_git, d) for d in LOCKED_GIT_DIRS]
            targets += [os.path.join(ctx.work, p) for p in ctx.hidden_dirs]
            for d in targets:
                if path_within(ctx.work, realpath(os.path.dirname(d))):
                    try:
                        os.makedirs(d, exist_ok=True)
                    except OSError:
                        pass
        deny = [p for p in deny if os.path.lexists(p)]
    else:
        # Anchor in every writable directory: srt then forbids deleting or
        # renaming the directory, so it cannot be replaced by a link for the
        # next session.
        deny += [os.path.join(p, f".{NAME}-anchor") for p in allow]
    return allow, deny


def write_srt_settings(ctx: Context) -> None:
    deny_read, allow_read = _read_rules(ctx)
    allow_write, deny_write = _write_rules(ctx)

    def clean(paths):
        return sorted({p for p in paths if p})

    settings = {
        "filesystem": {
            "denyRead": clean(deny_read),
            "allowRead": clean(allow_read),
            "allowWrite": clean(allow_write),
            "denyWrite": clean(deny_write),
        },
        "network": {
            "allowedDomains": allowed_domains(ctx),
            "deniedDomains": denied_domains(ctx),
            "allowLocalBinding": False,
        },
        "allowAppleEvents": False,
        "enableWeakerNetworkIsolation": False,
    }
    if own_terminal():
        settings["allowPty"] = True
    write_text(os.path.join(ctx.run_dir, "srt-settings.json"), json.dumps(settings, indent=2) + "\n")


def own_terminal() -> str:
    """On macOS, the terminal Claude runs in, when there is one. Seatbelt
    denies terminal control (raw mode, which Claude's interface needs to read
    keys) unless srt's allowPty is set; libexec/srt-own-terminal.mjs then
    narrows it to this terminal."""
    if platform.system() != "Darwin" or not os.isatty(0):
        return ""
    return os.ttyname(0)


# --- locked files absent at launch, on Linux ----------------------------------


def locked_files(ctx: Context) -> List[str]:
    """Files of the working directory that Claude must not write, whether
    they exist or not."""
    if ctx.home_session:
        return []
    return ([os.path.join(ctx.copy_git, f) for f in LOCKED_GIT_FILES]
            + [os.path.join(ctx.work, f) for f in LOCKED_CLAUDE_FILES])


def present(paths: List[str]) -> List[str]:
    return [p for p in paths if os.path.lexists(p)]


def set_aside_new_locked_files(ctx: Context, before: List[str]) -> None:
    """Linux: a locked file absent at launch could not be denied (see
    _write_rules). One that appeared during the session is renamed before
    anything outside the sandbox reads it: .git/commondir would point git at
    a configuration Claude wrote, .claude/settings.json would give hooks to
    a Claude Code started there without the sandbox."""
    if platform.system() != "Linux":
        return
    for path in present(locked_files(ctx)):
        # Only through real directories: a .claude or .git that Claude turned
        # into a link would make the wrapper rename a file elsewhere.
        if path not in before and realpath(os.path.dirname(path)) == os.path.dirname(path):
            aside = f"{path}.{NAME}-{stamp()}"
            os.rename(path, aside)
            warn(f"{path} was created during the session: renamed to {aside}")


# --- launch --------------------------------------------------------------------


def _sandbox_env(ctx: Context) -> dict:
    """The environment of Claude Code in the sandbox."""
    env = dict(os.environ)
    # Without the wrapper: a claude started in the session is the real one,
    # already confined.
    wrapper = realpath(os.path.join(ctx.root, "bin"))
    path = [d for d in env.get("PATH", "").split(":") if realpath(d or ".") != wrapper]
    env.update({
        "CLAUDE_CONFIG_DIR": ctx.claude_home,
        "GIT_CONFIG_GLOBAL": os.path.join(ctx.run_dir, "gitconfig"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "PATH": ":".join([os.path.join(ctx.root, "libexec"), os.path.join(ctx.run_dir, "bin"), *path]),
        # Claude Code's own temp root, /tmp by default, which is unreadable.
        "CLAUDE_CODE_TMPDIR": ctx.tmp,
        "DISABLE_AUTOUPDATER": "1",
        # Plugins and skills synced from claude.ai: Claude Code turns their
        # sync off with a configuration directory of its own, unless asked.
        "CLAUDE_CODE_SYNC_PLUGINS": "1",
        # For scripts that want to know; the wrapper itself never trusts it.
        "CLAUDE_SECURE_SANDBOX_ACTIVE": "1",
    })
    # Package manager caches, in the home directory by default, which is
    # neither readable nor writable: per session instead.
    cache = os.path.join(ctx.tmp, ".cache")
    env.update({"XDG_CACHE_HOME": cache, "npm_config_cache": os.path.join(cache, "npm"),
                "PIP_CACHE_DIR": os.path.join(cache, "pip"), "GOCACHE": os.path.join(cache, "go-build"),
                "GOMODCACHE": os.path.join(cache, "go-mod")})
    # Java's temp directory is /tmp by default, unwritable on macOS.
    env["JAVA_TOOL_OPTIONS"] = " ".join(filter(None, [env.get("JAVA_TOOL_OPTIONS"),
                                                      f"-Djava.io.tmpdir={ctx.tmp}"]))
    for var in ("SSH_AUTH_SOCK", "GIT_ASKPASS", "SSH_ASKPASS"):
        env.pop(var, None)
    return env


def run_sandboxed(ctx: Context, cwd: str, args: List[str]) -> int:
    """Runs Claude Code inside srt. Returns claude's exit status."""
    env = _sandbox_env(ctx)
    cmd = [ctx.srt, "--settings", os.path.join(ctx.run_dir, "srt-settings.json"), "--",
           # srt sets TMPDIR to /tmp/claude, shared by every sandbox: the
           # session's tmp/ instead. The real path of claude: links outside the
           # readable areas do not resolve.
           # srt runs in the working directory, where its own protections
           # (.vscode, .mcp.json, .claude/commands...) apply; claude starts
           # in the developer's subdirectory.
           "/bin/sh", "-c", 'cd "$1" && shift && exec "$@"', "sh", cwd,
           "/usr/bin/env", f"TMPDIR={ctx.tmp}/", realpath(ctx.real_claude),
           "--settings", claude_settings(ctx),
           "--append-system-prompt-file", os.path.join(ctx.run_dir, "system-prompt.md"), *args]
    terminal = own_terminal()
    if terminal:
        node = shutil.which("node") or die("node not found")
        cmd = [node, "--import", os.path.join(ctx.root, "libexec", "srt-own-terminal.mjs"), *cmd]
        env["CLAUDE_SECURE_SANDBOX_TTY"] = terminal
    # srt's own temp directory (make_srt_tmp).
    env["TMPDIR"] = ctx.srt_tmp
    if not confirm_session(ctx):
        log("cancelled")
        return 130
    # Ctrl-C goes to Claude; the wrapper must survive to print the end of
    # session summary. A handler (unlike an ignored signal) is reset in child
    # processes.
    signal.signal(signal.SIGINT, lambda *_: None)
    status = subprocess.call(cmd, cwd=ctx.work, env=env)
    return 128 - status if status < 0 else status
