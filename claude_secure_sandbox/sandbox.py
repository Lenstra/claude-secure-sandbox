"""The sandbox: srt settings, git configuration, system prompt, launch."""

from __future__ import annotations

import glob
import ipaddress
import json
import os
import platform
import shlex
import shutil
import signal
import subprocess
from typing import List

from .common import (NAME, PLACEHOLDER_TEXT, PROTECT_FILE, die, ensure_real_dir, log, overlaps, path_within,
                     read_text, realpath, stamp, warn, write_text)
from .context import Context
from .filtered_copy import claude_project_dir
from .claude_config import PRIVATE_CLAUDE_ITEMS, claude_settings
from .protect import is_protected_path, normalize
from .screen import confirm_session, protected_code_exists

DEFAULT_DOMAINS = [
    "api.anthropic.com", "claude.ai", "platform.claude.com", "console.anthropic.com",
    "statsig.anthropic.com", "mcp-proxy.anthropic.com",
]

# Git hosts that are always denied, on top of the hosts of the developer's remotes.
GIT_HOSTS = [
    "github.com", "*.github.com", "*.githubusercontent.com", "gitlab.com", "*.gitlab.com",
    "bitbucket.org", "*.bitbucket.org", "dev.azure.com", "*.dev.azure.com",
    "*.visualstudio.com", "codeberg.org", "sourceforge.net", "*.sourceforge.net",
]

# Where credentials live, relative to the home directory. When protected code
# exists on the computer, they are hidden: they open the git servers that host
# it.
CREDENTIALS = [".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker/config.json", ".netrc",
               ".git-credentials", ".npmrc", ".pypirc", ".cargo/credentials.toml", ".config/gh",
               ".config/hub", ".config/gcloud", ".password-store", "Library/Keychains",
               ".local/share/keyrings"]
TEMP_DIRS = ["/tmp", "/private/tmp", "/var/tmp"]

# Paths of the copy's .git that Claude must not write: the wrapper's git or
# the developer's would read configuration, hooks or objects from there.
LOCKED_GIT_DIRS = ["hooks", "info", "objects/info", "modules", "worktrees"]
LOCKED_GIT_FILES = ["config", "config.worktree", "commondir", "shallow"]
LOCKED_GIT_PATHS = LOCKED_GIT_FILES + LOCKED_GIT_DIRS


def url_host(url: str) -> str:
    """Host of a git URL (https://h/x, ssh://u@h:22/x, u@h:x); empty for a local path."""
    if "://" in url:
        host = url.split("://", 1)[1].split("/", 1)[0].rsplit("@", 1)[-1]
        host = host.split("]", 1)[0] + "]" if host.startswith("[") else host.split(":", 1)[0]
    elif url.startswith(("/", "./", "../", "file:")):
        return ""
    elif ":" in url:
        host = url.split(":", 1)[0].rsplit("@", 1)[-1]
    else:
        return ""
    return host.lower()


def denied_domains(ctx: Context) -> List[str]:
    hosts = set(GIT_HOSTS)
    remotes = ctx.git_dev.value("config", "--get-regexp", r"^remote\..*\.(url|pushurl)$")
    for line in remotes.splitlines():
        parts = line.split(" ", 1)
        if len(parts) == 2:
            host = url_host(parts[1])
            if host:
                hosts.add(host)
    return sorted(hosts)


def allowed_domains(ctx: Context, denied: List[str]) -> List[str]:
    allowed = set()
    for d in DEFAULT_DOMAINS + ctx.config.paths("network", "allowed_domains"):
        d = d.lower()
        if d == "*" or d.startswith("*:"):
            die('allowed_domains cannot contain "*"')
        if d in denied:
            die(f"git host not allowed in allowed_domains: {d}")
        if local_destination(d):
            die(f"this computer is not allowed in allowed_domains: {d}")
        allowed.add(d)
    return sorted(allowed)


def local_destination(entry: str) -> bool:
    """An allowed_domains entry that designates this computer: localhost, a
    loopback, unspecified or link-local address. A service listening there
    (an MCP server, an adb or Docker daemon, a dev server) runs outside the
    sandbox: reached through srt's proxy, it would read for Claude what the
    sandbox hides. srt already refuses a name that resolves there."""
    host = entry
    if host.startswith("["):
        host = host[1:host.find("]")]
    elif host.count(":") == 1:
        host = host.split(":")[0]
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_unspecified or address.is_link_local


