"""Building and updating the filtered copy."""

from __future__ import annotations

import glob
import os
import shutil
import stat
import subprocess
from typing import List, Set, Tuple

from .common import (EMPTY_BLOB, NULL_OID, PLACEHOLDER_TEXT, SANDBOX_FILE, GitError,
                     die, log, path_within, read_lines, stamp, warn, write_lines, write_text)
from .context import Context
from .sandbox_file import hidden_roots, is_visible, sparse_escape

_FILE_MODES = ("100644", "100755", "120000")

# The developer's branches and HEAD land here during the launch; the session
# then updates the copy's branches from them (session.sync_branches), and
# deletes them.
INCOMING = "refs/incoming"


def dev_export_refs(ctx: Context) -> List[Tuple[str, str]]:
    """(sha, target refname) for the developer refs exported to the copy:
    branches, tags that point to a commit, and HEAD."""
    try:
        listing = ctx.git_dev.out(
            "for-each-ref", "--format=%(objectname) %(objecttype) %(*objecttype) %(refname)",
            "refs/heads", "refs/tags")
        head = ctx.git_dev.out("rev-parse", "HEAD", quiet=True)
    except GitError:
        die("cannot read the developer's refs")
    refs = []
    for line in listing.splitlines():
        sha, kind, peeled, ref = line.split(" ", 3)
        if ref.startswith("refs/heads/"):
            refs.append((sha, f"{INCOMING}/heads/" + ref[len("refs/heads/"):]))
        elif ref.startswith("refs/tags/") and "commit" in (kind, peeled):
            refs.append((sha, ref))
    refs.append((head, f"{INCOMING}/HEAD"))
    return refs


def write_spec(ctx: Context) -> str:
    """Sparse specification blob (patterns to include) for the fetch filter.
    It is written to a separate repository, added as an alternate for the
    developer's upload-pack: the developer's repository is not modified."""
    spec_git = ctx.state_file("spec.git")
    env = dict(os.environ, GIT_DIR=spec_git)
    if not os.path.isdir(spec_git):
        if subprocess.run(["git", "init", "-q", "--bare", "--template=", spec_git]).returncode:
            die(f"init {spec_git}")
    spec = "".join(f"/{sparse_escape(p)}\n" for p in [SANDBOX_FILE] + ctx.visible)
    proc = subprocess.run(["git", "hash-object", "-w", "--stdin"], input=spec, env=env,
                          stdout=subprocess.PIPE, encoding="utf-8")
    if proc.returncode:
        die("cannot write the sparse filter")
    return proc.stdout.strip()


def _hash_placeholder(ctx: Context, write: bool) -> str:
    args = ["hash-object", "--stdin"] + (["-w"] if write else [])
    return ctx.git_copy.out(*args, input=PLACEHOLDER_TEXT + "\n")


def init_copy(ctx: Context) -> None:
    shutil.rmtree(ctx.work, ignore_errors=True)
    os.makedirs(ctx.work)
    env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")
    if subprocess.run(["git", "init", "-q", "--template=", ctx.work], env=env).returncode:
        die("git init of the copy")
    for d in ("hooks", "info", "objects/info"):
        os.makedirs(os.path.join(ctx.copy_git, d), exist_ok=True)
    name = ctx.git_dev.value("config", "user.name") or "Claude"
    email = ctx.git_dev.value("config", "user.email") or "claude@localhost"
    settings = [
        ("core.repositoryformatversion", "1"),
        ("extensions.partialClone", "host"),
        ("remote.host.url", "csnull::host"),
        ("remote.host.promisor", "true"),
        ("remote.host.partialclonefilter", f"sparse:oid={ctx.spec}"),
        ("core.sparseCheckout", "true"),
        ("core.sparseCheckoutCone", "false"),
        ("core.commitGraph", "false"),
        ("gc.auto", "0"),
        ("gc.writeCommitGraph", "false"),
        ("fetch.writeCommitGraph", "false"),
        ("maintenance.auto", "false"),
        ("user.name", name),
        ("user.email", email),
    ]
    for key, value in settings:
        ctx.git_copy.out("config", key, value)
    write_text(os.path.join(ctx.copy_git, "info", "sparse-checkout"), "/*\n")
    for f in ("host-tips", "hidden-blobs", "hidden-paths"):
        write_text(ctx.state_file(f), "")
    ctx.placeholder = _hash_placeholder(ctx, write=True)


