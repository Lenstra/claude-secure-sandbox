# claude-secure-sandbox

Use Claude Code on repositories that contain code it must never read. You list in a `.claudesandbox` file what Claude may see, and keep typing `claude`: the operating system guarantees that Claude sees nothing else, in files and in git history alike, and Claude still commits.

- **Enforced by the OS, not by the model.** Every `claude` session runs in an OS sandbox ([`srt`](https://www.npmjs.com/package/@anthropic-ai/sandbox-runtime): Seatbelt on macOS, bubblewrap on Linux). Protected content is not on Claude's disk at all, so no tool, script or `git show` can reach it, even if a prompt injection tells Claude to try.
- **Git keeps working.** Claude works in a filtered copy of your repository with the same commits and SHAs. It reads history, commits on your branch, and you fetch its work with `git fetch claude`.
- **Your other files stay hidden too.** Claude reads the system and the tools on your `PATH`, but nothing else in your home folder, on other disks or in temp files, unless a repository asks for it and you confirm. Other protected repositories, your credentials and your old Claude conversations are hidden. Claude writes only in its working directory and its temp directory.
- **Native, no Docker.** No VM and no image: sessions start instantly, with Claude Code as installed on the machine.

How it works and why it is built this way: [architecture document](docs/ARCHITECTURE.md).

## Contents

1. [Installation on macOS](#installation-on-macos)
2. [Installation on Linux](#installation-on-linux)
3. [Usage](#usage)
4. [Uninstallation](#uninstallation)
5. [Documentation](#documentation)

## Installation on macOS

Requirements: git, Python 3.9 or later (the one of the Xcode Command Line Tools works), Node.js 20 or later and `ripgrep`. `fd` is optional and speeds up launch.

```bash
brew install ripgrep node fd
git clone git@github.com:Lenstra/claude-secure-sandbox.git ~/.local/share/claude-secure-sandbox
cd ~/.local/share/claude-secure-sandbox
./install.sh
```

> [!IMPORTANT]
> The cloned directory is the tool itself: keep it in a permanent place, such as `~/.local/share/claude-secure-sandbox`. Not in `Downloads`, a temporary directory or a folder synced by iCloud (`~/Documents` often is). If it is moved or deleted, `claude` silently runs Claude Code without the sandbox. If you cloned it elsewhere, `install.sh` offers to move it first.

Then, as `install.sh` tells you with your own paths:

1. **Put the wrapper first on your `PATH`.** The `bin` directory holds the wrapper's two commands, `claude` and `claude-unsandboxed`. It must come before the directory of the real `claude` in your `PATH`. Add this line at the end of your shell startup file (`.zshrc` or `.zprofile` for zsh, `.bashrc` for bash):

   ```bash
   export PATH="$HOME/.local/share/claude-secure-sandbox/bin:$PATH"
   ```

2. **Check**, in a new shell, that both commands come from the wrapper:

   ```bash
   command -v claude               # .../claude-secure-sandbox/bin/claude
   command -v claude-unsandboxed   # .../claude-secure-sandbox/bin/claude-unsandboxed
   ```

3. **Log in** to Claude Code if you have not yet: `claude-unsandboxed`, then `/login`. At each launch, the wrapper copies Claude Code's keychain entry, and only it, into the sandbox; the rest of your keychain stays hidden.

> [!IMPORTANT]
> If `command -v claude` does not point to the wrapper, `claude` runs Claude Code without any sandbox. Check it again after updating Claude Code or changing your `PATH`.

## Installation on Linux

Requirements: git, Python 3.9 or later, Node.js 20 or later, `ripgrep`, `bubblewrap` and `socat`. `fd` is optional and speeds up launch.

```bash
sudo apt-get install git python3 nodejs npm ripgrep bubblewrap socat fd-find   # Debian, Ubuntu
sudo dnf install git python3 nodejs npm ripgrep bubblewrap socat fd-find       # Fedora
sudo pacman -S git python nodejs npm ripgrep bubblewrap socat fd                # Arch
git clone git@github.com:Lenstra/claude-secure-sandbox.git ~/.local/share/claude-secure-sandbox
cd ~/.local/share/claude-secure-sandbox
./install.sh
```

> [!IMPORTANT]
> The cloned directory is the tool itself: keep it in a permanent place, such as `~/.local/share/claude-secure-sandbox`. Not in `Downloads`, a temporary directory or a synced folder. If it is moved or deleted, `claude` silently runs Claude Code without the sandbox. If you cloned it elsewhere, `install.sh` offers to move it first.

> [!NOTE]
> On Ubuntu 24.04 and later, bubblewrap also needs user namespaces with capabilities: run `sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0`, or add an AppArmor profile that grants `userns` to `bwrap`.

Then, as `install.sh` tells you with your own paths:

1. **Put the wrapper first on your `PATH`.** The `bin` directory holds the wrapper's two commands, `claude` and `claude-unsandboxed`. It must come before the directory of the real `claude` in your `PATH`. Add this line at the end of your shell startup file (`.bashrc` for bash, `.zshrc` or `.zprofile` for zsh):

   ```bash
   export PATH="$HOME/.local/share/claude-secure-sandbox/bin:$PATH"
   ```

2. **Check**, in a new shell, that both commands come from the wrapper:

   ```bash
   command -v claude               # .../claude-secure-sandbox/bin/claude
   command -v claude-unsandboxed   # .../claude-secure-sandbox/bin/claude-unsandboxed
   ```

3. **Log in** to Claude Code if you have not yet: `claude-unsandboxed`, then `/login`. At each launch, the wrapper copies `~/.claude/.credentials.json`, and only it, into the sandbox.

> [!IMPORTANT]
> If `command -v claude` does not point to the wrapper, `claude` runs Claude Code without any sandbox. Check it again after updating Claude Code or changing your `PATH`.

## Usage

Every `claude` session runs in the sandbox, and shows it. At each launch, the wrapper first looks for protected repositories on your computer (`looking for protected repositories on this computer`, a few seconds, with a percentage estimated from the previous launch), so that it can hide them. Then a screen says what applies here, what Claude will not see and what happens to your files; press Enter to start, or Ctrl-C to cancel:

```
🔒 claude-secure-sandbox

This repository is protected: Claude only sees what its .claudesandbox lists.

   Hidden from Claude:  core/engine, licensing/keys.go
   Claude works on:     a copy of this repository, branch feature-x
   Your files:          untouched; get Claude's commits with: git fetch claude
   Claude can read:     the system and the tools on your PATH, not your other files

Press Enter to start Claude, or Ctrl-C to cancel:
```

Where nothing is protected, the screen starts with `Nothing is protected here: Claude works directly in this folder.` and Claude edits that folder directly. Your other files stay hidden as well, and protected repositories too, including one inside that folder.

While Claude runs, the bottom line of Claude Code shows `🔒 sandboxed · protected repository my-repo · feature-x` (unless you already set your own status line).

If you see neither the screen nor that line, `claude` is not going through the sandbox: check your `PATH`.

When a script runs Claude for you (for example `claude -p "..."` in a CI job), nobody is there to press Enter, so the screen is skipped and Claude starts right away.

> [!NOTE]
> If you run `claude` directly in your home folder (or in `/`), Claude can look at your files but cannot change any of them. It will show you the changes instead, and you apply them yourself.
>
> Why: some files in your home folder are run automatically outside the sandbox, such as `.zshrc` when you open a terminal. If Claude could edit them, it could make your computer run commands outside the sandbox.
>
> To let Claude edit files, run `claude` in a project folder instead.

The real binary stays available as `claude-unsandboxed`, for the rare session that must run without the sandbox.

> [!WARNING]
> `claude-unsandboxed` runs Claude Code with no protection at all. Claude can then read every protected repository on your machine, its files and its git history, and anything else your user account can read, even when you start it from another directory.

### The `.claudesandbox` file

To protect a repository, commit a `.claudesandbox` file at its root. Its presence is what protects the repository. It has three sections, all optional:

```
[allowed-git]
# everything else is hidden from Claude
src/
tests/
package.json

[allowed-external]
~/.m2

[allowed-domain]
registry.npmjs.org
```

- **`[allowed-git]`**: the only paths Claude may see in the repository, as committed. Everything else is hidden, including what you add later. Paths are relative to the root; a directory shows everything below it. Literal paths only: no `*`, `?`, `[` or `!`. `.` shows the whole repository; an empty file hides all of it. Without the file, nothing is hidden: Claude works directly in your folder.
- **`[allowed-external]`**: what Claude may read outside the repository, such as an SDK or a package cache. Absolute or `~/...`, read only.
- **`[allowed-domain]`**: the network domains Claude may reach beyond Claude's own API and login, such as a package registry. The claude.ai connectors (`mcp-proxy.anthropic.com`) are closed by default: a GitHub connector would read your protected code on GitHub.

Protection rests on git: Claude only ever gets what is committed. Files git does not track never reach it, even under a listed path: untracked and ignored files (`.env`, `node_modules`, build outputs) and uncommitted changes. Claude installs dependencies itself in its copy, from a registry listed under `[allowed-domain]`. A folder that is not a git repository cannot be protected: `git init`, then commit what Claude may see.
- Lines starting with `#` are comments. A `#` after an entry is part of it: put comments on their own line.

Every section is an allowlist: what is not listed stays closed. The first time an entry appears, the wrapper asks you before Claude starts:

```
claude-secure-sandbox: .claudesandbox asks for access Claude does not have by default:
  domain registry.npmjs.org
claude-secure-sandbox: allow it? [y/N]
```

Claude can edit this file, so nothing it adds there is granted without your answer. Removing an entry, or hiding more, asks nothing. Some entries are always refused: git hosts, this computer (`localhost`), and paths that hold credentials, temp files or other protected code.

There is no other configuration: the wrapper does not read any settings file in your home folder.

That is all the setup a repository needs. Everything else is created by the wrapper, on your machine only.

### Daily workflow

A complete cycle, on a feature branch:

```bash
git switch -c feature-x main                    # 1. create your branch
claude                                          # 2. Claude works on feature-x, in its copy
git fetch claude                                # 3. fetch Claude's commits and review them
git log --oneline feature-x..claude/feature-x
git diff feature-x...claude/feature-x
                                                # 4. bring them into your branch, either:
git rebase claude/feature-x                     #    if you do not sign your commits
git cherry-pick -S feature-x..claude/feature-x  #    if you sign your commits
git push origin feature-x                       # 5. push, then open a pull request
claude                                          # 6. next session, from your latest commit
```

1. **Your branch.** Claude always works on the branch you are on when you run `claude`.
2. **The session.** The wrapper updates Claude's filtered copy and puts it on `feature-x`. Claude reads the code, runs commands and commits there; you can keep working in your repository meanwhile. When the session ends, the wrapper tells you what there is to fetch, with the command of step 4 that fits your configuration (here, signed commits):

   ```
   claude-secure-sandbox: end of session
     feature-x: 3 new commit(s)
     next: git fetch claude, then on feature-x: git cherry-pick -S feature-x..claude/feature-x
   ```

3. **Fetch and review.** The `claude` remote was added to your repository at the first launch; it points to Claude's copy, and push to it is disabled.
4. **Integrate.** Two cases, depending on whether you sign your commits:
   - **You do not sign them:** `git rebase claude/feature-x`. If you have not committed on `feature-x` in the meantime, your branch simply moves forward to Claude's last commit; otherwise your new commits are replayed after Claude's.
   - **You sign them:** your signing key never enters the sandbox, so Claude's commits are unsigned. `git cherry-pick -S feature-x..claude/feature-x` copies Claude's commits that are not in your branch yet after your latest commit, and signs each copy. It never rewrites a commit you already have, pushed or not.

   In both cases there is no merge commit; resolve conflicts, if any, as usual with git.
5. **Push.** History on `origin` stays linear, without merge commits. With the rebase, commits of yours that were already pushed and come after Claude's are rewritten: push them with `--force-with-lease`. The cherry-pick never rewrites anything, so its push is always a normal one. CI builds and tests with the full code, hidden paths included.
6. **Next session.** The wrapper sees that Claude's commits are in your branch, even as signed copies, and Claude starts from your latest commit. If you run `claude` before integrating them, Claude continues on its own commits; if both of you committed in between, the wrapper asks what to do ([branches at a new session](docs/ARCHITECTURE.md#branches-at-a-new-session)).

`claude --continue` and `claude --resume` resume the previous conversation, with the copy as Claude left it.

> [!IMPORTANT]
> Review Claude's commits like any contribution before integrating them. Build scripts, `package.json` or CI changes run on your machine or your CI once merged, outside the sandbox. Outside a protected repository, Claude writes in your folder directly: a dev server, a file watcher or an IDE extension running on that folder runs what Claude writes there, outside the sandbox.

## Uninstallation

`./uninstall.sh` changes nothing: it lists what to remove on your machine, with the exact paths and commands. Remove, in this order:

1. **The PATH line** you added to your shell startup file (`export PATH=".../claude-secure-sandbox/bin:$PATH"`), then open a new shell: `claude` runs Claude Code directly.
2. **In each repository you used it in**, the `claude` remote.

   ```bash
   git remote remove claude
   ```

3. **The wrapper's state**: Claude's copies, archives and sessions, what you confirmed Claude may see and reach, and its copy of your Claude Code login.

   ```bash
   rm -rf ~/.cache/claude-secure-sandbox
   ```

4. **The tool itself**: the `claude-secure-sandbox` directory.

Claude Code itself is not affected.

> [!CAUTION]
> Deleting the wrapper's state deletes the Claude branches you never fetched. Fetch them first (`git fetch claude`) if you want to keep them; `uninstall.sh` lists them.

## Documentation

Everything else is in the [architecture document](docs/ARCHITECTURE.md):

- [what Claude sees](docs/ARCHITECTURE.md#what-claude-sees) in its copy, and why some visible files are absent;
- [what happens to the branches](docs/ARCHITECTURE.md#branches-at-a-new-session) when you run `claude` again, and the question asked when your branch and Claude's have diverged;
- [the `.claudesandbox` file](docs/ARCHITECTURE.md#5-the-claudesandbox-file): what Claude sees in the repository, opening a path or a domain;
- [sessions outside protected repositories](docs/ARCHITECTURE.md#4-interception-and-sessions), and [what Claude can read on your computer](docs/ARCHITECTURE.md#reading-an-allowlist-where-users-files-live);
- [why there is no configuration file](docs/ARCHITECTURE.md#10-configuration), [troubleshooting](docs/ARCHITECTURE.md#troubleshooting) and [limits](docs/ARCHITECTURE.md#14-limits-and-troubleshooting).

Tests: `npm test` (unit tests, then end-to-end tests in the real sandbox; `jq` required), `npm run test:linux` for the same suite on Linux, in Docker, and `python3 tests/tui-smoke.py` for the real Claude Code in a terminal. The CI runs them all on macOS and Linux.
