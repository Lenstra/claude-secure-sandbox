# claude-secure-sandbox

Use Claude Code on repositories that contain code it must never read. You list the sensitive paths in a `.claudeprotected` file and keep typing `claude`: the operating system guarantees that Claude only sees the rest, in files and in git history alike, and Claude still commits.

- **Enforced by the OS, not by the model.** Every `claude` session runs in an OS sandbox ([`srt`](https://www.npmjs.com/package/@anthropic-ai/sandbox-runtime): Seatbelt on macOS, bubblewrap on Linux). Protected content is not on Claude's disk at all, so no tool, script or `git show` can reach it, even if a prompt injection tells Claude to try.
- **Git keeps working.** Claude works in a filtered copy of your repository with the same commits and SHAs. It reads history, commits on your branch, and you fetch its work with `git fetch claude`.
- **Nothing else on the machine.** Claude reads its working directory, the system and the installed tools. Your home directory, other projects, temp directories, SSH keys and keychain stay out of reach, in every session.
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
git clone git@github.com:Lenstra/claude-secure-sandbox.git
cd claude-secure-sandbox
./install.sh
```

Put the `bin` directory first on your `PATH`, in `~/.zshrc`, then check that `claude` resolves to the wrapper:

```bash
export PATH="$HOME/path/to/claude-secure-sandbox/bin:$PATH"
command -v claude   # .../claude-secure-sandbox/bin/claude
```

Log in to Claude Code as usual (`claude-unsandboxed`, then `/login`). At each launch, the wrapper copies Claude Code's keychain entry, and only it, into the sandbox; the rest of your keychain stays unreadable.

## Installation on Linux

Requirements: git, Python 3.9 or later, Node.js 20 or later, `ripgrep`, `bubblewrap` and `socat`. `fd` is optional and speeds up launch.

```bash
sudo apt-get install git python3 nodejs npm ripgrep bubblewrap socat fd-find   # Debian, Ubuntu
sudo dnf install git python3 nodejs npm ripgrep bubblewrap socat fd-find       # Fedora
sudo pacman -S git python nodejs npm ripgrep bubblewrap socat fd                # Arch
git clone git@github.com:Lenstra/claude-secure-sandbox.git
cd claude-secure-sandbox
./install.sh
```

On Ubuntu 24.04 and later, bubblewrap also needs user namespaces with capabilities:

```bash
sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0
```

or an AppArmor profile that grants `userns` to `bwrap`.

Put the `bin` directory first on your `PATH`, in `~/.bashrc` or `~/.zshrc`, then check that `claude` resolves to the wrapper:

```bash
export PATH="$HOME/path/to/claude-secure-sandbox/bin:$PATH"
command -v claude   # .../claude-secure-sandbox/bin/claude
```

Log in to Claude Code as usual (`claude-unsandboxed`, then `/login`). At each launch, the wrapper copies `~/.claude/.credentials.json`, and only it, into the sandbox.

System directories (`/usr`, `/lib`, `/etc`, `/opt`, and `/nix/store`, `/snap` or Linuxbrew when present) and the directories of your `PATH` outside your home directory are readable in the sandbox. If your distribution puts tools elsewhere, add them to `filesystem.allow_read` ([configuration](docs/ARCHITECTURE.md#10-configuration)).

## Usage

The real binary stays available as `claude-unsandboxed`, for the rare session that must run without the sandbox.

### Protect a repository

Commit a `.claudeprotected` file at the repository root, one path per line:

```
# a directory: everything below it, at any depth
core/engine/
# a single file
licensing/keys.go
```

- Paths are relative to the root. A directory protects everything below it, at any depth; the trailing `/` is optional.
- Literal paths only: no `*`, `?`, `[` or `!`. Absolute paths, `..`, `.git` and the file itself are refused.
- Lines starting with `#` are comments; a `#` after a path is part of the path.

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

3. **Fetch and review.** The `claude` remote was added to your repository at the first launch; it points to Claude's copy, and push to it is disabled. Review Claude's commits like any contribution: build scripts, `package.json` or CI changes run on your machine or your CI once merged.
4. **Integrate.** Two cases, depending on whether you sign your commits:
   - **You do not sign them:** `git rebase claude/feature-x`. If you have not committed on `feature-x` in the meantime, your branch simply moves forward to Claude's last commit; otherwise your new commits are replayed after Claude's.
   - **You sign them:** your signing key never enters the sandbox, so Claude's commits are unsigned. `git cherry-pick -S feature-x..claude/feature-x` copies Claude's commits that are not in your branch yet after your latest commit, and signs each copy. It never rewrites a commit you already have, pushed or not.

   In both cases there is no merge commit; resolve conflicts, if any, as usual with git.
5. **Push.** History on `origin` stays linear, without merge commits. With the rebase, commits of yours that were already pushed and come after Claude's are rewritten: push them with `--force-with-lease`. The cherry-pick never rewrites anything, so its push is always a normal one. CI builds and tests with the full code, protected paths included.
6. **Next session.** The wrapper sees that Claude's commits are in your branch, even as signed copies, and Claude starts from your latest commit. If you run `claude` before integrating them, Claude continues on its own commits; if both of you committed in between, the wrapper asks what to do ([branches at a new session](docs/ARCHITECTURE.md#branches-at-a-new-session)).

`claude --continue` and `claude --resume` resume the previous conversation, with the copy as Claude left it.

## Uninstallation

`./uninstall.sh` changes nothing: it lists what to remove on your machine, with the exact paths and commands. Remove, in this order:

1. **The PATH line** you added to `~/.zshrc` or `~/.bashrc` (`export PATH=".../claude-secure-sandbox/bin:$PATH"`), then open a new shell: `claude` runs Claude Code directly.
2. **In each repository you used it in**, the `claude` remote and your confirmed removals of protection. Fetch Claude's branches first if you want to keep them; the script lists the ones you never fetched.

   ```bash
   git remote remove claude
   rm -rf .git/claude-secure-sandbox
   ```

3. **The wrapper's state**: Claude's copies, archives and sessions, and its copy of your Claude Code login.

   ```bash
   rm -rf ~/.cache/claude-secure-sandbox
   ```

4. **The configuration**, if you created one.

   ```bash
   rm -rf ~/.config/claude-secure-sandbox
   ```

5. **The tool itself**: the `claude-secure-sandbox` directory.

Claude Code itself is not affected.

## Documentation

Everything else is in the [architecture document](docs/ARCHITECTURE.md):

- [what Claude sees](docs/ARCHITECTURE.md#what-claude-sees) in its copy, and why some public files are absent;
- [what happens to the branches](docs/ARCHITECTURE.md#branches-at-a-new-session) when you run `claude` again, and the question asked when your branch and Claude's have diverged;
- [adding or removing a protected path](docs/ARCHITECTURE.md#5-the-claudeprotected-file);
- [sessions outside protected repositories](docs/ARCHITECTURE.md#4-interception-and-session-modes);
- [configuration](docs/ARCHITECTURE.md#10-configuration), [troubleshooting](docs/ARCHITECTURE.md#troubleshooting) and [limits](docs/ARCHITECTURE.md#14-limits-and-troubleshooting).

Tests: `npm test` (unit tests, then end-to-end tests in the real sandbox; `jq` required), and `npm run test:linux` for the same suite on Linux, in Docker.
