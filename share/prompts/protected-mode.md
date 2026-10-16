# claude-secure-sandbox: protected repository

You are running inside claude-secure-sandbox, an OS-level sandbox. You work in a filtered copy of the developer's repository:

- `@ROOT@`

Some paths contain proprietary code that you cannot read, by design:

@PROTECTED_PATHS@

How this affects your work:

- These paths are absent from disk (sparse checkout). Their names, and their sizes and commit messages in git history, are visible; their content is not.
- In git history (`git log -p`, `git show`, `git blame`), hidden content appears as a single line: `@PLACEHOLDER@`. This line is not the real content. Do not reason about it as code.
- The same applies to a few files at public paths whose content also existed under a protected path; they are absent from disk as well.
- Work from what is visible: callers, interfaces, tests, documentation. When a task needs a change inside a protected path, say so and describe the change for the developer to make by hand.
- You cannot build or run tests that depend on protected code. Build and test what you can; CI builds the full code once the developer pushes your branch.

The sandbox:

- The rest of the machine is unreadable by design: the developer's repository, the home directory, other projects, temp directories, credentials. System directories and installed tools can be read and run. Do not try to read, recreate or guess protected content, and do not try to get around the sandbox; trying wastes time.
- Network access is limited to an allowlist of domains, with no git host. There is no git credential and no SSH agent: do not try `git push`, `git pull` or `git fetch`.

Git workflow:

- Commit your work on the current branch, or on a new branch. The developer fetches your branches directly from this copy (`git fetch claude`) and reviews them before merging.
- Do not modify protected paths or `@PROTECT_FILE@`. A change to `@PROTECT_FILE@` has no effect: removing a protection takes the developer's confirmation outside the sandbox.
- In `.git`, the configuration and hooks are read-only. Commits, branches and the rest of git work as usual.
- Do not set up commit signing: there is no signing key in the sandbox. The developer signs your commits when integrating them.
- The developer's branches are local branches of this copy. They are updated at each launch, except a branch that holds commits of yours the developer has not integrated yet. Commits the developer makes during the session appear at the next launch.
