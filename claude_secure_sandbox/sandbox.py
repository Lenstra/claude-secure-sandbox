"""Separate Claude configuration, srt settings, system prompt, launch."""

from __future__ import annotations

import glob
import json
import os
import platform
import shlex
import shutil
import signal
import subprocess
import sys
from typing import List

from .common import (NAME, PLACEHOLDER_TEXT, PROTECT_FILE, die, ensure_real_dir, install_file, volume_roots,
                     log, path_within, read_text, realpath, regular_file, remove_entry, stamp, warn, write_text)
from .context import Context
from .filtered_copy import claude_project_dir
from .protect import is_protected_path, normalize

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

DEFAULT_SYNC = ["settings.json", "CLAUDE.md", "agents", "commands", "skills", "plugins"]

# Reading is denied everywhere ("/"), then re-allowed for the system, the
# tools and the session. Per operating system; Windows needs no list: srt runs
# the sandbox under a separate account that has no rights on the user's files.
SYSTEM_READ = {
    "Darwin": [
        "/usr", "/bin", "/sbin", "/System", "/dev", "/private/etc", "/private/var/select",
        "/private/var/db/timezone", "/Library/Apple", "/Library/Developer",
        "/Library/Preferences/com.apple.dt.Xcode.plist", "/opt/homebrew",
        # The /etc, /var and /tmp links themselves, so that paths through them
        # resolve; exact-match globs, which srt does not follow to their target.
        "/et[c]", "/va[r]", "/tm[p]",
    ],
    "Linux": [
        "/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32", "/etc", "/opt",
        # Other layouts, when present: Nix, Guix, snaps, Homebrew on Linux.
        "/nix/store", "/gnu/store", "/snap", "/home/linuxbrew/.linuxbrew",
    ],
}
# Data directories inside the system paths above (databases, services).
SYSTEM_DENY = {"Darwin": ["/opt/homebrew/var", "/usr/local/var"], "Linux": []}
# Tools whose installation is made readable wherever it lives.
TOOLS = ("git", "node", "python3", "rg")

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
        allowed.add(d)
    return sorted(allowed)