def fetch_from_dev(ctx: Context) -> List[str]:
    """Filtered fetch from the developer's repository. Returns the new tips,
    or raises GitError."""
    refs = dev_export_refs(ctx)
    tips = sorted({sha for sha, _ in refs})
    upload_pack = (f"GIT_ALTERNATE_OBJECT_DIRECTORIES='{ctx.state_file('spec.git')}/objects' "
                   "git -c uploadpack.allowFilter=true -c uploadpack.allowAnySHA1InWant=true upload-pack")
    refspecs = "".join(f"+{sha}:{ref}\n" for sha, ref in refs)
    ctx.git_copy.out(
        "-c", f"url.{ctx.dev_root}.insteadOf=csnull::host",
        "fetch", "--quiet", "--no-tags", "--no-write-fetch-head", "--no-recurse-submodules",
        f"--filter=sparse:oid={ctx.spec}", f"--upload-pack={upload_pack}", "--stdin", "host",
        input=refspecs)
    # Delete leftovers and tags that no longer exist on the developer side
    # (refs/remotes/host: the developer's branches in earlier versions).
    wanted = {ref for _, ref in refs}
    existing = ctx.git_copy.out("for-each-ref", "--format=%(refname)",
                                INCOMING, "refs/remotes/host", "refs/tags").splitlines()
    stale = [ref for ref in existing if ref and ref not in wanted]
    if stale:
        ctx.git_copy.out("update-ref", "--stdin", input="".join(f"delete {r}\n" for r in stale))
    return tips


def _log_hidden_paths(ctx: Context, revs: List[str]) -> str:
    """git log --raw restricted to hidden paths: everything except [visible].
    --full-history walks every side branch, including merges that discarded
    it."""
    specs = [":/"] + [f":(top,exclude,literal){p}" for p in [SANDBOX_FILE] + ctx.visible]
    return ctx.git_dev.out(
        "-c", "diff.renames=false", "-c", "log.showSignature=false", "-c", "core.quotePath=false",
        "log", "--stdin", "--format=", "--raw", "--no-abbrev", "--no-renames", "--root",
        "--full-history", "--diff-merges=first-parent", "--no-ext-diff", "--", *specs,
        input="".join(r + "\n" for r in revs))


def scan_hidden_history(ctx: Context, tips: List[str]) -> Set[str]:
    """Fingerprints of every content that existed under a hidden path, in
    the commits that are new since the last scan, added to
    state/hidden-blobs, and those paths, added to state/hidden-paths.
    Returns the blobs found by this scan."""
    old = read_lines(ctx.state_file("host-tips"))
    revs = tips + ["^" + t for t in old]
    try:
        raw = _log_hidden_paths(ctx, revs)
    except GitError:
        die("cannot scan the developer's history")
    found, paths = set(), set()
    for line in raw.splitlines():
        if not line.startswith(":"):
            continue
        meta, _, path = line.partition("\t")
        paths.add(path)
        fields = meta.split()
        old_mode, new_mode, old_oid, new_oid = fields[0][1:], fields[1], fields[2], fields[3]
        if old_mode in _FILE_MODES:
            found.add(old_oid)
        if new_mode in _FILE_MODES:
            found.add(new_oid)
    found -= {NULL_OID, EMPTY_BLOB}
    known = set(read_lines(ctx.state_file("hidden-blobs")))
    write_lines(ctx.state_file("hidden-blobs"), sorted(known | found))
    known_paths = set(read_lines(ctx.state_file("hidden-paths")))
    if not paths <= known_paths:
        write_lines(ctx.state_file("hidden-paths"), sorted(known_paths | paths))
    return found


def _all_objects(ctx: Context) -> List[Tuple[str, str]]:
    proc = ctx.git_copy.run("cat-file", "--batch-all-objects",
                            "--batch-check=%(objectname) %(objecttype)", quiet=True)
    return [tuple(line.split(" ", 1)) for line in proc.stdout.splitlines() if line]


def present_blobs(ctx: Context) -> Set[str]:
    return {oid for oid, kind in _all_objects(ctx) if kind == "blob"}


def repack_without(ctx: Context, remove: Set[str]) -> None:
    """Rewrites the copy's packs without the given objects."""
    pack_dir = os.path.join(ctx.copy_git, "objects", "pack")
    keep = sorted(oid for oid, _ in _all_objects(ctx) if oid not in remove)
    try:
        name = ctx.git_copy.out("pack-objects", "-q", os.path.join(pack_dir, ".tmp-cs"),
                                input="".join(o + "\n" for o in keep))
    except GitError:
        die("cannot repack the copy")
    for f in glob.glob(os.path.join(pack_dir, "pack-*")) + glob.glob(os.path.join(pack_dir, "multi-pack-index*")):
        os.unlink(f)
    objects = os.path.join(ctx.copy_git, "objects")
    for entry in os.listdir(objects):
        path = os.path.join(objects, entry)
        if len(entry) == 2 and os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
    for f in glob.glob(os.path.join(pack_dir, f".tmp-cs-{name}.*")):
        ext = f.rsplit(".", 1)[1]
        os.rename(f, os.path.join(pack_dir, f"pack-{name}.{ext}"))
    write_text(os.path.join(pack_dir, f"pack-{name}.promisor"), "")