def copy_dependency_dirs(ctx: Context) -> None:
    """Copies the dependency directories listed in the configuration
    (node_modules, etc.) from the developer's repository, when they are
    missing from the copy."""
    for d in ctx.config.paths("copy_dirs"):
        try:
            d = normalize(d) or ""
        except ValueError:
            warn(f"copy_dirs: invalid path ignored: {d}")
            continue
        if not d:
            continue
        if is_protected_path(ctx.protected, d):
            warn(f"copy_dirs: {d} is protected, ignored")
            continue
        if any(p.startswith(d + "/") for p in ctx.protected):
            warn(f"copy_dirs: {d} contains a protected path, ignored")
            continue
        src = os.path.join(ctx.dev_root, d)
        dst = os.path.join(ctx.work, d)
        if not os.path.isdir(src) or os.path.lexists(dst):
            continue
        # The closest existing parent must be inside the copy (no link planted
        # by Claude that would make the wrapper write elsewhere).
        parent = os.path.dirname(dst)
        while not os.path.lexists(parent):
            parent = os.path.dirname(parent)
        if not path_within(ctx.work, realpath(parent)):
            warn(f"copy_dirs: {d} leads outside the copy, ignored")
            continue
        log(f"copying {d} from the developer's repository")
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copytree(src, dst, symlinks=True)
        except (OSError, shutil.Error):
            warn(f"incomplete copy of {d}")


def other_sessions(ctx: Context) -> List[str]:
    """Sessions of other repositories: unreadable from this sandbox."""
    mine = claude_project_dir(ctx)
    result = []
    for d in sorted(glob.glob(os.path.join(ctx.claude_home, "projects", "*"))):
        if not os.path.isdir(d) or d == mine:
            continue
        result.append(d)
        for transcript in glob.glob(os.path.join(d, "*.jsonl")):
            sid = os.path.basename(transcript)[:-len(".jsonl")]
            history = os.path.join(ctx.claude_home, "file-history", sid)
            if os.path.lexists(history):
                result.append(history)
    return result


