# claude-secure-sandbox: sandboxed directory

You are running inside claude-secure-sandbox, an OS-level sandbox. You work directly in this directory, and can read and write it:

- `@ROOT@`

The sandbox:

- The rest of the machine is unreadable by design: the home directory, other projects, temp directories, credentials. System directories and installed tools can be read and run. Do not try to get around the sandbox; trying wastes time.
- Network access is limited to an allowlist of domains, with no git host. There is no git credential and no SSH agent: do not try `git push`, `git pull` or `git fetch`.

Git workflow:

- Commit as usual, in this directory's repository.
- In `.git`, the configuration and hooks are read-only. Commits, branches and the rest of git work as usual.
- Do not set up commit signing: there is no signing key in the sandbox.
