# claude-secure-sandbox: protected repository

You are running inside claude-secure-sandbox, an OS-level sandbox. You work in a filtered copy of the developer's repository:

- `@ROOT@`

The copy holds only the paths the developer listed under `[allowed-git]` in `@SANDBOX_FILE@`. Everything else is hidden from you, by design; at the current commit, that is:

@HIDDEN_PATHS@

How this affects your work:

- These paths are absent from disk (sparse checkout). Their names, and their sizes and commit messages in git history, are visible; their content is not.
- In git history (`git log -p`, `git show`, `git blame`), hidden content appears as a single line: `@PLACEHOLDER@`. This line is not the real content. Do not reason about it as code.
- The same applies to a few files at visible paths whose content also existed under a hidden path; they are absent from disk as well.
- Work from what is visible: callers, interfaces, tests, documentation. When a task needs a change inside a hidden path, say so and describe the change for the developer to make by hand.
- You cannot build or run tests that depend on hidden code. Build and test what you can; CI builds the full code once the developer pushes your branch.

The sandbox:

- @READ@ Do not try to read, recreate or guess hidden content.
- You can write only in this copy and in your temp directory ($TMPDIR).
- Network access is limited to an allowlist of domains, with no git host. There is no git credential and no SSH agent: do not try `git push`, `git pull` or `git fetch`.
- When a task needs a domain or a path you cannot reach (a package registry, an SDK), say so: the developer can add it under `[allowed-domain]` or `[allowed-external]` in `@SANDBOX_FILE@`, and confirms it at the next launch.

Git workflow:

- Commit your work on the current branch, or on a new branch. The developer fetches your branches directly from this copy (`git fetch claude`) and reviews them before merging.
- Do not create files under hidden paths, nor modify `@SANDBOX_FILE@`: a change to it has no effect until the developer confirms it outside the sandbox. You can create files at new paths (a `Makefile` at the root, a new directory): once the developer has merged them, they are hidden from you too, until the developer lists them under `[allowed-git]`; tell the developer which paths to add.
- In `.git`, the configuration and hooks are read-only. Commits, branches and the rest of git work as usual.
- Do not set up commit signing: there is no signing key in the sandbox. The developer signs your commits when integrating them.
- The developer's branches are local branches of this copy. They are updated at each launch, except a branch that holds commits of yours the developer has not integrated yet. Commits the developer makes during the session appear at the next launch.