def _load_json(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _install_json(ctx: Context, data, dst: str) -> None:
    tmp = os.path.join(ctx.wtmp, os.path.basename(dst))
    write_text(tmp, json.dumps(data, indent=2) + "\n")
    install_file(tmp, dst)


# Items of ~/.claude that hold transcripts, history or credentials: never
# passed through to the sandbox, even when listed in "sync".
PRIVATE_CLAUDE_ITEMS = {"projects", "file-history", "history.jsonl", "todos", "shell-snapshots",
                        "sessions", "statsig", "debug", "ide", ".credentials.json"}


def sync_claude_config(ctx: Context) -> None:
    """Passes the configuration items of ~/.claude through to the sandbox's
    configuration directory, read only: each one is a link to the original,
    whose real path becomes readable (ctx.config_read). Nothing in ~/.claude
    is writable from the sandbox, and transcripts stay unreadable."""
    src = os.path.join(os.environ["HOME"], ".claude")
    dst = ctx.claude_home
    ensure_real_dir(dst)
    sensitive = [ctx.state_root, os.path.join(os.environ["HOME"], ".ssh")]
    if not ctx.directory_mode:
        sensitive += [ctx.dev_root, ctx.dev_git_common]
    for item in ctx.config.paths("sync") or DEFAULT_SYNC:
        parts = item.split("/")
        if not item or item.startswith("/") or ".." in parts or "." in parts or parts[0] in PRIVATE_CLAUDE_ITEMS:
            warn(f"sync item ignored: {item}")
            continue
        target = os.path.join(dst, item)
        # Never write through a link Claude planted in claude-home.
        if not path_within(dst, realpath(os.path.dirname(target))):
            warn(f"sync item ignored (link in {dst}): {item}")
            continue
        try:
            remove_entry(target)
            source = os.path.join(src, item)
            if not os.path.exists(source):
                continue
            real = realpath(source)
            if any(_conflicts(real, s) for s in sensitive if s):
                warn(f"sync item ignored (it leads to {real}): {item}")
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            os.symlink(source, target)
            ctx.config_read.append(real)
        except OSError:
            warn(f"cannot pass ~/.claude/{item} through")
    # MCP servers of ~/.claude.json, copied into the sandbox's .claude.json:
    # the global ones, and those of this repository (claude mcp add, local
    # scope), attached to the directory Claude works in. The rest of that file
    # is Claude Code's state (account, every project's paths and sessions),
    # rewritten constantly, so it is neither passed through nor copied. The
    # sandbox's .claude.json is only read when it is a regular file (not a
    # link Claude planted).
    home_json = os.path.join(os.environ["HOME"], ".claude.json")
    source = _load_json(home_json) if os.path.isfile(home_json) else None
    if isinstance(source, dict):
        target = os.path.join(dst, ".claude.json")
        current = _load_json(target) if regular_file(target) else None
        data = current if isinstance(current, dict) else {}
        data["mcpServers"] = source.get("mcpServers") or {}
        projects = source.get("projects") if isinstance(source.get("projects"), dict) else {}
        mine = projects.get(ctx.dev_root) if isinstance(projects.get(ctx.dev_root), dict) else {}
        if not isinstance(data.get("projects"), dict):
            data["projects"] = {}
        entry = data["projects"].get(ctx.work)
        entry = entry if isinstance(entry, dict) else {}
        entry["mcpServers"] = mine.get("mcpServers") or {}
        data["projects"][ctx.work] = entry
        _install_json(ctx, data, target)


# Claude Code's login on macOS, in the user's keychain.
KEYCHAIN_SERVICE = "Claude Code-credentials"


def _expires_at(data) -> int:
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    value = oauth.get("expiresAt") if isinstance(oauth, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _read_login():
    """Claude Code's login outside the sandbox: the macOS keychain entry, or
    ~/.claude/.credentials.json on Linux."""
    if platform.system() == "Darwin":
        user = os.environ.get("USER", "")
        proc = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", user, "-w"],
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, encoding="utf-8")
        if proc.returncode != 0:
            return None
        try:
            return json.loads(proc.stdout)
        except ValueError:
            return None
    source = os.path.join(os.environ["HOME"], ".claude", ".credentials.json")
    return _load_json(source) if regular_file(source) else None


def sync_claude_credentials(ctx: Context) -> None:
    """Claude Code keeps its login in the macOS keychain, which the sandbox
    cannot read: opening it would expose every credential it holds. On Linux
    it is in ~/.claude, unreadable too. The wrapper copies that login, and
    only it, into the sandbox's configuration, where Claude Code uses a file.
    A newer login already there (refreshed by Claude Code during a session)
    is kept. Nothing is ever copied back."""
    login = _read_login()
    if not _expires_at(login):
        return
    target = os.path.join(ctx.claude_home, ".credentials.json")
    current = _load_json(target) if regular_file(target) else None
    if _expires_at(current) >= _expires_at(login):
        return
    tmp = os.path.join(ctx.wtmp, ".credentials.json")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(login, f)
    install_file(tmp, target)


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


def _conflicts(a: str, b: str) -> bool:
    return path_within(a, b) or path_within(b, a)


def _path_read_paths(secrets: List[str]) -> List[str]:
    """Directories of the PATH, and the prefix of the .../bin ones, outside
    user data and temp areas: tools work wherever the distribution or the
    developer installed them (Nix, Linuxbrew, /usr/local...)."""
    home = os.environ["HOME"]
    excluded = [home, os.path.dirname(home), "/root", "/tmp", "/private/tmp", "/var/tmp",
                "/var/folders", "/private/var/folders"] + volume_roots() + secrets
    paths = []
    for entry in os.environ.get("PATH", "").split(":"):
        if not entry.startswith("/"):
            continue
        real = realpath(entry)
        if not os.path.isdir(real):
            continue
        candidates = [real]
        if os.path.basename(real) in ("bin", "sbin"):
            candidates.append(os.path.dirname(real))
        for c in candidates:
            if c != "/" and not any(_conflicts(c, e) for e in excluded if e):
                paths.append(c)
    return paths


def _system_read_paths(secrets: List[str]) -> List[str]:
    system = platform.system()
    paths = [p for p in SYSTEM_READ.get(system, []) if "[" in p or os.path.lexists(p)]
    paths += _path_read_paths(secrets)
    if system == "Darwin":
        # Developer tools of the /usr/bin shims (git, clang, make...).
        proc = subprocess.run(["xcode-select", "-p"], stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, encoding="utf-8")
        developer = proc.stdout.strip() if proc.returncode == 0 else ""
        if developer:
            # Xcode.app/Contents/Developer: the shims also read the bundle's
            # Info.plist and shared frameworks.
            app = developer.split(".app/", 1)[0] + ".app" if ".app/" in developer else developer
            paths.append(app)
        # The shims' lookup cache, read only: a writable cache would let Claude
        # point git at its own program, which the developer would then run.
        try:
            paths.append(os.confstr("CS_DARWIN_USER_TEMP_DIR") + "xcrun_db")
        except (ValueError, OSError):
            pass
    # Installation prefix of each tool (Homebrew Cellar, ~/.nvm/versions/...),
    # unless it contains or sits inside a sensitive area.
    home = os.environ["HOME"]
    for tool in TOOLS:
        found = shutil.which(tool)
        if not found:
            continue
        prefix = os.path.dirname(os.path.dirname(realpath(found)))
        if prefix in ("/", home, os.path.dirname(home)) or any(_conflicts(prefix, s) for s in secrets):
            continue
        paths.append(prefix)
    return paths


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


def _read_root(ctx: Context) -> List[str]:
    """Paths that make the session's directory readable. When it holds the
    wrapper's state (a session started in the home directory), the branch that
    leads to the state is left out and its siblings are allowed one by one: on
    Linux, a read-only bind of the whole directory would cover the writable
    mounts of tmp/ and claude-home inside it; on macOS, the deny of the state
    would win over their allow."""
    if ctx.work == ctx.state_root or not path_within(ctx.work, ctx.state_root):
        return [ctx.work]
    paths = []
    current = ctx.work
    for part in os.path.relpath(ctx.state_root, ctx.work).split("/"):
        if platform.system() == "Darwin" and current != "/":
            # The directory itself, for listing it: an exact-match glob.
            paths.append(current[:-1] + "[" + current[-1] + "]")
        try:
            entries = os.listdir(current)
        except OSError:
            entries = []
        paths += [os.path.join(current, e) for e in entries if e != part]
        current = os.path.join(current, part)
    return paths


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
            if _conflicts(secret, claude_dir):
                die(f"the claude binary ({claude_dir}) is inside a sensitive area ({secret}): set claude_bin")

    # Paths added by the configuration: not inside a sensitive area, not above
    # one, and not through a link (srt follows links).
    extra_read = ctx.config.paths("filesystem", "allow_read")
    extra_write = ctx.config.paths("filesystem", "allow_write")
    for p in extra_read + extra_write:
        real = realpath(p)
        if p == "/" or real == "/":
            die("the configuration opens the root directory /")
        for secret in secrets:
            if _conflicts(p, secret) or _conflicts(real, secret):
                die(f"the configuration opens {p}, which conflicts with {secret}")

    # Everything is unreadable, except what is re-allowed below; the specific
    # denies still win over a broader allow.
    # The wrapper's state, except what the session needs: its Claude
    # configuration and its own directory (run/, tmp/, the copy). A deny that
    # covered them would win over their allow when the home directory is
    # readable (read_only_root).
    deny_read = ["/", ctx.state, ctx.archive] + volume_roots() + other_sessions(ctx)
    deny_read += [os.path.join(ctx.state_root, e) for e in sorted(os.listdir(ctx.state_root))
                  if os.path.join(ctx.state_root, e) not in (ctx.claude_home, ctx.repo_dir)]
    # Old transcripts may hold protected code read outside the sandbox.
    deny_read += [os.path.join(home, ".claude", item) for item in PRIVATE_CLAUDE_ITEMS]
    if ctx.directory_mode:
        # The home directory is readable only when the session's directory
        # holds it (read_only_root): a deny inside an allow would win.
        deny_read += [d for d in (home, os.path.dirname(home)) if not path_within(ctx.work, d)]
    else:
        deny_read += [home, os.path.dirname(home), ctx.dev_root, ctx.dev_git_common]
    deny_read += SYSTEM_DENY.get(platform.system(), [])
    allow_read = _read_root(ctx) + [ctx.tmp, ctx.run_dir, ctx.claude_home,
                  os.path.join(ctx.root, "libexec"), claude_dir] + extra_read
    allow_read += _system_read_paths(secrets)
    # ~/.claude configuration items, passed through read only (sync_claude_config).
    allow_read += ctx.config_read
    # srt's helpers (the seccomp filter on Linux) run inside the sandbox.
    allow_read.append(os.path.join(os.path.dirname(os.path.dirname(realpath(ctx.srt))), "vendor"))
    allow_write = ([] if ctx.read_only_root else [ctx.work]) + [ctx.tmp, ctx.claude_home] + extra_write
    deny_write = [] if ctx.read_only_root else [os.path.join(ctx.copy_git, p) for p in LOCKED_GIT_PATHS]
    deny_write.append(os.path.join(ctx.work, PROTECT_FILE))
    deny_write += [os.path.join(ctx.work, p) for p in ctx.protected]
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
    write_text(os.path.join(ctx.run_dir, "srt-settings.json"), json.dumps(settings, indent=2) + "\n")


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


def _session_label(ctx: Context) -> str:
    if ctx.directory_mode:
        name = ctx.work if ctx.read_only_root else os.path.basename(ctx.work)
        return f"directory {name}" + (" (read only)" if ctx.read_only_root else "")
    branch = ctx.git_copy.value("symbolic-ref", "-q", "--short", "HEAD") or "detached HEAD"
    return f"protected repository {os.path.basename(ctx.dev_root)} · {branch}"


def _has_status_line(ctx: Context) -> bool:
    """True if the developer configured their own Claude Code status line."""
    home = os.path.join(os.environ["HOME"], ".claude")
    for f in (os.path.join(home, "settings.json"), os.path.join(home, "settings.local.json"),
              os.path.join(ctx.work, ".claude", "settings.json"),
              os.path.join(ctx.work, ".claude", "settings.local.json")):
        data = _load_json(f) if os.path.isfile(f) else None
        if isinstance(data, dict) and data.get("statusLine"):
            return True
    return False


def claude_settings(ctx: Context) -> str:
    """Settings passed on Claude Code's command line: its own sandbox off
    (srt wraps it), and a status line that marks the session as sandboxed,
    unless the developer has their own."""
    settings: dict = {"sandbox": {"enabled": False}}
    if not _has_status_line(ctx):
        text = f"🔒 sandboxed · {_session_label(ctx)}"
        settings["statusLine"] = {"type": "command",
                                  "command": f"printf '\\033[1;32m%s\\033[0m' {shlex.quote(text)}"}
    return json.dumps(settings, ensure_ascii=False)


def known_protected_repositories(ctx: Context) -> List[str]:
    """Protected repositories the wrapper has been used in, outside the
    session's directory, as the developer would write them (~/...)."""
    home = os.environ["HOME"]
    found = []
    for marker in sorted(glob.glob(os.path.join(ctx.state_root, "*", "state", "repository"))):
        try:
            repo = read_text(marker).strip()
        except OSError:
            continue
        if repo and os.path.isfile(os.path.join(repo, PROTECT_FILE)) and not path_within(ctx.work, repo):
            found.append("~" + repo[len(home):] if path_within(home, repo) else repo)
    return found


def confirm_session(ctx: Context) -> bool:
    """In a terminal, says which mode starts and why, then waits for Enter.
    Returns False if the developer cancels."""
    if not (sys.stdin.isatty() and sys.stderr.isatty()):
        return True
    green, bold, reset = "\033[1;32m", "\033[1m", "\033[0m"
    if not ctx.directory_mode:
        branch = ctx.git_copy.value("symbolic-ref", "-q", "--short", "HEAD") or "detached HEAD"
        hidden = ", ".join(ctx.protected[:5]) + (f" and {len(ctx.protected) - 5} more"
                                                 if len(ctx.protected) > 5 else "")
        intro = f"This repository is protected: it has a {PROTECT_FILE} file."
        lines = [
            f"Hidden from Claude:  {hidden}",
            f"Claude works on:     a copy of this repository, branch {branch}",
            "Your files:          untouched; get Claude's commits with: git fetch claude",
        ]
    elif ctx.read_only_root:
        intro = f"You started Claude in {ctx.work}, which holds your home folder. Nothing in it is protected."
        lines = [
            "Claude can read:     everything in it, including SSH keys and keychain",
            "Claude can edit:     nothing there; it will tell you the changes to make",
            "Hidden from Claude:  your old Claude conversations, and other disks",
        ]
    else:
        intro = f"No {PROTECT_FILE} here: Claude works directly in this folder."
        lines = [
            f"Claude can edit:     {ctx.work}",
            "Hidden from Claude:  the rest of your computer (home folder, other projects, SSH keys)",
        ]
    if ctx.directory_mode:
        protected = known_protected_repositories(ctx)
        if protected:
            shown = ", ".join(protected[:3]) + (f" and {len(protected) - 3} more" if len(protected) > 3 else "")
            lines.append(f"Protected, hidden:   {shown}")
    title = NAME
    prompt = "Press Enter to start Claude, or Ctrl-C to cancel: "
    texts = ["", f"🔒 {title}", "", intro, ""] + [f"   {line}" for line in lines] + ["", prompt]
    print(f"\n{green}🔒 {title}{reset}\n\n{bold}{intro}{reset}\n", file=sys.stderr)
    for line in lines:
        print(f"   {line}", file=sys.stderr)
    print(f"\n{prompt}", end="", file=sys.stderr, flush=True)
    try:
        started = bool(sys.stdin.readline())
    except KeyboardInterrupt:
        print(file=sys.stderr)
        return False
    if started:
        # Erase the screen before Claude takes the terminal: up as many rows
        # as were printed (long lines wrap), then clear to the end.
        columns = max(shutil.get_terminal_size().columns, 1)
        widths = [len(t) + t.count("🔒") for t in texts]  # the emoji takes two cells
        rows = sum(max(1, -(-w // columns)) for w in widths)
        print(f"\033[{rows}A\r\033[J", end="", file=sys.stderr, flush=True)
    return started


def run_sandboxed(ctx: Context, cwd: str, args: List[str]) -> int:
    """Runs Claude Code inside srt. Returns claude's exit status."""
    env = dict(os.environ)
    path = env.get("PATH", "")
    if platform.system() == "Linux":
        # bubblewrap shows allowed paths where they really are, not through
        # links: /run/current-system/sw/bin on NixOS would be missing.
        path = ":".join(dict.fromkeys(realpath(p) if p.startswith("/") else p for p in path.split(":")))
    env.update({
        "CLAUDE_CONFIG_DIR": ctx.claude_home,
        "GIT_CONFIG_GLOBAL": os.path.join(ctx.run_dir, "gitconfig"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "PATH": ":".join([os.path.join(ctx.root, "libexec"), os.path.join(ctx.run_dir, "bin"), path]),
        # Claude Code's own temp root, /tmp by default, which is unreadable.
        "CLAUDE_CODE_TMPDIR": ctx.tmp,
        "DISABLE_AUTOUPDATER": "1",
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
    # Ctrl-C goes to Claude; the wrapper must survive to print the end of
    # session summary. A handler (unlike an ignored signal) is reset in child
    # processes.
    if not confirm_session(ctx):
        log("cancelled")
        return 130
    signal.signal(signal.SIGINT, lambda *_: None)
    status = subprocess.call(cmd, cwd=cwd, env=env)
    return 128 - status if status < 0 else status
