"""Session lifecycle: the developer's "claude" remote, the copy's branch at
launch, and the end of session summary.

The developer fetches Claude's branches directly from the copy, as from any
remote. The copy's config and hooks are locked by the sandbox, and the
upload-pack the developer's git runs there never fetches missing objects nor
applies the placeholders.
"""

from __future__ import annotations

import sys
from typing import Dict, List

from .common import NAME, GitError, ask, die, log, read_lines, stamp, warn
from .context import Context
from .filtered_copy import (INCOMING, branch_tips, check_copy_integrity, new_commits, report_absent_public_files,
                            write_sparse_patterns)

# A commit of Claude's can reference content that is hidden, hence missing from
# the copy: pack-objects skips it (the developer has it) instead of failing
# the whole fetch. Command-line configuration, the only one git trusts for
# this hook.
UPLOAD_PACK = ("GIT_NO_LAZY_FETCH=1 GIT_NO_REPLACE_OBJECTS=1 GIT_CONFIG_COUNT=1 "
               "GIT_CONFIG_KEY_0=uploadpack.packObjectsHook "
               "GIT_CONFIG_VALUE_0=\"sh -c 'exec \\\"\\$@\\\" --missing=allow-any' sh\" git upload-pack")


def ensure_dev_remote(ctx: Context) -> None:
    """Remote "claude" of the developer's repository, pointing to the copy."""
    g = ctx.git_dev
    url = g.value("config", "--get", "remote.claude.url")
    if url and url != ctx.work:
        warn(f'a remote "claude" already exists ({url}); Claude\'s branches are in {ctx.work}')
        return
    if not url:
        if not g.ok("remote", "add", "claude", ctx.work):
            warn('cannot add the remote "claude"')
            return
        log('remote "claude" added to the repository: git fetch claude retrieves Claude\'s branches')
    for key, value in (("fetch", "+refs/heads/*:refs/remotes/claude/*"),
                       ("pushurl", f"/dev/null/{NAME}-push-disabled"),
                       ("tagOpt", "--no-tags"),
                       ("uploadpack", UPLOAD_PACK)):
        if g.value("config", "--get", f"remote.claude.{key}") != value:
            g.run("config", "--replace-all", f"remote.claude.{key}", value)


def _integrate_hint(ctx: Context, branch: str) -> str:
    """The commands that bring Claude's commits into the developer's branch:
    copies signed by the developer if they sign their commits."""
    if ctx.git_dev.value("config", "--bool", "commit.gpgsign") == "true":
        return f"git fetch claude, then on {branch}: git cherry-pick -S --no-merges {branch}..claude/{branch}"
    return f"git fetch claude, then on {branch}: git rebase claude/{branch}"


def _count(ctx: Context, include: str, *exclude: str) -> int:
    """Number of commits reachable from include and from none of exclude."""
    revs = "".join(r + "\n" for r in [include] + ["^" + e for e in ctx.git_copy.commits(exclude)])
    value = ctx.git_copy.value("rev-list", "--count", "--stdin", input=revs)
    return int(value) if value.isdigit() else 0


def _integrated(ctx: Context, sha: str, tip: str) -> bool:
    """True if the developer's commit already holds every commit of Claude's
    branch, as is or rewritten (rebase, cherry-pick, new signature)."""
    if ctx.git_copy.ok("merge-base", "--is-ancestor", tip, sha, quiet=True):
        return True
    # Rewritten commits are compared by patch, in the developer's repository:
    # it has Claude's commits once they are fetched.
    if not ctx.git_dev.ok("cat-file", "-e", f"{tip}^{{commit}}", quiet=True):
        return False
    # A merge has no patch to compare: Claude's merges are kept aside instead
    # (_keep_merges) when its branch follows the developer's.
    proc = ctx.git_dev.run("rev-list", "--right-only", "--cherry-pick", "--no-merges", f"{sha}...{tip}", quiet=True)
    return proc.returncode == 0 and not proc.stdout.strip()


def _keep_merges(ctx: Context, branch: str, sha: str, tip: str) -> None:
    """Before Claude's branch follows the developer's, a merge commit of
    Claude's the developer does not have (its conflict resolution, say) is
    kept under refs/backup: _integrated cannot tell whether it was taken."""
    if ctx.git_copy.value("rev-list", "--merges", f"{sha}..{tip}"):
        backup = f"refs/backup/{stamp()}/{branch}"
        ctx.git_copy.out("update-ref", backup, tip)
        log(f"Claude's {branch} has merge commits that are not in yours: kept, git fetch claude {backup}")


def _choose(ctx: Context, branch: str, sha: str, tip: str) -> str:
    log(f"your branch {branch} and Claude's have diverged: "
        f"{_count(ctx, sha, tip)} commit(s) of yours are not in Claude's branch, "
        f"{_count(ctx, tip, sha)} of Claude's are not in yours.\n"
        "  1) start from your branch (Claude's commits stay retrievable)\n"
        f"  2) quit, to integrate Claude's commits first ({_integrate_hint(ctx, branch)})\n"
        "  3) continue on Claude's branch, without your new commits")
    while True:
        answer = ask("choice [1/2/3]: ")
        if answer is None:
            warn("no terminal to ask: continuing on Claude's branch")
            return "3"
        if answer in ("1", "2", "3"):
            return answer


