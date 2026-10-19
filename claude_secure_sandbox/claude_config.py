"""Claude Code's configuration in the sandbox: the developer's settings,
skills and MCP servers, its login, and the settings passed on its command
line."""

from __future__ import annotations

import json
import os
import platform
import shlex
import shutil
import subprocess

from .common import (ensure_real_dir, install_file, install_json, load_json, overlaps, path_within, realpath,
                     regular_file, remove_entry, warn)
from .context import Context
from .screen import session_label

# Items of ~/.claude passed through, read only.
SYNC = ["settings.json", "CLAUDE.md", "agents", "commands", "skills", "plugins"]
# Entries of ~/.claude/plugins that Claude Code rewrites itself when it syncs
# the plugins of claude.ai: the sandbox keeps its own copy of them.
OWN_PLUGIN_ITEMS = {"synced", "store"}
# The onboarding already done outside (theme, login method), so that Claude
# Code does not start over in the sandbox.
ONBOARDING_KEYS = ("hasCompletedOnboarding", "lastOnboardingVersion", "theme")
# Claude Code's login on macOS, in the user's keychain.
KEYCHAIN_SERVICE = "Claude Code-credentials"

# Items of ~/.claude that hold transcripts, history or credentials: hidden
# from the sandbox.
PRIVATE_CLAUDE_ITEMS = {"projects", "file-history", "history.jsonl", "todos", "shell-snapshots",
                        "sessions", "statsig", "debug", "ide", ".credentials.json"}


def sync_claude_config(ctx: Context) -> None:
    """The sandbox's Claude configuration: the developer's, read only, and
    the parts of ~/.claude.json the session needs."""
    ensure_real_dir(ctx.claude_home)
    _link_config_items(ctx)
    _copy_claude_json(ctx)


def _link_config_items(ctx: Context) -> None:
    """Each SYNC item of ~/.claude becomes a link to the original, whose real
    path becomes readable (ctx.config_read): nothing in ~/.claude is writable
    from the sandbox, and transcripts stay unreadable. An item that leads to
    a sensitive place is left out."""
    src, dst = os.path.join(os.environ["HOME"], ".claude"), ctx.claude_home
    sensitive = [ctx.state_root, os.path.join(os.environ["HOME"], ".ssh")]
    if ctx.protected:
        sensitive += [ctx.dev_root, ctx.dev_git_common]
    for item in SYNC:
        target = os.path.join(dst, item)
        # Never write through a link Claude planted in claude-home.
        if not path_within(dst, realpath(os.path.dirname(target))):
            warn(f"sync item ignored (link in {dst}): {item}")
            continue
        try:
            source = os.path.join(src, item)
            real = realpath(source) if os.path.exists(source) else ""
            if real and any(overlaps(real, s) for s in sensitive if s):
                warn(f"sync item ignored (it leads to {real}): {item}")
                real = ""
            if item == "plugins" and real and os.path.isdir(real):
                pass_plugins_through(source, target)
            else:
                remove_entry(target)
                if not real:
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                os.symlink(source, target)
            ctx.config_read.append(real)
        except OSError:
            warn(f"cannot pass ~/.claude/{item} through")


def _copy_claude_json(ctx: Context) -> None:
    """~/.claude.json is Claude Code's state (account, every project's paths
    and sessions), rewritten constantly: neither passed through nor copied.
    Only these parts go into the sandbox's own .claude.json: the global MCP
    servers, this repository's (claude mcp add, local scope) and its trust,
    attached to the directory Claude works in, and the onboarding."""
    home_json = os.path.join(os.environ["HOME"], ".claude.json")
    source = load_json(home_json) if os.path.isfile(home_json) else None
    if not isinstance(source, dict):
        return
    target = os.path.join(ctx.claude_home, ".claude.json")
    # Read only when it is a regular file, never a link Claude planted.
    current = load_json(target) if regular_file(target) else None
    data = current if isinstance(current, dict) else {}
    data["mcpServers"] = source.get("mcpServers") or {}
    for key in ONBOARDING_KEYS:
        if key in source:
            data[key] = source[key]
    projects = source.get("projects") if isinstance(source.get("projects"), dict) else {}
    mine = projects.get(ctx.dev_root) if isinstance(projects.get(ctx.dev_root), dict) else {}
    if not isinstance(data.get("projects"), dict):
        data["projects"] = {}
    entry = data["projects"].get(ctx.work)
    entry = entry if isinstance(entry, dict) else {}
    entry["mcpServers"] = mine.get("mcpServers") or {}
    if mine.get("hasTrustDialogAccepted") is True:
        entry["hasTrustDialogAccepted"] = True
    data["projects"][ctx.work] = entry
    install_json(data, target, ctx.wtmp)


def pass_plugins_through(source: str, target: str) -> None:
    """~/.claude/plugins in the sandbox: a directory of read-only links to its
    entries, except OWN_PLUGIN_ITEMS, copied from the original the first time
    and then kept up to date by Claude Code in the sandbox. A plugin Claude
    writes there runs only in the sandbox."""
    if os.path.islink(target) or not os.path.isdir(target):
        remove_entry(target)
        os.makedirs(target)
    names = set(os.listdir(source))
    for name in os.listdir(target):
        if name not in names and os.path.islink(os.path.join(target, name)):
            os.unlink(os.path.join(target, name))
    for name in sorted(names):
        original, entry = os.path.join(source, name), os.path.join(target, name)
        if name not in OWN_PLUGIN_ITEMS:
            remove_entry(entry)
            os.symlink(original, entry)
        elif os.path.islink(entry) or not os.path.lexists(entry):
            remove_entry(entry)
            if os.path.isdir(original):
                shutil.copytree(original, entry, symlinks=True)
            else:
                shutil.copy2(original, entry, follow_symlinks=False)


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
    return load_json(source) if regular_file(source) else None


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
    current = load_json(target) if regular_file(target) else None
    if _expires_at(current) >= _expires_at(login):
        return
    tmp = os.path.join(ctx.wtmp, ".credentials.json")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(login, f)
    install_file(tmp, target)


def _has_status_line(ctx: Context) -> bool:
    """True if the developer configured their own Claude Code status line."""
    home = os.path.join(os.environ["HOME"], ".claude")
    for f in (os.path.join(home, "settings.json"), os.path.join(home, "settings.local.json"),
              os.path.join(ctx.work, ".claude", "settings.json"),
              os.path.join(ctx.work, ".claude", "settings.local.json")):
        data = load_json(f) if os.path.isfile(f) else None
        if isinstance(data, dict) and data.get("statusLine"):
            return True
    return False


def claude_settings(ctx: Context) -> str:
    """Settings passed on Claude Code's command line: its own sandbox off
    (srt wraps it), and a status line that marks the session as sandboxed,
    unless the developer has their own."""
    settings: dict = {"sandbox": {"enabled": False}}
    if not _has_status_line(ctx):
        text = f"🔒 sandboxed · {session_label(ctx)}"
        settings["statusLine"] = {"type": "command",
                                  "command": f"printf '\\033[1;32m%s\\033[0m' {shlex.quote(text)}"}
    return json.dumps(settings, ensure_ascii=False)


