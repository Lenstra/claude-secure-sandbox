"""What a session may reach beyond its working directory: network domains,
paths that nothing may open, tools on the PATH, and the check of what a
repository's .claudesandbox asks for."""

from __future__ import annotations

import ipaddress
import os
import platform
import socket
import subprocess
from typing import List

from .common import SANDBOX_FILE, die, overlaps, path_within, realpath
from .context import Context

# --- network -------------------------------------------------------------------

# Claude's API and login, nothing else. Not mcp-proxy.anthropic.com: through
# it, the claude.ai connectors (GitHub among them) read for Claude, with the
# developer's rights, what the sandbox hides.
DEFAULT_DOMAINS = ["api.anthropic.com", "claude.ai", "platform.claude.com", "console.anthropic.com"]

# Git hosts that are always denied, on top of the hosts of the developer's remotes.
GIT_HOSTS = [
    "github.com", "*.github.com", "*.githubusercontent.com", "gitlab.com", "*.gitlab.com",
    "bitbucket.org", "*.bitbucket.org", "dev.azure.com", "*.dev.azure.com",
    "*.visualstudio.com", "codeberg.org", "sourceforge.net", "*.sourceforge.net",
    # GitHub's MCP server, which reads the repositories GitHub hosts.
    "githubcopilot.com", "*.githubcopilot.com",
]


def url_host(url: str) -> str:
    """Host of a git URL (https://h/x, ssh://u@h:22/x, u@h:x, u@[::1]:x);
    empty for a local path."""
    if "://" in url:
        host = url.split("://", 1)[1].split("/", 1)[0].rsplit("@", 1)[-1]
    elif url.startswith(("/", "./", "../", "file:")) or ":" not in url:
        return ""
    else:
        user, at, rest = url.partition("@")
        host = rest if at and ":" not in user else url
    host = host.split("]", 1)[0] + "]" if host.startswith("[") else host.split(":", 1)[0]
    return host.lower()


def denied_domains(ctx: Context) -> List[str]:
    """The git hosts, and the hosts of the developer's remotes, as git
    resolves them (url.<base>.insteadOf applied)."""
    hosts = set(GIT_HOSTS)
    for remote in ctx.git_dev.value("remote").split():
        for push in ([], ["--push"]):
            for url in ctx.git_dev.value("remote", "get-url", "--all", *push, remote).splitlines():
                if url_host(url):
                    hosts.add(url_host(url))
    return sorted(hosts)


def allowed_domains(ctx: Context) -> List[str]:
    """The Anthropic domains, and the confirmed [allowed-domain] entries."""
    return sorted(set(DEFAULT_DOMAINS) | set(ctx.granted_domains))


def domains_overlap(a: str, b: str) -> bool:
    """True if two domain entries (name or *.name, with an optional port)
    can match the same host: *.name matches every name below it."""
    def split(entry):
        host = entry.rsplit(":", 1)[0] if entry.count(":") == 1 else entry
        return (host[2:], True) if host.startswith("*.") else (host, False)
    (ha, wa), (hb, wb) = split(a), split(b)
    return (ha == hb or (wa and hb.endswith("." + ha)) or (wb and ha.endswith("." + hb))
            or (wa and wb and (ha.endswith("." + hb) or hb.endswith("." + ha))))


def local_destination(entry: str) -> bool:
    """True for an [allowed-domain] entry that designates this computer: localhost, a
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
        # The short and numeric forms of IPv4 that resolvers accept: 127.1,
        # 2130706433, 0x7f.1.
        try:
            address = ipaddress.ip_address(socket.inet_aton(host))
        except OSError:
            return False
    if getattr(address, "ipv4_mapped", None):
        address = address.ipv4_mapped
    return address.is_loopback or address.is_unspecified or address.is_link_local or _assigned_here(address)


def _assigned_here(address) -> bool:
    """True if the address is one of this computer's own (192.168.1.10 on its
    Wi-Fi, for instance): binding a socket to it succeeds only then."""
    family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as s:
            s.bind((str(address), 0))
        return True
    except OSError:
        return False


# --- paths ---------------------------------------------------------------------

# Credentials, relative to the home directory: they open the git servers
# that host protected code.
CREDENTIALS = [".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker/config.json", ".netrc",
               ".git-credentials", ".npmrc", ".pypirc", ".cargo/credentials.toml", ".config/gh",
               ".config/hub", ".config/gcloud", ".password-store", "Library/Keychains",
               ".local/share/keyrings"]
TEMP_DIRS = ["/tmp", "/private/tmp", "/var/tmp"]
# Where users' files live, with the home directory, the other disks and the
# temp directories: closed by default.
USER_AREAS = {"Darwin": ["/Users"], "Linux": ["/home", "/root"]}


def user_temp_dirs() -> List[str]:
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


def secrets(ctx: Context) -> List[str]:
    """Paths nothing may open: credentials, Claude's transcripts and login,
    the temp directories, the wrapper's state and protected code."""
    home = os.environ["HOME"]
    # Protected code first: it is the reason given when a path overlaps several.
    paths = list(ctx.protected_elsewhere)
    if ctx.protected:
        paths += [ctx.dev_root, ctx.dev_git_common]
    paths += [ctx.state_root, os.path.join(home, ".claude"), os.path.join(home, ".claude.json")]
    paths += [os.path.join(home, c) for c in CREDENTIALS] + TEMP_DIRS + user_temp_dirs()
    return paths


def overlapped_secret(ctx: Context, path: str) -> str:
    """The secret that path lies in or holds, or an empty string."""
    for secret in secrets(ctx):
        if overlaps(path, secret) or overlaps(realpath(path), realpath(secret)):
            return secret
    return ""


def path_tools(ctx: Context) -> List[str]:
    """The tools on the developer's PATH, readable even inside the home
    directory: each bin directory with its installation (~/.nvm/versions/
    node/v22 for ~/.nvm/versions/node/v22/bin), never the home directory
    itself, and never a directory that overlaps a secret."""
    home = os.environ["HOME"]
    own = {os.path.join(ctx.root, "bin"), os.path.join(ctx.root, "libexec"), os.path.join(ctx.run_dir, "bin")}
    tools = []
    for d in os.environ.get("PATH", "").split(":"):
        if not d.startswith("/") or d in own or not os.path.isdir(d):
            continue
        real = realpath(d)
        top = os.path.dirname(real) if os.path.basename(real) == "bin" else real
        for candidate in (top, real):
            if not path_within(candidate, home) and not overlapped_secret(ctx, candidate):
                tools.append(candidate)
                break
    return tools


# --- .claudesandbox ------------------------------------------------------------


def check_access(ctx: Context, paths: List[str], domains: List[str]) -> None:
    """Stops the launch, before the developer is asked, if the repository's
    .claudesandbox asks for what nothing may open: a git host, this computer,
    a secret."""
    denied = denied_domains(ctx)
    for d in domains:
        if any(domains_overlap(d, h) for h in denied):
            die(f"{SANDBOX_FILE}: git host not allowed in [allowed-domain]: {d}")
        if local_destination(d):
            die(f"{SANDBOX_FILE}: this computer is not allowed in [allowed-domain]: {d}")
    for p in paths:
        secret = overlapped_secret(ctx, p)
        if secret:
            die(f"{SANDBOX_FILE}: [allowed-external] {p} not allowed, it overlaps {secret}")