def _start_session(ctx: Context, branch: str, sha: str) -> bool:
    """New session: puts the copy on the developer's current branch, or keeps
    Claude's when it has commits the developer has not integrated yet.
    Returns False if the developer chose to quit."""
    now = stamp()
    target = sha
    if not ctx.fresh_copy:
        tip = ctx.git_copy.value("rev-parse", "-q", "--verify", f"refs/heads/{branch}^{{commit}}") if branch else ""
        if tip and tip != sha and _integrated(ctx, sha, tip):
            _keep_merges(ctx, branch, sha, tip)
        elif tip and tip != sha:
            if ctx.git_copy.ok("merge-base", "--is-ancestor", sha, tip, quiet=True):
                choice = "3"
                log(f"continuing on Claude's {branch}: {_count(ctx, tip, sha)} commit(s) "
                    f"not in your branch yet ({_integrate_hint(ctx, branch)})")
            else:
                choice = _choose(ctx, branch, sha, tip)
            if choice == "2":
                log(f"integrate Claude's commits ({_integrate_hint(ctx, branch)}), then run claude again")
                return False
            if choice == "3":
                target = tip
            else:
                ctx.git_copy.out("update-ref", f"refs/backup/{now}/{branch}", tip)
                log(f"Claude's {branch} kept: git fetch claude refs/backup/{now}/{branch}")
        status = ctx.git_copy_worktree.value("status", "--porcelain", "--untracked-files=all")
        if status:
            if not ctx.git_copy_worktree.ok("stash", "push", "--include-untracked", "--quiet",
                                      "-m", f"{NAME} {now}"):
                die(f"cannot stash the changes in the copy ({ctx.work}); run with --continue or clean it up")
            log(f"uncommitted changes from the previous session stashed (git stash, \"{NAME} {now}\")")
    report_absent_public_files(ctx, write_sparse_patterns(ctx, target))
    if branch:
        if not ctx.git_copy_worktree.ok("checkout", "-q", "-f", "-B", branch, target):
            die(f"cannot check out {branch} in the copy")
    elif not ctx.git_copy_worktree.ok("checkout", "-q", "-f", "--detach", sha):
        die(f"cannot check out {sha} in the copy")
    ctx.git_copy_worktree.run("sparse-checkout", "reapply", quiet=True)
    return True


def _sync_branch(ctx: Context, branch: str, sha: str, tip: str) -> None:
    """A branch the copy does not have checked out follows the developer's,
    unless it holds commits of Claude's the developer has not integrated."""
    if not tip:
        ctx.git_copy.out("update-ref", f"refs/heads/{branch}", sha)
    elif tip != sha:
        if _integrated(ctx, sha, tip):
            _keep_merges(ctx, branch, sha, tip)
            ctx.git_copy.out("update-ref", f"refs/heads/{branch}", sha, tip)
        else:
            log(f"Claude's {branch} has commits you have not integrated: left as is "
                f"({_integrate_hint(ctx, branch)})")


def sync_branches(ctx: Context, new_session: bool) -> bool:
    """Updates the copy's branches from the developer's, fetched under
    refs/incoming at this launch. The branch Claude has checked out is left
    alone when a session is resumed. Returns False if the developer chose to
    quit."""
    # The HEAD fetched at this launch: the developer may have committed since.
    sha = ctx.git_copy.value("rev-parse", "-q", "--verify", f"{INCOMING}/HEAD^{{commit}}")
    if not sha:
        die("the developer's HEAD is missing from the copy")
    current = ctx.git_dev.value("symbolic-ref", "-q", "--short", "HEAD")
    listing = ctx.git_copy.value("for-each-ref", "--format=%(objectname) %(refname)", f"{INCOMING}/heads")
    incoming = {ref[len(INCOMING) + len("/heads/"):]: oid
                for oid, _, ref in (line.partition(" ") for line in listing.splitlines()) if ref}
    try:
        if new_session and not _start_session(ctx, current, sha):
            return False
        skip = ctx.git_copy.value("symbolic-ref", "-q", "--short", "HEAD")
        tips = branch_tips(ctx)
        for branch, oid in sorted(incoming.items()):
            if branch != skip:
                try:
                    _sync_branch(ctx, branch, oid, tips.get(branch, ""))
                except GitError:
                    warn(f"cannot update {branch} in the copy")
        # Branches deleted by the developer: kept only if Claude has work on them.
        for branch, tip in sorted(tips.items()):
            if branch in incoming or branch == skip:
                continue
            try:
                if not new_commits(ctx, tip):
                    ctx.git_copy.out("update-ref", "-d", f"refs/heads/{branch}", tip)
            except GitError:
                pass
        return True
    finally:
        stale = ctx.git_copy.value("for-each-ref", "--format=delete %(refname)", INCOMING)
        if stale:
            ctx.git_copy.run("update-ref", "--stdin", input=stale + "\n")


def end_of_session(ctx: Context, before: Dict[str, str]) -> None:
    """Tells the developer which branches have new commits to fetch."""
    if not check_copy_integrity(ctx):
        warn("the copy's .git was tampered with during the session: "
             "it will be rebuilt at the next launch, and its branches archived")
        return
    known = set(before.values()) | set(read_lines(ctx.state_file("host-tips")))
    lines: List[str] = []
    changed = []
    for branch, tip in sorted(branch_tips(ctx).items()):
        if before.get(branch) == tip:
            continue
        n = _count(ctx, tip, *known)
        if n:
            lines.append(f"  {branch}: {n} new commit(s)")
            changed.append(branch)
    dirty = len(ctx.git_copy_worktree.value("status", "--porcelain", "--untracked-files=all").splitlines())
    if not lines and not dirty:
        return
    out = [f"\n{NAME}: end of session"] + lines
    if changed:
        target = changed[0] if len(changed) == 1 else "<branch>"
        out.append(f"  next: {_integrate_hint(ctx, target)}")
    if dirty:
        out.append(f"  {dirty} uncommitted file(s) left in the copy ({ctx.work}); "
                   "they will be stashed at the next launch")
    print("\n".join(out), file=sys.stderr, flush=True)