def remove_hidden_blobs(ctx: Context) -> bool:
    """Removes every known hidden content from the copy, then checks it is gone."""
    hidden = set(read_lines(ctx.state_file("hidden-blobs")))
    hits = present_blobs(ctx) & hidden
    if hits:
        log(f"removing {len(hits)} hidden content(s) also seen under a visible path")
        repack_without(ctx, hits)
    return not (present_blobs(ctx) & hidden)


def _links_in(path: str) -> bool:
    """True if path contains a symbolic link, or a file with several hard links."""
    for dirpath, dirnames, filenames in os.walk(path):
        for name in dirnames + filenames:
            st = os.lstat(os.path.join(dirpath, name))
            if stat.S_ISLNK(st.st_mode):
                return True
            if stat.S_ISREG(st.st_mode) and st.st_nlink > 1:
                return True
    return False


def check_copy_integrity(ctx: Context) -> bool:
    """Structural integrity of the copy's .git, checked from outside the sandbox."""
    for d in (ctx.work, ctx.copy_git):
        if not os.path.isdir(d) or os.path.islink(d):
            return False
    for f in ("objects/info/alternates", "objects/info/http-alternates", "commondir",
              "shallow", "info/grafts"):
        if os.path.lexists(os.path.join(ctx.copy_git, f)):
            return False
    if _links_in(os.path.join(ctx.copy_git, "objects")):
        return False
    return (ctx.git_copy.value("config", "--get", "remote.host.url") == "csnull::host"
            and ctx.git_copy.value("config", "--get", "extensions.partialClone") == "host")


def install_placeholders(ctx: Context, new: Set[str]) -> None:
    """Creates the replace refs that show the placeholder instead of hidden
    contents. If Claude deleted some, all of them are recreated."""
    ctx.placeholder = _hash_placeholder(ctx, write=True)
    known = read_lines(ctx.state_file("hidden-blobs"))
    have = len(ctx.git_copy.out("for-each-ref", "--format=x", "refs/replace").splitlines())
    targets = known if have < len(known) else sorted(new)
    updates = "".join(f"update refs/replace/{oid} {ctx.placeholder}\n"
                      for oid in targets if oid != ctx.placeholder)
    try:
        if updates:
            ctx.git_copy.out("update-ref", "--stdin", input=updates)
        ctx.git_copy.out("pack-refs", "--all")
    except GitError:
        die("cannot create placeholders")


def prepare_copy(ctx: Context) -> None:
    """Creates the copy if needed, or updates it. Rebuilds it if a check fails."""
    try:
        old_hash = open(ctx.state_file("visible-hash")).read().strip()
    except OSError:
        old_hash = ""
    rebuild = ""
    if not os.path.isdir(ctx.copy_git):
        rebuild = "init"
    elif old_hash != ctx.visible_hash:
        rebuild = "visible-changed"
    elif not check_copy_integrity(ctx):
        rebuild = "integrity"
    ctx.spec = write_spec(ctx)
    messages = {
        "visible-changed": (log, f"the [visible] paths of {SANDBOX_FILE} changed: rebuilding the copy and archiving sessions"),
        "integrity": (warn, "the copy's .git was tampered with: rebuilding"),
        "verify": (warn, "hidden content found in the copy: rebuilding"),
        "init": (log, "creating the filtered copy (first run, may take a while)"),
    }
    for attempt in (1, 2):
        if rebuild:
            say, message = messages[rebuild]
            say(message)
            if rebuild != "init":
                archive_copy(ctx, rebuild)
            init_copy(ctx)
            ctx.fresh_copy = True
        try:
            tips = fetch_from_dev(ctx)
        except GitError:
            tips = None
        if tips is not None:
            new = scan_hidden_history(ctx, tips)
            if remove_hidden_blobs(ctx):
                install_placeholders(ctx, new)
                known = set(read_lines(ctx.state_file("host-tips")))
                write_lines(ctx.state_file("host-tips"), sorted(known | set(tips)))
                write_text(ctx.state_file("visible-hash"), ctx.visible_hash + "\n")
                return
        if attempt == 2:
            break
        rebuild = "verify"
    die("cannot get a copy free of hidden content; session cancelled")


def claude_project_dir(ctx: Context, path: str = "") -> str:
    """Claude Code's session directory for a given working directory."""
    encoded = "".join(c if c.isascii() and c.isalnum() else "-" for c in (path or ctx.work))
    return os.path.join(ctx.claude_home, "projects", encoded)


