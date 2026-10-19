# CLAUDE.md

claude-secure-sandbox is a `claude` wrapper that runs Claude Code inside an OS sandbox (`srt`: Seatbelt on macOS, bubblewrap on Linux) so that Claude never reads code it must not see. How it works is in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md); this file holds the rules the project keeps. A change that breaks one of them needs a reason written in the commit, and a test.

## Security rules

1. **Impossible by construction, not by convention.** A protection rests on the kernel sandbox and on what the wrapper computes before the session, never on Claude following an instruction, and never on a test of one tool at a time.
2. **Allowlists everywhere.** What Claude sees in a protected repository (`[allowed-git]`), reads outside it (`[allowed-external]`) and reaches on the network (`[allowed-domain]`) is listed; everything else is closed. The user areas (home directories, other disks, temp directories) are closed by default.
3. **Fail closed.** When the wrapper cannot tell, it hides or refuses: a repository git cannot read counts as protected, a scan that does not finish stops the launch, a missing seccomp helper on Linux stops the launch, no terminal means nothing new is opened.
4. **Every widening is confirmed by the developer in the terminal, before Claude starts.** Claude can edit `.claudesandbox`, so a new entry only takes effect once confirmed; narrowing never asks; a confirmation ends when its entry leaves the file.
5. **Protection comes from the disk and from git only.** A directory is protected if it holds a `.claudesandbox`, or if it is a repository that ever committed one at its root, on any branch or tag; its git directory too, wherever it lives (submodule, `--separate-git-dir`, bare clone). Once protected, a repository stays hidden from every other session.
6. **Only files tracked by git reach Claude** in a protected repository: untracked and ignored files (`.env`, `node_modules`, build outputs) and uncommitted changes never do. A directory outside git cannot be protected.
7. **History is filtered by path, then by exact content.** Every blob ever under a hidden path is removed from the copy wherever it appears, and the copy is checked after every update; a failed check rebuilds it, a second failure cancels the session.
8. **Every protected repository of the computer is hidden from every session**, neither readable nor writable. A session never starts inside one (its `.git`, a repository nested in it), nor inside credentials, `~/.claude` or the wrapper's state.
9. **Nothing outside the sandbox acts for Claude.** No local socket, no local network destination (any spelling of this computer is refused), no Apple Events, only a fixed list of system Mach services. By default the network reaches Claude's API and login only: no git host, no `mcp-proxy.anthropic.com`.
10. **Nothing in the environment makes the wrapper skip the sandbox.** A file Claude writes (an `.envrc`) could set it. Inside a session, `claude` on the `PATH` is the real binary, already confined. The only test hooks are `CLAUDE_SECURE_SANDBOX_HOME` and `CLAUDE_SECURE_SANDBOX_SCAN_ROOT`, the latter honored only for a self-contained test tree and announced at each launch.
11. **Everything Claude can write is untrusted.** The wrapper's git on the copy runs with hooks, fsmonitor, replace refs and protocols disabled on the command line; the developer's `GIT_*` variables are ignored (except where the git configuration lives); a link Claude planted is replaced, never followed (`install_file`, `regular_file`, `ensure_real_dir`); the wrapper's own temp files never live where Claude writes.
12. **What a program outside the sandbox would run stays locked:** `.git/config`, hooks and the other locked `.git` paths, `.claude/settings.json` and `.claude/settings.local.json`, and srt's own list (`.vscode`, `.mcp.json`, shell startup files...), from the root of the working directory. On Linux, a locked file absent at launch that appears during the session is set aside afterwards.
13. **The developer's Claude configuration is read only.** `~/.claude` items are linked; `settings.json` is a write-locked copy; nothing is copied back. Each directory has its own `claude-home`, so sessions of different repositories never share anything.

## The wrapper's state

- It lives in `~/.cache/claude-secure-sandbox/<directory>-<hash>/`: the copy (`work/`), the session's `tmp/`, `run/`, `claude-home/`, `state/` and `archive/`.
- It holds confirmations (`state/granted`) and the copy's bookkeeping, never a source of protection: deleting it hides nothing less, it only asks the confirmations again.
- It is out of every sandbox's reach, except the session's own `work/`, `tmp/`, `run/` and `claude-home/`.
- There is no configuration file, in the home directory or elsewhere: what a session may reach is decided by the tool and by each repository's `.claudesandbox`.
- One session per directory, through an atomic lock.

## Repositories

- **Protected repository:** Claude works on the filtered copy and never touches the developer's repository; the developer retrieves Claude's branches with `git fetch claude` and reviews them. Claude can create files at new paths; once merged, they are hidden until listed.
- **Anywhere else:** Claude works in the directory itself, with the locked paths of rule 12. A session started in the home directory or above is read only.
- **Refused:** starting from a hidden path, from inside another protected repository, from credentials, `~/.claude` or the wrapper's state; a `.claudesandbox` outside a git repository; an `[allowed-external]` or `[allowed-domain]` entry that overlaps what nothing may open.
- A repository that was protected stays protected until the developer, in a terminal, confirms it no longer is.

## Code

- Python 3.9 or later, standard library only; `srt` pinned in `package.json`.
- Everything in English: code, comments, messages, documentation. No em-dash character anywhere.
- Comments and docstrings say what the code does now and why, never what an earlier version did.
- Use the shared helpers instead of rewriting them: `common.Git` and `git_dev` / `git_copy` / `git_copy_worktree` for git, `command_output` for other commands, `fs_within` / `overlaps` for paths on disk (case-insensitive on macOS), `Context.protected_code` and `access.credential_paths()` for what must stay hidden.
- One module per question: `scan.py` decides what is protected, `sandbox_file.py` reads `.claudesandbox` and keeps confirmations, `access.py` decides what a session may reach, `sandbox.py` writes the `srt` rules, `filtered_copy.py` and `session.py` build and update the copy, `cli.py` orders the launch.

## Tests

- Every fix comes with a regression test; every security rule has a test that fails if the rule is broken.
- A check must fail for the right reason: a `tn` on a path the session could otherwise read, objects read without placeholders (`blobs_contain`), a tool that is missing reported as skipped, never as blocked.
- Before pushing: `python3 tests/test_unit.py`, `bash tests/run-tests.sh` (macOS), `npm run test:linux` (Linux in Docker), `python3 tests/tui-smoke.py` (the real Claude Code in a terminal), pyflakes, shellcheck, actionlint.
- CI runs the quick checks first, then macOS and Ubuntu (x64 and arm64) in parallel.

## Commits

- Conventional format: `feat:`, `fix:`, `docs:`, `test:`, `ci:`, `refactor:`, then a lowercase summary; the body says what changed and why.
- Commits are signed with the developer's key. No `Co-Authored-By` or "Generated with" line.
