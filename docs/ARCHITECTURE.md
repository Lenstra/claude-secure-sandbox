# claude-secure-sandbox: architecture

claude-secure-sandbox lets developers use Claude Code on repositories that contain code Claude must never read. Sensitive paths are listed in a `.claudeprotected` file; Claude works in a copy of the repository from which their content has been removed, in files and in git history alike, inside an OS sandbox that hides the rest of the machine. Claude still uses git normally, and the developer fetches its work with `git fetch claude`.

This document explains how it works, the security model, and why it is built this way. The [README](../README.md) covers installation and usage.

## Contents

1. [Goal and scope](#1-goal-and-scope)
2. [Why existing setups fall short](#2-why-existing-setups-fall-short)
3. [Overview](#3-overview)
4. [Interception and session modes](#4-interception-and-session-modes)
5. [The `.claudeprotected` file](#5-the-claudeprotected-file)
6. [The filtered copy](#6-the-filtered-copy)
7. [The sandbox](#7-the-sandbox)
8. [Session lifecycle](#8-session-lifecycle)
9. [Retrieving Claude's work](#9-retrieving-claudes-work)
10. [Configuration](#10-configuration)
11. [Security model](#11-security-model)
12. [Code execution (planned)](#12-code-execution-planned)
13. [Technical choices](#13-technical-choices)
14. [Limits and troubleshooting](#14-limits-and-troubleshooting)
15. [Roadmap](#15-roadmap)
16. [On-disk layout](#16-on-disk-layout)
17. [Code layout and tests](#17-code-layout-and-tests)

## 1. Goal and scope

Protected code is tracked by git and present in its history: that is the normal case, not an accident to clean up. The tool must hold even if Claude is manipulated by a prompt injection (malicious text in a file, an issue, a web page) and actively tries to reach that code.

**Guaranteed**

- The content of protected paths is unreadable from the sandbox, whatever the tool: file reads, shell, scripts, git, network. That includes past versions, and identical content at other paths.
- In every session, protected repository or not, Claude reads only its working directory, the system and the installed tools. The developer's home directory, other projects, temp directories, SSH keys and keychain are unreadable.
- No process started by Claude can get a program outside the sandbox to run something for it (launchd, cron, other applications, sockets, files run later by the developer's tools).
- The network is limited to an allowlist of domains, with no git host.
- A path removed from `.claudeprotected` stays protected until the developer confirms the removal, outside the sandbox.

**Out of scope**

- Stopping a developer from bypassing the tool (`claude-unsandboxed`) or pasting code into the conversation: that is the job of MDM and internal policy.
- Hiding the names of protected files, their sizes and commit messages.
- Inference from visible code (callers, tests, interfaces).
- Kernel or `srt` vulnerabilities (`srt` is a research preview).
- Windows, submodules and git worktrees (v1).

## 2. Why existing setups fall short

- **An instruction in the prompt or in `CLAUDE.md`** ("do not read `core/`") is not deterministic. Claude can lose track of it in a long session, or be steered by a prompt injection.
- **Claude Code's permission rules and built-in sandbox** are enforced, but tool by tool and path by path. A rule that blocks reading `core/` with one tool does not cover another tool, a script, or `git show` on an old commit.
- **A Docker container or devcontainer** isolates the whole process tree, but mounting the repository mounts its `.git`, with the full history.

| Approach | Enforced by | Protected files on disk | Protected content in git history | Claude can commit |
| --- | --- | --- | --- | --- |
| Instruction in the prompt or `CLAUDE.md` | the model, not deterministic | readable | readable | yes |
| Claude Code permission rules and built-in sandbox | Claude Code, tool by tool | blocked for the configured tools | readable through `.git`, unless `.git` is blocked too | only if `.git` stays readable, which leaks history |
| Docker container or devcontainer | the container | depends on the mount | readable if `.git` is mounted | only if `.git` is mounted, which leaks history |
| claude-secure-sandbox | the OS kernel, for every process Claude starts | absent | absent | yes, fetched with `git fetch` |

Every protected file lives on in `.git`: in packfiles, in `git log -p`, in `git show <old-commit>:path`. Hiding the working tree is not enough, and blocking `.git` takes git away from Claude. claude-secure-sandbox removes that trade-off.

## 3. Overview

```mermaid
flowchart LR
  subgraph host["Developer machine, outside the sandbox"]
    dev["Developer repository<br/>full code and history"]
    wrapper["Wrapper<br/>bin/claude"]
  end
  subgraph sandbox["srt sandbox (Seatbelt on macOS, bubblewrap on Linux)"]
    claude["Claude Code"]
    copy["Filtered copy<br/>no protected content"]
    claude --> copy
  end
  dev -- "1. filtered fetch" --> wrapper
  wrapper -- "2. updates" --> copy
  wrapper -- "3. launches" --> claude
  copy -- "4. git fetch claude" --> dev
```

1. **Filtered copy.** At launch, the wrapper fetches the developer's repository into a copy, without the protected content, and puts it on the developer's current branch. In history, a hidden content appears as one line: `[claude-secure-sandbox] hidden by .claudeprotected`.
2. **Sandboxed session.** Claude Code runs inside `srt`, in the copy. Reading is denied everywhere except the copy, the system and the tools.
3. **Retrieval.** The developer's repository has a `claude` remote that points to the copy: `git fetch claude` brings Claude's branches as `claude/<branch>`.
4. **Integration.** The developer reviews, merges or rebases, and pushes. CI builds and tests with the full code.

| Component | Role |
| --- | --- |
| Wrapper `bin/claude` (Python 3.9+, standard library only) | Intercepts the `claude` command, prepares the copy and the sandbox, asks the developer when a decision is theirs. Placed before the real binary on the `PATH`; the real binary stays available as `claude-unsandboxed`. |
| `srt` (`@anthropic-ai/sandbox-runtime` 0.0.78, pinned) | Confines Claude's whole process tree: tools, shell, local MCP servers, hooks. On macOS, a kernel Seatbelt profile; on Linux, bubblewrap namespaces with a seccomp filter. Both are generated from the same settings file. |
| Filtered copy (`~/.cache/claude-secure-sandbox/<repo>-<hash>/work`) | Claude's workspace, with its own `.git`: same commits and SHAs as the developer's repository, without protected content. |
| `libexec/git-remote-csnull` | Promisor remote of the copy, which serves no object, so git never tries to fetch hidden content. |
| The `claude` remote | Remote of the developer's repository that points to the copy, push disabled. |

The wrapper is the only trusted component that touches both sides. Claude only ever touches the copy, and everything it writes there is treated as untrusted.

### What Claude sees

| | In Claude's copy |
| --- | --- |
| Public files | as in the developer's repository, at the current commit |
| Protected paths | absent from disk |
| Protected content in history | `git log -p`, `git show` and `git blame` show one line instead: `[claude-secure-sandbox] hidden by .claudeprotected` |
| Public files whose content is identical to a protected file (a copy, a file moved out of a protected directory) | absent too, listed once before Claude starts; changing such a file, even by one line, makes the new version visible |
| Names of protected files, their sizes, commit messages | visible |
| The developer's branches | updated at each launch, without the protected content |
| The developer's uncommitted changes, and commits made during the session | not visible until they are committed, and the next launch |

Claude cannot build or test what depends on protected code. It builds what it can, and when a task needs a change in a protected path, it describes the change for the developer to make by hand.

## 4. Interception and session modes

The wrapper is a script named `claude`, placed before the real Claude Code binary on the `PATH`. The developer keeps typing `claude`.

```mermaid
flowchart TD
  start["claude ..."] --> sub{"Subcommand without a session?<br/>--version, mcp, config, update..."}
  sub -- yes --> real["exec the real claude"]
  sub -- no --> inside{"Already in a sandbox<br/>or inside the wrapper's state?"}
  inside -- yes --> real
  inside -- no --> home{"Directory holds the home<br/>directory or other volumes?"}
  home -- yes --> refusehome["refuse: run claude<br/>from a project directory"]
  home -- no --> nested{".claudeprotected below<br/>the repository root?"}
  nested -- yes --> refuse["refuse: run claude<br/>from that directory"]
  nested -- no --> root{".claudeprotected at the<br/>repository root?"}
  root -- yes --> protected["protected mode:<br/>filtered copy + sandbox"]
  root -- no --> directory["directory mode:<br/>sandbox in the directory itself"]
```

Every session runs in the sandbox, in one of two modes:

| | Protected mode | Directory mode |
| --- | --- | --- |
| When | `.claudeprotected` at the repository root | anywhere else |
| Claude works in | the filtered copy | the repository root, or the current directory outside a repository, directly |
| Readable | the copy, the system, the tools | that directory, the system, the tools |
| Writable in `.git` | everything except configuration, hooks and object store metadata | the same |
| Retrieval | `git fetch claude` | nothing to retrieve: Claude commits in the directory |

In both modes the rest of the machine is unreadable (section 7), so a protected repository next to the working directory stays out of reach without the wrapper knowing about it.

- **Finding the real binary.** `claude_bin` from the configuration, otherwise the first `claude` on the `PATH` that is not the wrapper itself (symlinks resolved).
- **Subcommands without a session** run unchanged: `-v`, `--version`, `-h`, `--help`, `update`, `doctor`, `mcp`, `config`, `plugin`, `setup-token`, `install`, `migrate-installer`.
- **Nested sandboxes.** Inside a sandbox the wrapper sets `CLAUDE_SECURE_SANDBOX_ACTIVE=1`; a `claude` started from there runs the real binary, already confined.
- **Home directory.** The directory a session can read must not hold the home directory, its parent (`/Users`, `/home`), other volumes (`/Volumes`; `/mnt`, `/media`, `/run/media` on Linux) or the wrapper's state: running `claude` in `~` or `/` is refused. It would make the credentials readable, the keychain included (section 7).
- **Protection files further down.** The subtree of the repository root (or of the current directory outside a repository) is scanned for `.claudeprotected` with `fd`, else `rg`, else `find`, skipping `.git`, `node_modules` and the wrapper's state: about 0.07 s for 75,000 files, 0.7 s for a 550,000-file home directory. A protection file anywhere other than the root leads to a refusal: running from a parent directory would expose that repository.
- **Preconditions:** macOS or Linux, `git`, `rg`, `srt` installed by `npm ci`, and on Linux `bwrap` and `socat`. In protected mode, no git worktree. Submodules trigger a warning: their content is missing from the copy. A lock (`state/lock`, with the PID) allows one session per protected repository; a stale lock is taken over. Starting from inside a protected path is refused; from another subdirectory, Claude starts in the same subdirectory of the copy.
- **Directory mode** has no copy, no lock and no end of session summary. `.git/config`, `.git/hooks` and the other locked paths of `.git` are read-only (section 7): the developer's git, outside the sandbox, would otherwise run what Claude writes there. It uses its own system prompt, `share/prompts/directory-mode.md`.

## 5. The `.claudeprotected` file

The file sits at the repository root, is committed, and lists one path per line:

```
# proprietary code
core/engine
licensing/keys.go
```

| Rule | Detail |
| --- | --- |
| Literal paths | Relative to the root; a directory protects everything below it. Leading `./`, trailing `/` and repeated `/` are normalized. |
| Comments | A line that starts with `#`, and empty lines, are ignored. |
| Refused | Absolute paths, `..`, `.git`, the root itself, `.claudeprotected` itself, patterns (`*`, `?`, `[`, `\`, leading `!`). An invalid line stops the launch with the list of errors. |
| Source | The file on disk and the version committed at `HEAD`; the union of both applies. |
| Addition | The wrapper keeps a hash of the protected list. When it changes, the copy is rebuilt from scratch and the repository's Claude sessions are archived, since they may have read files that are now protected. |
| Removal | Every version of the file in the history of `HEAD` is read (`git log --full-history -- .claudeprotected`). A path listed by an earlier version and no longer protected stays protected until the developer confirms its removal. |

**Why a removal needs a confirmation.** Inside the copy, `.claudeprotected` is write-denied, but that does not stop a commit that changes it: Claude writes the copy's object store, so it can build such a commit with git plumbing (`hash-object`, `update-index`, `commit-tree`) without touching the file on disk. The commit carries the developer's identity and, once merged, looks like any other change. The wrapper therefore asks in the terminal, before Claude starts:

```
claude-secure-sandbox: paths no longer listed in .claudeprotected, Claude would be able to read them:
  core/engine
claude-secure-sandbox: remove their protection? [y/N]
```

Confirmations are kept in `.git/claude-secure-sandbox/unprotected` in the developer's repository, out of the sandbox's reach. A confirmation ends when the path is protected again, so a later removal asks again. Without a terminal (`claude -p` in a script), the path stays protected. Losing the confirmations only means being asked again: it never unprotects anything.

**Making one file of a protected directory public.** Patterns and exceptions are not supported: list the other entries of the directory instead of the directory itself, and confirm the removal. Files added to that directory later are then not protected until they are listed. A file moved out of a protected directory stays hidden by its fingerprint (section 6) until its content changes.

## 6. The filtered copy

The copy is a git repository built by the wrapper from the developer's local repository, with the same commits and SHAs but none of the protected contents. The developer's repository is only read, except for the `claude` remote and the confirmations of section 5, both in its local `.git`.

### Steps

1. **Filtered fetch.** The wrapper fetches the developer's branches and `HEAD` into the copy under `refs/incoming/`, and tags that point to a commit under their own name, with a `sparse:oid` partial clone filter. The filter is a sparse specification (`/*`, then `!/<path>` for each protected path), stored in a small repository of the wrapper's state, `state/spec.git`, and made visible to the developer's `upload-pack` through `GIT_ALTERNATE_OBJECT_DIRECTORIES`. Commits and trees are complete; blobs under protected paths are not sent. The fetch is incremental.
2. **Removal by fingerprint.** git filters by path, so a protected content that also exists under a public path (a file moved into a protected directory later, an identical copy, a branch merged with `-s ours`, a commit of Claude's that places a protected blob at a public path) would come through. The wrapper lists every blob that ever existed under a protected path with `git log --raw --full-history --diff-merges=first-parent --root -- <protected paths>` on the developer's repository, and keeps that list in `state/protected-blobs`. Every blob of the copy found in the list is dropped by rewriting the copy's packs with `pack-objects`; the new pack is marked `.promisor`, so git treats missing objects as expected. The empty blob is never hidden, since its content is known.
3. **Absence check.** After every update, the wrapper checks that no blob of the list is present in the copy. If the check fails, the copy is rebuilt from scratch; if it fails again, the session is cancelled.
4. **Placeholders.** Each hidden content is replaced, in the copy only, by the one-line blob `[claude-secure-sandbox] hidden by .claudeprotected`, through git replace refs (`refs/replace/<hidden oid>`). `git log -p`, `git show` and `git blame` keep working. If Claude deletes some replace refs, all of them are recreated at the next launch.
5. **Promisor remote that serves nothing.** The copy's promisor remote is `csnull::host`, handled by `libexec/git-remote-csnull`: it lists no branch and answers every fetch with "success, nothing to send". git never fetches a missing content, and diffs do not fail. Only the wrapper's own fetch redirects `csnull::host` to the developer's repository, for that command only.
6. **Branches.** The copy's branches are updated from `refs/incoming/`, which is then deleted (section 8): the developer's branches become plain branches of the copy, under their own names.
7. **Sparse checkout.** Non-cone sparse checkout excludes protected paths, and public files whose content is hidden at the checked-out commit. They are absent from disk: a placeholder on disk could be edited and committed by mistake. At a new session, the wrapper lists the public files absent for that reason, once per file (`state/reported-absent`).

### Copy configuration

| Setting | Value | Why |
| --- | --- | --- |
| `extensions.partialClone` | `host` | Missing blobs are expected, not corruption. |
| `remote.host.url` | `csnull::host` | Served by the null helper. |
| `remote.host.partialclonefilter` | `sparse:oid=<spec>` | Filter applied to every fetch. |
| `core.sparseCheckout`, `core.sparseCheckoutCone` | `true`, `false` | Arbitrary file exclusions. |
| `core.commitGraph`, `gc.auto`, `maintenance.auto` | off | No background rewrite of the object store. |
| `user.name`, `user.email` | copied from the developer's repository | Claude's commits carry the developer's identity. |

### Hardened git for the wrapper

Claude can write to the copy, so every git command the wrapper runs on it ignores what Claude could have planted:

- no hooks (`core.hooksPath=/dev/null`), no fsmonitor, no untracked cache, no commit-graph, no external diff, no pager, no credential helper, no SSH, protocols limited to `file`;
- no global or system configuration, no grafts, no lazy fetch (`GIT_NO_LAZY_FETCH=1`);
- no replace refs for checks (`GIT_NO_REPLACE_OBJECTS=1`), so placeholders never hide what a commit really contains.

At every launch and at the end of each session, the wrapper also checks the structure of the copy's `.git`: no `alternates`, `http-alternates`, `commondir`, `shallow` or `info/grafts`, no symbolic or hard link in `objects/`, and the promisor remote unchanged. A failure rebuilds the copy at the next launch.

### Performance

Only commits that are new since the last launch are fetched and scanned (`state/host-tips`). The first creation scans the whole history: about 20 s to clone and 23 s to remove fingerprints on git/git (85,000 commits), measured with the prototype.

## 7. The sandbox

Claude Code runs as:

```
srt --settings run/srt-settings.json -- \
  /usr/bin/env TMPDIR=tmp/ claude --settings '{"sandbox":{"enabled":false}}' \
  --append-system-prompt-file run/system-prompt.md <developer's arguments>
```

Claude Code's own sandbox is disabled because `srt` already wraps the whole process tree: every tool, shell command, local MCP server and hook started by Claude inherits the same confinement. Claude is launched by the real path of its binary, since links outside the readable areas do not resolve inside the sandbox.

### Reading: denied by default

`srt` reads are allowed everywhere unless denied. The wrapper denies `/` and re-allows what a session needs. In `srt`, an allowed read beats a broader deny, and a more specific deny still beats an allow.

| Area | Access |
| --- | --- |
| The copy (`work/`), or the directory in directory mode | read and write |
| Session `TMPDIR` (`tmp/`), sandbox Claude configuration (`claude-home/`) | read and write |
| Generated settings, prompt, gitconfig and `bin/` (`run/`), `libexec/`, the Claude Code installation | read |
| System: `/usr`, `/bin`, `/sbin`, `/System`, `/dev`, `/private/etc`, `/Library/Apple`, `/Library/Developer`, `/private/var/select`, the timezone database | read |
| Developer tools: the Xcode bundle (from `xcode-select -p`) and its license file, `/opt/homebrew` except `/opt/homebrew/var`, the installation prefix of `git`, `node`, `python3` and `rg` | read |
| The directories of the `PATH`, and the prefix of the `.../bin` ones, outside the home directory and temp areas (Nix, Linuxbrew, `/usr/local`...) | read |
| `srt`'s own helpers (`vendor/`, for the seccomp filter on Linux) | read |
| The `/etc`, `/var` and `/tmp` links themselves, as exact-match globs, so that paths through them resolve; not the directories they lead to | read |
| `xcrun`'s lookup cache in the user's temp directory | read only (below) |
| Paths of `filesystem.allow_read` and `allow_write` | read, or read and write |
| Everything else: `$HOME`, `/Users`, `/Volumes`, `/tmp`, the user's temp directory (`/var/folders`), `/Applications`, other projects, the wrapper's state, archives, other repositories' sessions | none |
| In protected mode, the developer's repository and its `.git` | none |

- A tool whose installation prefix holds or sits inside a sensitive area (the home directory, `~/.ssh`, `~/.claude`, the repository, the wrapper's state) is not made readable. A Claude Code installation inside such an area is refused, except `~/.claude/local`.
- **xcrun.** On macOS, `/usr/bin/git` is a shim that looks the real binary up through `xcrun` and its cache. The cache is readable but not writable: a writable cache would let Claude point `git` at its own program, which the developer would then run outside the sandbox. Since the shim cannot refresh its cache, `run/bin/git` calls the real binary directly, found by `xcrun -f git` outside the sandbox; without it, every `git` call would run `xcodebuild`.
- The lists are per operating system (`SYSTEM_READ` in `sandbox.py`); only the entries that exist on the machine are used. On Linux: `/usr`, `/bin`, `/sbin`, `/lib*`, `/etc`, `/opt`, and when present `/nix/store`, `/gnu/store`, `/snap` and `/home/linuxbrew/.linuxbrew`. Anything else a distribution or a developer uses is covered by the `PATH` rule above, or by `filesystem.allow_read`. On Windows (not supported yet), `srt` runs the sandbox under a separate account that has no rights on the user's files, so no list would be needed.

### Writing

Writes are denied everywhere except the copy (or the directory), `tmp/`, `claude-home/` and `allow_write`. Inside those, these stay write-denied:

- the `.git` paths that would let the wrapper or the developer's git run code or read elsewhere: `config`, `config.worktree`, `hooks`, `info`, `objects/info`, `commondir`, `shallow`, `modules`, `worktrees`;
- `.claudeprotected` and the protected paths;
- on macOS, an anchor file in every writable directory (`.claude-secure-sandbox-anchor`), which makes `srt` forbid deleting or renaming the directory. `srt` resolves symlinks in its rules, so a `tmp/` swapped for a link would otherwise open another directory in the next session; the wrapper also checks at launch that none is a link. On Linux, a writable directory is a mount point, which cannot be renamed or removed.

`srt` adds its own write protections everywhere: shell startup files (`.bashrc`, `.zshrc`...), `.gitconfig`, `.gitmodules`, `.vscode`, `.idea`, `.mcp.json`, `.claude/commands` and `.claude/agents`.

### Linux specifics

bubblewrap confines by mounting, which changes a few behaviours compared with Seatbelt:

- **Read denies are empty `tmpfs` mounts.** A read-denied directory appears empty and writable inside the sandbox; what is written there vanishes with the session and never reaches the real directory. The escape tests therefore check the real files after the session.
- **Allowed paths appear only where they really are**, not through links. The wrapper launches Claude by its real path and gives the sandbox a `PATH` whose entries are resolved (`/run/current-system/sw/bin` on NixOS).
- **A write deny on a path that does not exist yet** is a mount point that bubblewrap creates on the host for the session. A `.git/commondir` stand-in would break git, so the wrapper creates the locked `.git` directories and the protected paths as empty directories beforehand, and denies only paths that exist. A locked `.git` file that is absent at launch (`commondir`, `shallow`, `config.worktree`) is checked after the session: if one appeared, it is renamed aside with a warning, before any git outside the sandbox reads it.
- **`srt`'s stand-ins** for its own protected files (`.bashrc`, `.gitconfig`, `.vscode`...) appear in every writable directory during the session, on the host too, and are removed afterwards. The sandbox's git ignores them (`core.excludesFile` in `run/gitconfig`), so `git add -A` keeps working.
- **Unix sockets** are blocked by a seccomp filter (x86-64 and arm64): D-Bus, X11 and Wayland clipboards, the SSH agent.
- **A detached process** ends with the session, instead of outliving it confined.
- On Ubuntu 24.04 and later, bubblewrap needs `kernel.apparmor_restrict_unprivileged_userns=0` or an AppArmor profile that grants it user namespaces.

### Process tree and escape attempts

The Seatbelt profile is attached to the process by the kernel and inherited by every descendant. Indirect ways of getting a command run outside the sandbox are closed too; each line below is an end-to-end test, which cleans up if the attempt ever succeeds.

| Attempt from inside the sandbox | Result |
| --- | --- |
| child, grandchild, detached process (`nohup`, background job) | same confinement |
| `crontab`, `at` | denied, nothing created |
| macOS: `launchctl submit`, `open`, `osascript` with an Apple Event, clipboard (`pbcopy`) | denied: Apple Events and Launch Services are off (`allowAppleEvents: false`) |
| Linux: `systemd-run --user`, `xdg-open`, the D-Bus session bus | denied: no Unix socket is reachable |
| SSH agent and other Unix sockets | denied |
| TCP to `localhost` | denied: only the `srt` proxy is reachable (`allowLocalBinding: false`) |
| writing `~/.zshrc`, a LaunchAgent, an autostart entry, the `xcrun` cache | the real files are never written |
| a detached process after the session | macOS: still confined, it cannot read the home directory and loses the network when `srt` exits; Linux: ended with the session |

Two channels remain outside the sandbox's reach by nature: what the developer runs from Claude's commits after merging them (build scripts, `package.json`, CI), which review covers; and escape sequences written to the developer's terminal, which a terminal may use to set the clipboard (OSC 52; in Ghostty, `clipboard-write = ask`). The copy should not be opened in an IDE that runs project tasks or `.envrc` files.

### Keychain and login

`srt` always allows the Mach services of the macOS keychain (`securityd`). With `$HOME` readable, a sandboxed process can read keychain items without any prompt, for example git credentials through `git credential-osxkeychain`. Those credentials open the server that hosts the protected code; from the sandbox they could leave through the conversation, through server-side tools such as web search, or through an allowed domain. The wrapper keeps `$HOME` unreadable, which hides the login keychain, and refuses any session or configuration that would open it.

Claude Code keeps its own login in the keychain on macOS (entry `Claude Code-credentials`), and in `~/.claude/.credentials.json` on Linux. At each launch the wrapper reads that login, and only it, outside the sandbox, and writes it to `claude-home/.credentials.json` (mode 600), where Claude Code falls back when the keychain is unavailable. A newer login already there, refreshed by Claude Code during a session, is kept. Nothing is ever written back to the keychain. Without a login in the keychain, `/login` inside a session stores one in the same file. Claude can read its own token; it can only send it to allowed domains, which is one more reason to keep that list short.

### Separate Claude configuration

Claude Code stores transcripts in `~/.claude/projects/` and file backups in `file-history/`, which can contain protected code read before the wrapper was installed. The wrapper runs Claude with `CLAUDE_CONFIG_DIR=~/.cache/claude-secure-sandbox/claude-home`; in `claude-home`, the sessions of other repositories are unreadable too.

The configuration is passed through, read only. At each launch, each item of the `sync` list (`settings.json`, `CLAUDE.md`, `agents/`, `commands/`, `skills/`, `plugins/` by default) becomes a link in `claude-home` to the original in `~/.claude`, and the real path of that original becomes readable from the sandbox. Claude Code uses the developer's configuration as it is; Claude's own sandbox is disabled on the command line (`--settings`). Nothing in `~/.claude` is writable: Claude could otherwise add a hook, a skill or an MCP server that the developer's Claude would run outside the sandbox. Items that hold transcripts, history or credentials (`projects`, `file-history`, `history.jsonl`, `todos`, `shell-snapshots`, `sessions`, `.credentials.json`...) are never passed through, even when listed in `sync`, and an item whose real path leads into the repository, the wrapper's state or `~/.ssh` is skipped.

MCP servers live in `~/.claude.json`, which is Claude Code's state rather than configuration: the account, every project's paths, sessions and costs, caches, all rewritten constantly. It is neither passed through nor copied; the wrapper copies only the MCP servers into the sandbox's own `.claude.json`: the global ones, and those registered for this repository (`claude mcp add`, local scope), attached to the directory Claude works in. Files the wrapper writes into `claude-home` replace the entry rather than writing through it, files it reads there must be regular files, and its own temp files live in `state/tmp`, out of the sandbox's reach.

### Network

`srt` routes traffic through a filtering proxy. Allowed: the Anthropic domains (`api.anthropic.com`, `claude.ai`, `platform.claude.com`, `console.anthropic.com`, `statsig.anthropic.com`, `mcp-proxy.anthropic.com`) and the domains added in the configuration. Always denied: GitHub, GitLab, Bitbucket, Azure DevOps, Codeberg, SourceForge, and the hosts of the repository's remotes. A configuration that allows one of them, or `*`, is refused.

### Environment and git configuration

| Variable | Value | Why |
| --- | --- | --- |
| `CLAUDE_CONFIG_DIR` | `claude-home` | Separate Claude configuration. |
| `GIT_CONFIG_GLOBAL`, `GIT_CONFIG_NOSYSTEM` | `run/gitconfig`, `1` | The developer's global git configuration (credentials, aliases, hooks) is not used. |
| `PATH` | `libexec/` and `run/bin/` prepended | `git-remote-csnull`, and the direct `git` link. |
| `TMPDIR`, `CLAUDE_CODE_TMPDIR` | `tmp/` | Per-repository temp directory. `srt` forces `/tmp/claude`, shared by every sandbox, and Claude Code's own temp root is `/tmp`, which is unreadable. |
| `DISABLE_AUTOUPDATER` | `1` | No update from inside the sandbox. |
| `CLAUDE_SECURE_SANDBOX_ACTIVE` | `1` | A nested `claude` runs without a new sandbox. |
| `SSH_AUTH_SOCK`, `GIT_ASKPASS`, `SSH_ASKPASS` | unset | No SSH agent and no credential prompt. |

`run/gitconfig` sets the developer's name and email, `commit.gpgsign = false` (no key in the sandbox), and `core.excludesFile` and `core.attributesFile` to `/dev/null`, since their defaults under `~/.config` are unreadable.

### System prompt

The prompt appended to Claude's depends on the mode. In protected mode (`share/prompts/protected-mode.md`) it tells Claude which paths are protected, that their content is absent on purpose, that the placeholder line is not code, that it cannot build what depends on protected code, and how the git workflow works. Both prompts (`share/prompts/directory-mode.md` for directory mode) say what the sandbox makes unreadable, that git hosts are unreachable, and that `.git` configuration and hooks are read-only. Prompts guide Claude but guarantee nothing; only the sandbox does.

## 8. Session lifecycle

```mermaid
sequenceDiagram
  actor Dev as Developer
  participant W as Wrapper (outside the sandbox)
  participant K as Filtered copy
  participant C as Claude Code (in srt)
  Dev->>W: claude, on branch feature-x
  W->>W: read .claudeprotected, confirm removed protections
  W->>K: filtered fetch, fingerprints, absence check
  W->>K: update branches, check out feature-x
  W->>C: launch in the copy
  C->>K: read, edit, run commands, commit on feature-x
  C-->>W: session ends
  W-->>Dev: summary of the new commits
  Dev->>K: git fetch claude
  Dev->>Dev: review, merge or rebase, push
```

### Launch (protected mode)

1. Repository root, `.claudeprotected`, removed protections confirmed or kept (section 5).
2. Lock, then update of the filtered copy (section 6). A warning is printed if the developer has uncommitted work, which Claude will not see.
3. The `claude` remote is added to the developer's repository, or checked (section 9).
4. The copy's branches are updated (below).
5. Configuration and login sync, optional copy of dependency directories (`copy_dirs`), generation of the gitconfig, the `git` link, the `srt` settings and the system prompt, all outside the writable areas.
6. Launch of Claude Code in `srt`, in the copy.

### Branches at a new session

`claude` starts a new session; `claude --continue` and `claude --resume` resume one. At a new session, Claude's uncommitted changes are stashed in the copy (`git stash`, message `claude-secure-sandbox <date>`), then the wrapper compares the developer's current branch with the copy's branch of the same name:

| Situation | What the wrapper does |
| --- | --- |
| The developer's branch contains Claude's commits, as they are or rewritten (merge, rebase, cherry-pick, new signature; rewritten commits are compared by patch with `git rev-list --cherry-pick` in the developer's repository) | The copy restarts from the developer's commit. |
| Claude's branch contains the developer's commit | Claude continues on its own commits, and the wrapper says how many are not integrated yet. |
| Both have new commits | The wrapper asks, in the terminal, before Claude starts. |

```
claude-secure-sandbox: your branch feature-x and Claude's have diverged: 2 commit(s) of yours are not in Claude's branch, 3 of Claude's are not in yours.
  1) start from your branch (Claude's commits stay retrievable)
  2) quit, to integrate Claude's commits first (git fetch claude, then on feature-x: git cherry-pick -S feature-x..claude/feature-x)
  3) continue on Claude's branch, without your new commits
claude-secure-sandbox: choice [1/2/3]:
```

With choice 1, Claude's branch is kept as `refs/backup/<date>/<branch>`, fetchable with `git fetch claude refs/backup/<date>/<branch>`. Without a terminal, choice 3 applies: nothing is lost. A detached `HEAD` is checked out as is.

The developer's other branches follow theirs with the same rule and no question: a branch that holds commits of Claude's the developer has not integrated is left as is, with a message. A branch the developer deleted is deleted from the copy, unless Claude has commits on it. When a session is resumed, the branch Claude has checked out is left alone and the others are updated. Commits the developer makes during a session appear at the next launch.

### Exit

Nothing is verified or copied at the end of a session: Claude's branches are already in the copy, which the `claude` remote points to. The wrapper checks the structure of the copy's `.git` (section 6), then prints a summary:

```
claude-secure-sandbox: end of session
  feature-x: 3 new commit(s)
  next: git fetch claude, then on feature-x: git cherry-pick -S feature-x..claude/feature-x
  2 uncommitted file(s) left in the copy (...); they will be stashed at the next launch
```

The commands it suggests depend on the developer's configuration: `git cherry-pick -S` when `commit.gpgsign` is set, so that Claude's commits are integrated as copies signed by the developer, otherwise `git rebase`.

If the session ends abruptly (terminal closed, crash), nothing is lost: the commits are in the copy, and the next launch offers to continue on them. Ctrl-C reaches Claude; the wrapper survives it to print the summary.

## 9. Retrieving Claude's work

The developer fetches Claude's branches directly from the copy, like from any remote. There is no verification step: confidentiality rests on the sandbox, the filtered copy and the check of `.claudeprotected` at launch, not on checking Claude's commits.

**The `claude` remote**, added at the first launch:

| Setting | Value |
| --- | --- |
| `url` | the copy, `~/.cache/claude-secure-sandbox/<repo>-<hash>/work` |
| `fetch` | `+refs/heads/*:refs/remotes/claude/*` |
| `pushurl` | an invalid path: push is disabled |
| `tagOpt` | `--no-tags` |
| `uploadpack` | `upload-pack` with lazy fetch and replace refs disabled, and missing objects skipped (below) |

There is no `prune`: a branch Claude deletes stays on the developer side until they delete it. If a `claude` remote already exists with another URL, the wrapper leaves it alone and prints where Claude's branches are.

**Fetching from a repository Claude writes.** The developer's git runs `upload-pack` in the copy, outside the sandbox. What Claude can plant there is limited:

- The copy's `config` and `hooks` are write-protected by the sandbox, and git never reads `uploadpack.packObjectsHook` from a repository's own configuration.
- `GIT_NO_LAZY_FETCH=1` and `GIT_NO_REPLACE_OBJECTS=1`: `upload-pack` never contacts the promisor remote, and it sends the real objects, never the placeholders.
- A commit of Claude's can reference a hidden content, whose fingerprint it reads in the trees. That content is missing from the copy, which would make the whole fetch fail, so `pack-objects` runs with `--missing=allow-any` (through a hook set on the command line, the only place git trusts it) and skips it. The developer already has it: it comes from their own history.
- Links planted in the copy's object store can only make `upload-pack` read the developer's own files and send them to the developer's own repository. The wrapper reports the tampering and rebuilds the copy at the next launch.

**What a commit of Claude's can contain.** Claude's commits are reviewed like any contribution. The changes that matter for protected code are handled without a verification step:

| Change in one of Claude's commits | Risk | Counter-measure |
| --- | --- | --- |
| A path removed from `.claudeprotected` | The path would be readable at the next launch. | The removal only takes effect after the developer confirms it (section 5). |
| A hidden content placed at a public path, by its fingerprint | Protected content would surface at a public path. | Removal by fingerprint at every launch, whatever the path. |
| A change under a protected path, a placeholder instead of a real file, a submodule | The developer's code is modified or replaced. | Visible in review. Claude cannot read the protected code it would overwrite. |

**Signing.** The signing key never enters the sandbox. A developer who signs their commits signs Claude's while integrating them: `git cherry-pick -S <branch>..claude/<branch>` copies Claude's new commits after the developer's latest commit and signs each copy, without rewriting any existing commit. A plain merge would only sign the merge commit.

**Rebuilds.** When the copy is rebuilt (protection change, tampering), the previous copy is archived. For each branch holding commits the developer does not have, the wrapper prints the command that fetches it from the archive: `git fetch <archive>/work <branch>`.

## 10. Configuration

Optional, in `~/.config/claude-secure-sandbox/config.json` (or the path in `CLAUDE_SECURE_SANDBOX_CONFIG`). Example (`share/config.example.json`):

```json
{
  "network": { "allowed_domains": ["registry.npmjs.org", "pypi.org", "files.pythonhosted.org"] },
  "filesystem": { "allow_read": ["~/.nvm", "~/.cargo"] },
  "copy_dirs": ["node_modules"]
}
```

Keep `allowed_domains` short: every allowed domain is a place data can be sent to. Never list files that hold tokens (`~/.npmrc`, `~/.netrc`) in `allow_read`.

| Key | Purpose | Default |
| --- | --- | --- |
| `network.allowed_domains` | domains added to the network allowlist | Anthropic domains only |
| `filesystem.allow_read` | extra readable directories (tools installed under `~`) | none |
| `filesystem.allow_write` | extra writable directories; never a directory that programs outside the sandbox run code from, such as a shared package cache | none |
| `copy_dirs` | directories copied from the developer's repository when missing from the copy | none |
| `sync` | items of `~/.claude` copied to the sandbox's Claude configuration | `settings.json`, `CLAUDE.md`, `agents`, `commands`, `skills`, `plugins` |
| `claude_bin`, `srt_bin` | paths of the binaries | detected |
| `state_dir` | wrapper state (also `CLAUDE_SECURE_SANDBOX_HOME`) | `~/.cache/claude-secure-sandbox` |

**Refused configurations.** The launch stops if `allowed_domains` contains `*` or a git host, or if `allow_read` or `allow_write` opens `/`, or a path inside or above the repository, its `.git`, `~/.ssh`, `~/.claude`, `~/.claude.json` or the wrapper's state, directly or through a link.

**`copy_dirs` is a deliberate exception.** These directories are copied as they are, without the filtering applied to git; protected paths, and directories that contain one, are skipped. A file identical to a protected file, or a build directory that contains compiled protected code, would still become readable by Claude. Only list third-party dependencies.

## 11. Security model

Each risk below has a counter-measure, covered by an automated test on macOS.

| Risk | Counter-measure |
| --- | --- |
| Reading a protected file through another tool (shell, script, link, archive, rename) | Protected files are not in the copy, and the developer's repository is unreadable from the sandbox. |
| Reading protected code in git history | Filtered fetch, removal by fingerprint, absence check after every update. |
| A git filter applied incorrectly (silent git failure) | Mandatory absence check; rebuild, then cancellation if it fails again. |
| Reading a protected repository from a session started elsewhere | Every session runs in the sandbox, where only the working directory, the system and the tools are readable. |
| Reading copies of protected code in temp directories (editor files, build outputs, earlier sessions) | `/tmp` and the user's temp directory are unreadable. |
| Fetching the code from the git server | No git host in the network allowlist, no git credentials, no SSH agent, keychain hidden. |
| A commit that references protected content by its fingerprint, so that it surfaces at a public path | Removal by fingerprint at every launch, whatever the path. |
| Removing a path from `.claudeprotected` through a commit merged without noticing | A removal only takes effect after the developer's confirmation in the terminal. |
| Getting a program outside the sandbox to run code (launchd, cron, Apple Events, sockets, shell startup files, `xcrun` cache, git hooks) | Denied by the sandbox profile; `.git/config` and hooks are write-protected in both modes (section 7). |
| Rigging the copy's `.git` so that the wrapper, or the developer's `git fetch`, runs code or skips a check | Locked `.git` paths, hardened git for the wrapper and for the developer's `upload-pack`, structure check and rebuild. |
| Planting a symlink so that the wrapper writes or reads outside its directories | Private temp files, entries replaced rather than written through, regular files only, anchored writable directories. |
| Re-reading old Claude transcripts that contain protected code | Separate Claude configuration; `~/.claude` and other repositories' sessions are unreadable. |
| Adding a hook or an MCP server that runs outside the sandbox | Configuration flows one way, from `~/.claude` to `claude-home`. |
| Stealing the git signing key or keychain credentials | `~/.ssh`, the SSH agent and the keychain are unreachable; only Claude Code's own login is copied in. |

**Accepted and documented**

- Names and sizes of protected files are visible through git, as are commit messages.
- The role of protected code can be inferred from visible code.
- Same kernel and same user as the developer: the strength of the isolation rests on Seatbelt and on `srt`.
- Claude's commits are not verified: what the developer runs after merging them is covered by review.
- Terminal escape sequences reach the developer's terminal.
- Claude can read its own Claude Code login, and send it to allowed domains.

## 12. Code execution (planned)

By default Claude cannot build or test what depends on protected code. A local MCP server, started by the wrapper outside the sandbox, can fill that gap through a controlled channel, without giving Claude a shell on the machine.

```mermaid
flowchart LR
  subgraph sandbox["srt sandbox"]
    claude["Claude Code"]
  end
  subgraph host["Outside the sandbox"]
    mcp["Execution MCP server<br/>closed list of commands"]
    subgraph build["Second sandbox"]
      runner["build / test<br/>protected code readable<br/>no network"]
    end
  end
  claude -- "run test target" --> mcp
  mcp --> runner
  runner -- "raw output" --> mcp
  mcp -- "filtered result" --> claude
```

- A closed list of commands defined by the developer (`build`, `test <target>`), no free-form command, on an explicitly allowed local port.
- Execution in a second sandbox: protected code readable, no network, no credentials, writes limited to a build directory Claude cannot see.
- Filtered output: success or failure, test names, errors located in public files, no excerpt of protected code. Every execution logged and rate-limited.

**Residual risk to accept explicitly.** Builds and tests run code written by Claude with access to protected code. Even filtered, this channel leaks a little information per execution. It covers accidental leaks, not a deliberately manipulated Claude, and must be approved with each client before it is enabled.

## 13. Technical choices

| Choice | Rejected alternative | Reason |
| --- | --- | --- |
| Intercepting the `claude` command | A separate command | Developers keep typing `claude`. |
| Every session sandboxed | Sandboxing protected repositories only | An unsandboxed session started next to a protected repository could read it. Finding every protected repository on the machine takes about 5 s and still misses fresh clones; denying reads by default protects them without knowing them. |
| Reads denied by default, with an allowlist | Denying a list of sensitive areas | A denylist missed `/tmp` and the user's temp directory, which hold copies of files opened outside the sandbox. The allowlist model is also the one `srt` supports on Linux and Windows. |
| `srt` sandbox (Seatbelt, bubblewrap) | Docker container | Native on macOS and Linux: no VM, instant start, the machine's tools are reused, network filtering is built in. Weaker isolation than a VM, accepted. |
| Separate working copy | Claude in the developer's repository, with a filtered `.git` | A shared directory makes `git merge` fail on the current branch and puts the developer's work in progress at risk. |
| `sparse:oid` partial clone and removal by fingerprint | History rewrite (`git filter-repo`) | Same SHAs, so Claude's commits merge as they are; fingerprints cover moved and duplicated files. |
| The developer fetches directly from the copy | A separate repository holding verified copies of Claude's branches | No third repository and no verification step; nothing is lost when a session ends abruptly. |
| The developer's branches under their own names in the copy | A separate namespace (`host/<branch>`) | One kind of branch; the wrapper decides at launch, with the developer, what each branch becomes. |
| Removed protections confirmed in the terminal | Refusing every commit that changes `.claudeprotected` | A check at launch catches a removal however it reached the repository. |
| Claude Code's login copied from the keychain | Opening the keychain to the sandbox | The keychain also holds the credentials of the git server that hosts the protected code. |
| The developer signs Claude's commits when integrating them | A signing key in the sandbox, or signing by the wrapper | A key in the sandbox could leak through the conversation and often authenticates to the git server too. |
| No push to `origin` from the sandbox | Giving Claude git access | Access to the git server would allow cloning the full repository. |
| Separate Claude configuration with one-way sync | Sharing `~/.claude` | Old transcripts can contain protected code, and a writable shared configuration would let Claude add hooks that run outside the sandbox. |
| Python wrapper (3.9+, standard library only) | Bash, Node | Arguments passed as lists, real errors, JSON without `jq`, unit tests. Python 3.9 ships with the Xcode Command Line Tools. |
| `fd`, else `rg`, to find protection files | `find` | About 10 times faster; `rg` is already required by `srt`. |

## 14. Limits and troubleshooting

- No build or test of what depends on protected code until the execution MCP exists (section 12); changes to protected code are made by hand.
- Dependencies (`node_modules`, build outputs) must be installed in the copy, or copied with `copy_dirs`.
- Claude does not see the developer's uncommitted work, nor commits made during the session.
- Literal paths only in `.claudeprotected`, no patterns.
- Windows, submodules and git worktrees are not supported in v1.
- A terminal that accepts clipboard escape sequences (OSC 52) lets any program, Claude included, write to the clipboard: set it to ask first (Ghostty: `clipboard-write = ask`).

### Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| A file is missing from Claude's copy | It is protected, or its content is identical to a protected file (listed at launch). |
| Claude cannot see the latest changes | Uncommitted changes are not copied, and commits made during a session appear at the next launch. |
| A tool installed in the home directory, or in an unusual place, fails | Add its directory to `filesystem.allow_read`. System directories, the directories of the `PATH` outside the home directory, and the installations of git, node, python3 and rg are readable automatically. |
| A package registry or API is unreachable | Add the domain to `network.allowed_domains`. |
| `git push` or `git pull` fails inside the session | Expected: the sandbox has no git credentials and no access to git hosts. Claude's work is fetched with `git fetch claude`. |
| `git fetch claude` shows an unknown branch | Claude created it in its copy. |
| `claude` is refused in a directory | It holds the home directory or another volume, or a protected repository further down: run `claude` from the project directory. |
| Linux: the sandbox fails with "Operation not permitted" | bubblewrap cannot create user namespaces: on Ubuntu 24.04 and later, set `kernel.apparmor_restrict_unprivileged_userns=0` or add an AppArmor profile for `bwrap`. |
| Linux: `.bashrc`, `.gitconfig` or `.vscode` appear in the directory during a session | `srt`'s stand-ins for the files it write-protects (section 7); they disappear when the session ends, and the sandbox's git ignores them. |

## 15. Roadmap

### v1: done

- [x] Wrapper in Python 3.9+, `srt` pinned to 0.0.78.
- [x] Filtered copy: `sparse:oid` partial clone, removal by fingerprint, absence check, placeholders in `git log -p`, `show` and `blame`.
- [x] `claude` remote pointing to the copy; branch choice at a new session; nothing lost on abrupt ends.
- [x] Removed protections kept until the developer confirms.
- [x] Every session sandboxed (protected and directory modes), with reads denied by default.
- [x] Escape attempts covered by the tests.
- [x] Claude Code's login copied from the keychain, without opening the keychain.
- [x] Linux (bubblewrap), with the end-to-end suite run in Docker (`npm run test:linux`).
- [x] Separate Claude configuration, synced one way.
- [x] End-to-end tests: 190 on macOS, 186 on Linux.

### Next: validation on a real machine

- [ ] Interactive sessions of the real Claude Code, in both modes, with the developer's tools.
- [ ] Linux distributions beyond Debian: Ubuntu 24.04 (AppArmor), Fedora, NixOS.
- [ ] Independent security review of the current design (the previous review covered the verification-based design).
- [ ] Full network allowlist for an interactive session (remote MCP servers, package registries).
- [ ] Timings on a large repository: copy creation, removal by fingerprint, update, history of `.claudeprotected`.

### Hardening

- [ ] End the processes a session leaves behind (they stay confined, but keep writing to the copy).
- [ ] Per-session caches for package managers, instead of opening shared caches with `allow_write`.
- [ ] Exclude fingerprinted blobs at the source, in the developer's `upload-pack`, so that they never reach the copy's disk.

### Execution MCP outside the sandbox

- [ ] HTTP MCP server started by the wrapper, closed list of commands, second sandbox, filtered output, logging and rate limiting (section 12).
- [ ] Approval process with each client before enabling it.

### Later

- [ ] Exceptions in `.claudeprotected` (`!path`), confirmed like a removal.
- [ ] Submodules and git worktrees.
- [ ] Distribution through Homebrew.
- [ ] Windows.

## 16. On-disk layout

Per repository, in `~/.cache/claude-secure-sandbox/<repo>-<hash>/`:

| Path | Content | Access from the sandbox |
| --- | --- | --- |
| `work/` | filtered copy, Claude's workspace (protected mode) | read, write |
| `tmp/` | session `TMPDIR` | read, write |
| `run/` | `srt-settings.json`, `system-prompt.md`, `gitconfig`, `bin/git` | read |
| `state/spec.git` | sparse filter specification | none |
| `state/protected-blobs` | fingerprints of every protected content seen in history | none |
| `state/host-tips` | developer commits already fetched and scanned | none |
| `state/protect-hash` | hash of the protected list | none |
| `state/reported-absent` | public files already reported as absent | none |
| `state/repository` | path of the developer's repository, for `uninstall.sh` | none |
| `state/lock`, `state/tmp` | session lock, wrapper temp files | none |
| `archive/<date>-<reason>/` | previous copies and sessions, after a rebuild | none |

In the developer's repository: the `claude` remote in `.git/config`, and the confirmed removals of protection in `.git/claude-secure-sandbox/unprotected`.

Shared by every repository: `~/.cache/claude-secure-sandbox/claude-home`, the sandbox's Claude configuration and login (`CLAUDE_CONFIG_DIR`). Each repository's sessions there are unreadable from the other repositories' sandboxes.

## 17. Code layout and tests

```
bin/claude                                  wrapper entry point (Python 3.9+)
bin/claude-unsandboxed                      real binary, without the sandbox
claude_secure_sandbox/cli.py                interception, session modes, main flow
claude_secure_sandbox/common.py             configuration, hardened git on the copy, lock, prompts
claude_secure_sandbox/context.py            paths and git command lines of one run
claude_secure_sandbox/protect.py            reading .claudeprotected, removed protections
claude_secure_sandbox/filtered_copy.py      filtered copy: fetch, fingerprints, placeholders
claude_secure_sandbox/session.py            claude remote, branches at launch, end of session summary
claude_secure_sandbox/sandbox.py            read allowlist, srt settings, Claude configuration and login, launch
libexec/git-remote-csnull                   promisor remote that serves no object
share/prompts/protected-mode.md             prompt for protected mode
share/prompts/directory-mode.md             prompt for directory mode
share/config.example.json                   configuration example
install.sh                                  dependency check and npm ci
uninstall.sh                                removes remotes, confirmations, state and configuration
tests/run-tests.sh                          end-to-end tests
tests/test_unit.py                          unit tests of the pure functions
tests/linux/Dockerfile                      Linux test environment (Debian, bubblewrap)
```

Run the tests with `npm test` (`jq` is needed by the end-to-end suite), and on Linux in Docker with `npm run test:linux`. The unit tests cover `.claudeprotected` validation, path helpers and URL parsing. The end-to-end tests create a fake `HOME`, a test repository, a fake keychain and a fake `claude` binary that runs scenarios inside the real `srt` sandbox. They cover:

- blocked reads (developer repository, `~/.ssh`, `~/.claude`, wrapper state, `/tmp`, the user's temp directory, `/Applications`), a locked `.git`, and network filtering;
- escape attempts: `launchctl`, `crontab`, `at`, `open`, Apple Events, clipboard, Unix sockets, localhost, `~/.zshrc`, LaunchAgents, the `xcrun` cache, and a detached process outliving the session;
- no protected content in objects or on disk, including moved files, identical copies and branches merged with `-s ours`; placeholders; absent public files reported once;
- `git fetch claude` and `git pull`, a commit referencing hidden content, signing by the developer's rebase;
- the branch choice at a new session (Claude ahead, integrated after a rebase, diverged with each answer), the developer's other branches (kept, updated, deleted), resume;
- removed protections (no terminal, refused, confirmed, protected again), `.claudeprotected` additions, tampering with `.git`, planted links;
- directory mode, the home directory refused, Claude Code's login and temp root, the configuration passed through read only and the MCP servers copied, launch cases and refused configurations;
- `uninstall.sh`: unfetched branches reported, nothing removed when refused, remote, confirmations, state and configuration removed, the repository left intact.

Tested on macOS 27 (git 2.50, Claude Code 2.1.286) and Debian 12 in Docker on arm64 (Claude Code 2.1.301 started in the sandbox), with srt 0.0.78.
