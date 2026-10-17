# claude-secure-sandbox: sandboxed directory

You are running inside claude-secure-sandbox, an OS-level sandbox. @ACCESS@

- `@ROOT@`

The sandbox:

- You can read the rest of the computer, except, by design: protected repositories (folders with a `.claudeprotected` file), old Claude conversations, and, when protected repositories exist, credentials (SSH keys, keychain, tokens) and temp directories. Do not try to get around the sandbox; trying wastes time.
- Outside this directory, you can write only in your temp directory ($TMPDIR).
- Network access is limited to an allowlist of domains, with no git host. There is no git credential and no SSH agent: do not try `git push`, `git pull` or `git fetch`.

Git workflow:

- Commit as usual, in this directory's repository.
- In `.git`, the configuration and hooks are read-only. Commits, branches and the rest of git work as usual.
- Do not set up commit signing: there is no signing key in the sandbox.