def _gitconfig_value(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


# Files srt always write-protects; on Linux it covers each absent one with
# /dev/null in every writable directory for the session.
SRT_PROTECTED_FILES = [".gitconfig", ".gitmodules", ".bashrc", ".bash_profile", ".zshrc", ".zprofile",
                       ".profile", ".ripgreprc", ".mcp.json", ".vscode", ".idea", ".claude/commands",
                       ".claude/agents"]


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


def _user_temp_dirs() -> List[str]:
    """macOS: the per-user temp and cache directories under /var/folders."""
    if platform.system() != "Darwin":
        return []
    dirs = []
    for name in ("DARWIN_USER_TEMP_DIR", "DARWIN_USER_CACHE_DIR"):
        proc = subprocess.run(["getconf", name], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              encoding="utf-8")
        if proc.returncode == 0 and proc.stdout.strip():
            dirs.append(realpath(proc.stdout.strip()))
    return dirs


def write_srt_settings(ctx: Context) -> None:
    home = os.environ["HOME"]
    denied = denied_domains(ctx)
    allowed = allowed_domains(ctx, denied)
    secrets = [ctx.state_root, f"{home}/.ssh", f"{home}/.claude", f"{home}/.claude.json"]
    if not ctx.directory_mode:
        secrets += [ctx.dev_root, ctx.dev_git_common]

    # Claude Code installation: the local install (~/.claude/local) only holds
    # the program; any other location inside a sensitive area is refused.
    claude_dir = os.path.dirname(realpath(ctx.real_claude))
    if path_within(f"{home}/.claude/local", claude_dir):
        claude_dir = f"{home}/.claude/local"
    else:
        for secret in secrets:
            if overlaps(secret, claude_dir):
                die(f"the claude binary ({claude_dir}) is inside a sensitive area ({secret}): set claude_bin")

    # Extra writable paths from the configuration: not inside a sensitive
    # area, not above one, and not through a link (srt follows links).
    if ctx.config.get("filesystem", "allow_read") is not None:
        warn("configuration: filesystem.allow_read is no longer used, everything outside the denylist is readable")
    extra_write = ctx.config.paths("filesystem", "allow_write")
    for p in extra_write:
        real = realpath(p)
        if p == "/" or real == "/":
            die("the configuration opens the root directory /")
        for secret in secrets:
            if overlaps(p, secret) or overlaps(real, secret):
                die(f"the configuration opens {p}, which conflicts with {secret}")

    # Everything is readable, except a denylist:
    # - in protected mode, the developer's repository (Claude works in the
    #   filtered copy), and every other protected repository, entirely;
    # - the wrapper's state, except what the session needs (its Claude
    #   configuration and its own directory: run/, tmp/, the copy);
    # - old Claude transcripts, which may hold protected code;
    # - when protected code exists on the computer, its other ways in: the
    #   credentials that open its git servers, and the temp directories that
    #   may hold copies of it.
    deny_read = [ctx.state, ctx.archive] + other_sessions(ctx) + ctx.protected_elsewhere
    deny_read += [os.path.join(ctx.state_root, e) for e in sorted(os.listdir(ctx.state_root))
                  if os.path.join(ctx.state_root, e) not in (ctx.claude_home, ctx.repo_dir)]
    deny_read += [os.path.join(home, ".claude", item) for item in PRIVATE_CLAUDE_ITEMS]
    if not ctx.directory_mode:
        deny_read += [ctx.dev_root, ctx.dev_git_common]
    if protected_code_exists(ctx):
        deny_read += [os.path.join(home, c) for c in CREDENTIALS] + TEMP_DIRS + _user_temp_dirs()
    deny_read = [d for d in deny_read if os.path.lexists(d)]
    # What the session needs is re-allowed only where a deny covers it (when
    # it lives in a temp directory, say): on Linux, a read-only bind of a
    # directory would cover the writable mounts inside it.
    needed = [ctx.work, ctx.tmp, ctx.run_dir, ctx.claude_home, claude_dir, os.path.join(ctx.root, "libexec"),
              os.path.join(os.path.dirname(os.path.dirname(realpath(ctx.srt))), "vendor")] + ctx.config_read
    allow_read = [p for p in needed if any(path_within(d, p) and d != p for d in deny_read)]
    allow_write = ([] if ctx.read_only_root else [ctx.work]) + [ctx.tmp, ctx.claude_home] + extra_write
    # Protected repositories under the session's directory: not writable either.
    deny_write_protected = [r for r in ctx.protected_elsewhere if path_within(ctx.work, r)]
    deny_write = [] if ctx.read_only_root else [os.path.join(ctx.copy_git, p) for p in LOCKED_GIT_PATHS]
    deny_write.append(os.path.join(ctx.work, PROTECT_FILE))
    deny_write += [os.path.join(ctx.work, p) for p in ctx.protected] + deny_write_protected
    if platform.system() == "Linux":
        # bubblewrap denies a path by mounting over it: an absent path would
        # appear, on the host too, as an empty file during the session (git
        # then fails on .git/commondir). The locked directories are created
        # empty beforehand; the locked files that are absent are checked after
        # the session instead (neutralize_git_files). A mount point cannot be
        # renamed or removed, so writable directories need no anchor.
        # Protected paths, absent from the copy, are created as empty
        # directories for the same reason (git ignores empty directories).
        if not ctx.read_only_root and os.path.isdir(ctx.copy_git) and not os.path.islink(ctx.copy_git):
            targets = [os.path.join(ctx.copy_git, d) for d in LOCKED_GIT_DIRS]
            targets += [os.path.join(ctx.work, p) for p in ctx.protected]
            for d in targets:
                if path_within(ctx.work, realpath(os.path.dirname(d))):
                    try:
                        os.makedirs(d, exist_ok=True)
                    except OSError:
                        pass
        deny_write = [p for p in deny_write if os.path.lexists(p)]
    else:
        # Anchor in every writable directory: srt then forbids deleting or
        # renaming the directory, so it cannot be replaced by a link for the
        # next session.
        deny_write += [os.path.join(p, f".{NAME}-anchor") for p in allow_write]

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
            "allowedDomains": allowed,
            "deniedDomains": denied,
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


def git_files_present(ctx: Context) -> List[str]:
    return [f for f in LOCKED_GIT_FILES if os.path.lexists(os.path.join(ctx.copy_git, f))]


def neutralize_git_files(ctx: Context, before: List[str]) -> None:
    """Linux: a locked .git file that was absent at launch could not be denied
    (see write_srt_settings). One that appeared during the session is renamed,
    so that no git outside the sandbox reads it (.git/commondir would point git
    at a configuration Claude wrote)."""
    if platform.system() != "Linux":
        return
    for name in LOCKED_GIT_FILES:
        path = os.path.join(ctx.copy_git, name)
        if name not in before and os.path.lexists(path):
            aside = f"{path}.{NAME}-{stamp()}"
            os.rename(path, aside)
            warn(f"{path} was created during the session: renamed to {aside}")


def write_system_prompt(ctx: Context) -> None:
    name = "directory-mode.md" if ctx.directory_mode else "protected-mode.md"
    template = read_text(os.path.join(ctx.root, "share", "prompts", name))
    access = ("You work in this directory, which holds the developer's home directory. You can read it, "
              "but not write it: write files in the temporary directory ($TMPDIR), and give the developer "
              "the changes or commands to apply them:" if ctx.read_only_root
              else "You work directly in this directory, and can read and write it:")
    template = (template.replace("@PLACEHOLDER@", PLACEHOLDER_TEXT).replace("@PROTECT_FILE@", PROTECT_FILE)
                .replace("@ROOT@", ctx.work).replace("@ACCESS@", access))
    paths = "\n".join(f"- `{p}`" for p in ctx.protected)
    lines = [paths if line == "@PROTECTED_PATHS@" else line for line in template.split("\n")]
    write_text(os.path.join(ctx.run_dir, "system-prompt.md"), "\n".join(lines))


def run_sandboxed(ctx: Context, cwd: str, args: List[str]) -> int:
    """Runs Claude Code inside srt. Returns claude's exit status."""
    env = dict(os.environ)
    env.update({
        "CLAUDE_CONFIG_DIR": ctx.claude_home,
        "GIT_CONFIG_GLOBAL": os.path.join(ctx.run_dir, "gitconfig"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "PATH": ":".join([os.path.join(ctx.root, "libexec"), os.path.join(ctx.run_dir, "bin"), env.get("PATH", "")]),
        # Claude Code's own temp root, /tmp by default, which is unreadable.
        "CLAUDE_CODE_TMPDIR": ctx.tmp,
        "DISABLE_AUTOUPDATER": "1",
        # Plugins and skills synced from claude.ai: Claude Code turns their
        # sync off with a configuration directory of its own, unless asked.
        "CLAUDE_CODE_SYNC_PLUGINS": "1",
        "CLAUDE_SECURE_SANDBOX_ACTIVE": "1",
    })
    for var in ("SSH_AUTH_SOCK", "GIT_ASKPASS", "SSH_ASKPASS"):
        env.pop(var, None)
    # srt forces TMPDIR=/tmp/claude, shared by every sandbox: keep the
    # repository's own tmp/ instead.
    cmd = [ctx.srt, "--settings", os.path.join(ctx.run_dir, "srt-settings.json"), "--",
           # The real path: links outside the readable areas do not resolve.
           "/usr/bin/env", f"TMPDIR={ctx.tmp}/", realpath(ctx.real_claude),
           "--settings", claude_settings(ctx),
           "--append-system-prompt-file", os.path.join(ctx.run_dir, "system-prompt.md"), *args]
    terminal = own_terminal()
    if terminal:
        node = shutil.which("node") or die("node not found")
        cmd = [node, "--import", os.path.join(ctx.root, "libexec", "srt-own-terminal.mjs"), *cmd]
        env["CLAUDE_SECURE_SANDBOX_TTY"] = terminal
    # Ctrl-C goes to Claude; the wrapper must survive to print the end of
    # session summary. A handler (unlike an ignored signal) is reset in child
    # processes.
    if not confirm_session(ctx):
        log("cancelled")
        return 130
    signal.signal(signal.SIGINT, lambda *_: None)
    status = subprocess.call(cmd, cwd=cwd, env=env)
    return 128 - status if status < 0 else status