def claude_branches(ctx: Context) -> List[str]:
    """Branches of the copy holding commits the developer does not have."""
    result = []
    listing = ctx.git_copy.value("for-each-ref", "--format=%(objectname) %(refname:short)", "refs/heads")
    for line in listing.splitlines():
        tip, _, branch = line.partition(" ")
        try:
            if new_commits(ctx, tip):
                result.append(branch)
        except GitError:
            result.append(branch)
    return result


def archive_copy(ctx: Context, reason: str) -> None:
    """Moves the copy and this repository's sessions out of the sandbox's reach."""
    dest = os.path.join(ctx.archive, f"{stamp()}-{reason}")
    n = 1
    while os.path.lexists(dest):
        n += 1
        dest = os.path.join(ctx.archive, f"{stamp()}-{reason}-{n}")
    os.makedirs(dest)
    branches = claude_branches(ctx) if os.path.isdir(ctx.copy_git) else []
    if os.path.isdir(ctx.work) and not os.path.islink(ctx.work):
        os.rename(ctx.work, os.path.join(dest, "work"))
    enc = claude_project_dir(ctx)
    if os.path.isdir(enc) and not os.path.islink(enc):
        for transcript in glob.glob(os.path.join(enc, "*.jsonl")):
            sid = os.path.basename(transcript)[:-len(".jsonl")]
            history = os.path.join(ctx.claude_home, "file-history", sid)
            if os.path.isdir(history) and not os.path.islink(history):
                os.makedirs(os.path.join(dest, "file-history"), exist_ok=True)
                os.rename(history, os.path.join(dest, "file-history", sid))
        os.rename(enc, os.path.join(dest, "projects"))
    for f in ("host-tips", "hidden-blobs", "hidden-paths", "visible-hash"):
        try:
            os.unlink(ctx.state_file(f))
        except FileNotFoundError:
            pass
    log(f"previous copy and sessions archived in {dest}")
    for branch in branches:
        log(f"  Claude's {branch} is still there: git fetch {dest}/work {branch}")


def write_sparse_patterns(ctx: Context, commit: str) -> List[str]:
    """Sparse-checkout patterns: everything, except every path ever hidden in
    the developer's history (as high in the tree as possible), and the files
    whose content is hidden at the given commit. What Claude cannot read is
    already absent from the copy: these patterns keep git from looking for
    it, and let Claude add files at new paths (a Makefile at the root), which
    the developer can then list in [visible]. Returns the visible files whose
    content is hidden."""
    hidden = set(read_lines(ctx.state_file("hidden-blobs")))
    roots, _ = hidden_roots(ctx.visible, read_lines(ctx.state_file("hidden-paths")))
    lines = ["/*"] + [f"!/{sparse_escape(r)}" for r in roots]
    public = []
    for entry in ctx.git_copy.out("ls-tree", "-r", "-z", commit).split("\0"):
        if not entry:
            continue
        meta, path = entry.split("\t", 1)
        if meta.split(" ")[2] not in hidden:
            continue
        if is_visible(ctx.visible, path):
            lines.append(f"!/{sparse_escape(path)}")
            public.append(path)
        elif not any(path_within(r, path) for r in roots):
            lines.append(f"!/{sparse_escape(path)}")
    target = os.path.join(ctx.copy_git, "info", "sparse-checkout")
    write_lines(target + ".tmp", lines)
    os.replace(target + ".tmp", target)
    return public


def report_absent_public_files(ctx: Context, paths: List[str]) -> None:
    """Tells the developer, once per file, why a public file is missing from
    the copy: its content is also the content of a hidden file."""
    reported = set(read_lines(ctx.state_file("reported-absent")))
    new = [p for p in paths if p not in reported]
    if new:
        shown = new[:10] + ([f"... and {len(new) - 10} more"] if len(new) > 10 else [])
        log("visible files absent from the copy, because a hidden file has the same content:"
            + "".join(f"\n  {p}" for p in shown))
        write_lines(ctx.state_file("reported-absent"), sorted(reported | set(new)))


def new_commits(ctx: Context, tip: str) -> List[str]:
    """Commits reachable from tip but not from any commit known to the
    developer, oldest first. Raises GitError."""
    revs = [tip] + ["^" + t for t in read_lines(ctx.state_file("host-tips"))]
    # A developer commit may be gone from the copy (git gc run by Claude).
    return ctx.git_copy.out("rev-list", "--topo-order", "--reverse", "--ignore-missing", "--stdin",
                            input="".join(r + "\n" for r in revs)).split()
