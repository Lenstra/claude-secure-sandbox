"""Unit tests for the pure functions of claude-secure-sandbox."""

import io
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from claude_secure_sandbox.access import (check_access, domains_overlap, local_destination,  # noqa: E402
                                          path_tools, secrets, url_host)
from claude_secure_sandbox.common import (EMPTY_BLOB, NULL_OID, check_seccomp, ensure_real_dir,  # noqa: E402
                                          git_dev, install_file, overlaps, path_within, read_text,
                                          regular_file)
from claude_secure_sandbox.context import Context  # noqa: E402
from claude_secure_sandbox.filtered_copy import parse_raw_log  # noqa: E402
from claude_secure_sandbox.sandbox_file import (Grants, SandboxFile, confirm_access,  # noqa: E402
                                                confirm_visible, hidden_roots, is_visible, leads_to_visible,
                                                normalize_domain, normalize_visible, sparse_escape)
from claude_secure_sandbox.scan import find_sandbox_files, protected_repositories  # noqa: E402


class NormalizeVisibleTest(unittest.TestCase):
    def test_literal_paths(self):
        self.assertEqual(normalize_visible("core/engine"), "core/engine")
        self.assertEqual(normalize_visible("./core//engine/"), "core/engine")
        self.assertEqual(normalize_visible("a/./b"), "a/b")

    def test_refused(self):
        cases = {
            "/etc/passwd": "absolute path",
            "src/*.go": "pattern not supported",
            "!core": "pattern not supported",
            "a/[b]": "pattern not supported",
            "../outside": "outside the repository root",
            "a/../b": "outside the repository root",
            ".git/config": ".git path",
            "sub/.git/x": ".git path",
            ".claudesandbox": "always visible",
        }
        for line, message in cases.items():
            with self.subTest(line=line):
                with self.assertRaises(ValueError) as e:
                    normalize_visible(line)
                self.assertIn(message, str(e.exception))


class PathTest(unittest.TestCase):
    def test_path_within(self):
        self.assertTrue(path_within("/a/b", "/a/b"))
        self.assertTrue(path_within("/a/b", "/a/b/c"))
        self.assertFalse(path_within("/a/b", "/a/bc"))
        self.assertFalse(path_within("/a/b/c", "/a/b"))

    def test_sparse_escape(self):
        self.assertEqual(sparse_escape("a[1]*?.txt"), r"a\[1\]\*\?.txt")
        self.assertEqual(sparse_escape("!x"), r"\!x")
        self.assertEqual(sparse_escape("#x"), r"\#x")
        self.assertEqual(sparse_escape("x "), "x\\ ")


class SandboxFileTest(unittest.TestCase):
    def test_sections(self):
        f = SandboxFile("# comment\n[allowed-git]\nsrc/\n\n[allowed-external]\n~/.m2\n/opt/sdk/\n"
                        "[allowed-domain]\nRegistry.npmjs.org\n*.example.com:8443\n")
        self.assertEqual(f.errors, [])
        self.assertEqual(f.visible, {"src"})
        self.assertEqual(f.paths, {os.path.expanduser("~/.m2"), "/opt/sdk"})
        self.assertEqual(f.domains, {"registry.npmjs.org", "*.example.com:8443"})

    def test_errors(self):
        cases = {
            "secret\n": "outside a section",
            "[network]\nx.com\n": "unknown section",
            "[allowed-external]\nrelative/dir\n": "absolute path or ~/",
            "[allowed-external]\n~\n": "too broad",
            "[allowed-external]\n/\n": "too broad",
            "[allowed-external]\n~/a/../b\n": ". and .. not allowed",
            "[allowed-external]\n~/*.jar\n": "pattern not supported",
            "[allowed-domain]\n*\n": "not allowed",
            "[allowed-domain]\nhttps://x.com/\n": "invalid domain",
            "[allowed-git]\n/etc\n": "absolute path",
            "[protected]\nsecret\n": "unknown section [protected]",
        }
        for content, message in cases.items():
            with self.subTest(content=content):
                errors = SandboxFile(content).errors
                self.assertEqual(len(errors), 1)
                self.assertIn(message, errors[0])


class VisibleTest(unittest.TestCase):
    def test_whole_repository(self):
        self.assertEqual(normalize_visible("."), ".")
        self.assertEqual(normalize_visible("./"), ".")
        self.assertTrue(is_visible(["."], "core/x.c"))
        self.assertTrue(leads_to_visible(["."], "core"))
        self.assertEqual(hidden_roots(["."], ["core/x.c", "top.txt"]), ([], []))

    def test_visible(self):
        visible = ["src", "lib/public"]
        self.assertTrue(is_visible(visible, "src/a.c"))
        self.assertTrue(is_visible(visible, ".claudesandbox"))
        self.assertFalse(is_visible(visible, "srcx/a.c"))
        self.assertFalse(is_visible(visible, "lib"))
        self.assertTrue(leads_to_visible(visible, "lib"))
        self.assertTrue(leads_to_visible(visible, ""))
        self.assertFalse(leads_to_visible(visible, "core"))

    def test_hidden_roots(self):
        files = ["src/a.c", "core/x.c", "core/y/z.c", "lib/public/p.c", "lib/secret/s.c", "top.txt",
                 ".claudesandbox"]
        self.assertEqual(hidden_roots(["src", "lib/public"], files),
                         (["core", "lib/secret", "top.txt"], ["core", "lib/secret"]))


class SeccompTest(unittest.TestCase):
    def test_missing_helper_refused_on_linux(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        srt = os.path.join(root, "node_modules", ".bin", "srt")
        with mock.patch.object(platform, "system", return_value="Linux"):
            with mock.patch.object(platform, "machine", return_value="riscv64"), \
                    mock.patch("sys.stderr"), self.assertRaises(SystemExit):
                check_seccomp(srt)
            with mock.patch.object(platform, "machine", return_value="x86_64"):
                if os.path.exists(srt):
                    check_seccomp(srt)


class PathToolsTest(unittest.TestCase):
    def test_path_tools(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        ctx = SimpleNamespace(root=root, run_dir="/nonexistent/run", state_root="/nonexistent/state",
                              protected_elsewhere=[], protected=False)
        saved = dict(os.environ)
        try:
            os.environ["HOME"] = root
            os.environ["PATH"] = f"{root}/node_modules/.bin:{root}:/usr/bin:{root}/bin:relative/bin"
            # A bin directory comes with its installation (/usr for /usr/bin); the
            # home directory itself and the wrapper's own directories never.
            self.assertEqual(path_tools(ctx), [f"{root}/node_modules/.bin", "/usr"])
        finally:
            os.environ.clear()
            os.environ.update(saved)


class UrlHostTest(unittest.TestCase):
    def test_hosts(self):
        self.assertEqual(url_host("https://GitHub.com/org/repo.git"), "github.com")
        self.assertEqual(url_host("ssh://git@git.example.com:2222/repo"), "git.example.com")
        self.assertEqual(url_host("git@gitlab.com:org/repo.git"), "gitlab.com")
        self.assertEqual(url_host("https://user:pw@[::1]:8080/x"), "[::1]")

    def test_local_paths(self):
        for url in ("/srv/repo.git", "./repo", "../repo", "file:///srv/repo", "repo"):
            with self.subTest(url=url):
                self.assertEqual(url_host(url), "")


class DomainsOverlapTest(unittest.TestCase):
    def test_overlap(self):
        for a, b in (("github.com", "github.com"), ("api.github.com", "*.github.com"),
                     ("*.github.com", "*.github.com"), ("*.com", "*.github.com"), ("*.com", "github.com"),
                     ("api.githubcopilot.com:443", "*.githubcopilot.com")):
            with self.subTest(a=a, b=b):
                self.assertTrue(domains_overlap(a, b))
                self.assertTrue(domains_overlap(b, a))

    def test_no_overlap(self):
        for a, b in (("notgithub.com", "*.github.com"), ("registry.npmjs.org", "github.com"),
                     ("github.com.evil.io", "github.com")):
            with self.subTest(a=a, b=b):
                self.assertFalse(domains_overlap(a, b))
                self.assertFalse(domains_overlap(b, a))


class LocalDestinationTest(unittest.TestCase):
    def test_this_computer(self):
        for entry in ("localhost", "*.localhost", "app.localhost:3000", "127.0.0.1", "127.1.2.3:5037",
                      "::1", "[::1]:8080", "0.0.0.0", "169.254.169.254", "[fe80::1]"):
            with self.subTest(entry=entry):
                self.assertTrue(local_destination(entry))

    def test_other_hosts(self):
        for entry in ("api.anthropic.com", "registry.npmjs.org:443", "*.example.com", "10.0.0.5",
                      "192.168.1.10", "localhost.example.com"):
            with self.subTest(entry=entry):
                self.assertFalse(local_destination(entry))


# --- what guards the sandbox ---------------------------------------------------


def _tmp_tree(test: unittest.TestCase) -> str:
    """A real temporary directory, removed after the test."""
    d = os.path.realpath(tempfile.mkdtemp(prefix="csb-unit-"))
    test.addCleanup(shutil.rmtree, d, True)
    return d


def _touch(path: str, text: str = "") -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    return path


class NormalizeDomainTest(unittest.TestCase):
    def test_addresses(self):
        self.assertEqual(normalize_domain("[::1]"), "[::1]")
        self.assertEqual(normalize_domain("[2001:DB8::1]:8443"), "[2001:db8::1]:8443")
        self.assertEqual(normalize_domain("10.0.0.5:8080"), "10.0.0.5:8080")

    def test_refused(self):
        for entry in ("[garbage", "[::1", "[1.2.3.4]", "[::1]x", "[::1]:", "*", "*:443",
                      "https://x.com", "x.com/path", "-x.com", "x_y.com"):
            with self.subTest(entry=entry), self.assertRaises(ValueError):
                normalize_domain(entry)


class LocalDestinationFormsTest(unittest.TestCase):
    def test_other_spellings_of_this_computer(self):
        # Resolvers accept short and numeric IPv4 forms, and IPv4-mapped IPv6.
        for entry in ("127.1", "2130706433", "0x7f.1", "0", "0x7f000001:22", "[::ffff:127.0.0.1]",
                      "[::]", "LOCALHOST"):
            with self.subTest(entry=entry):
                self.assertTrue(local_destination(entry.lower()))


class ParseRawLogTest(unittest.TestCase):
    A, B, C = "a" * 40, "b" * 40, "c" * 40

    def test_both_sides_and_odd_paths(self):
        raw = "\0".join([
            f":100644 100644 {self.A} {self.B} M", "core/tab\tname.c",
            f":000000 100755 {NULL_OID} {self.C} A", 'core/quo"te.c',
            f":100644 000000 {self.C} {NULL_OID} D", ":starts-with-colon",
            f":120000 120000 {self.A} {self.A} M", "core/link",
            f":160000 160000 {'d' * 40} {'e' * 40} M", "core/submodule",
            f":000000 100644 {NULL_OID} {EMPTY_BLOB} A", "core/empty",
            f":100644 100644 {self.B} {self.A} M", "core/new\nline",
        ]) + "\0"
        blobs, paths = parse_raw_log(raw)
        # A submodule is a commit, not a blob; the empty blob is known to all.
        self.assertEqual(blobs, {self.A, self.B, self.C})
        self.assertEqual(paths, {"core/tab\tname.c", 'core/quo"te.c', ":starts-with-colon", "core/link",
                                 "core/submodule", "core/empty", "core/new\nline"})

    def test_empty(self):
        self.assertEqual(parse_raw_log(""), (set(), set()))

    def test_real_git(self):
        """The format the wrapper parses, as this git writes it, through the
        wrapper's own history scan."""
        from claude_secure_sandbox.filtered_copy import _log_hidden_paths
        repo = _tmp_tree(self)
        env = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t",
               "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

        def git(*args):
            return subprocess.run(["git", "-C", repo, *args], check=True, stdout=subprocess.PIPE,
                                  env=dict(os.environ, **env), encoding="utf-8").stdout.strip()
        git("init", "-q")
        _touch(os.path.join(repo, "src", "a.c"), "public")
        _touch(os.path.join(repo, "core", 'quo"te\tx.c'), "SECRET-1")
        git("add", "-A")
        git("commit", "-qm", "1")
        _touch(os.path.join(repo, "core", 'quo"te\tx.c'), "SECRET-2")
        git("commit", "-qam", "2")
        secret1 = git("rev-parse", "HEAD~1:core/quo\"te\tx.c")
        secret2 = git("rev-parse", "HEAD:core/quo\"te\tx.c")
        ctx = SimpleNamespace(visible=["src"], git_dev=git_dev(repo))
        blobs, paths = parse_raw_log(_log_hidden_paths(ctx, [git("rev-parse", "HEAD")]))
        self.assertEqual(blobs, {secret1, secret2})
        self.assertEqual(paths, {'core/quo"te\tx.c'})


class GrantsTest(unittest.TestCase):
    def test_kinds_are_independent(self):
        state = _tmp_tree(self)
        g = Grants(state)
        g.set("see", ["src", "lib"])
        g.set("read", ["/opt/sdk"])
        g.set("unprotected", [""])
        g = Grants(state)
        self.assertEqual(g.get("see"), ["src", "lib"])
        self.assertEqual(g.get("read"), ["/opt/sdk"])
        self.assertEqual(g.get("unprotected"), [""])
        self.assertEqual(g.get("domain"), [])
        g.set("see", [])
        g.set("unprotected", [])
        self.assertEqual(Grants(state).lines, ["read /opt/sdk"])

    def test_no_write_when_unchanged(self):
        state = _tmp_tree(self)
        g = Grants(state)
        g.set("see", ["src"])
        with mock.patch("claude_secure_sandbox.sandbox_file.write_lines") as write:
            Grants(state).set("see", ["src"])
        write.assert_not_called()


class _Answers:
    """Replaces the terminal: each question takes the next answer (None: no terminal)."""

    def __init__(self, test: unittest.TestCase, *answers):
        self.answers = list(answers)
        self.questions = []
        for patcher in (mock.patch("claude_secure_sandbox.sandbox_file.ask", side_effect=self.ask),
                        mock.patch("sys.stderr", new_callable=io.StringIO)):
            patcher.start()
            test.addCleanup(patcher.stop)

    def ask(self, question):
        self.questions.append(question)
        return self.answers.pop(0)


def _file(content: str) -> SandboxFile:
    f = SandboxFile(content)
    f.exists = True
    return f


class ConfirmVisibleTest(unittest.TestCase):
    def test_widening_asks_once(self):
        state = _tmp_tree(self)
        answers = _Answers(self, "y")
        self.assertEqual(confirm_visible(state, _file("[allowed-git]\nsrc\n"), False), (True, ["src"]))
        self.assertEqual(confirm_visible(state, _file("[allowed-git]\nsrc\n"), False), (True, ["src"]))
        self.assertEqual(len(answers.questions), 1)

    def test_narrowing_never_asks(self):
        state = _tmp_tree(self)
        Grants(state).set("see", ["."])
        answers = _Answers(self)
        self.assertEqual(confirm_visible(state, _file("[allowed-git]\nsrc/a\nlib\n"), False),
                         (True, ["lib", "src/a"]))
        self.assertEqual(answers.questions, [])

    def test_no_or_no_terminal_keeps_hidden(self):
        state = _tmp_tree(self)
        Grants(state).set("see", ["src"])
        _Answers(self, "n", None, "")
        for _ in range(3):
            self.assertEqual(confirm_visible(state, _file("[allowed-git]\nsrc\ncore\n"), False),
                             (True, ["src"]))
        self.assertEqual(Grants(state).get("see"), ["src"])

    def test_confirmation_ends_with_its_entry(self):
        state = _tmp_tree(self)
        Grants(state).set("see", ["src", "core"])
        answers = _Answers(self, "n")
        confirm_visible(state, _file("[allowed-git]\nsrc\n"), False)
        self.assertEqual(Grants(state).get("see"), ["src"])
        # Listed again: asked again.
        confirm_visible(state, _file("[allowed-git]\nsrc\ncore\n"), False)
        self.assertEqual(len(answers.questions), 1)

    def test_empty_file_hides_everything(self):
        state = _tmp_tree(self)
        Grants(state).set("see", ["src"])
        self.assertEqual(confirm_visible(state, _file(""), False), (True, []))

    def test_never_protected(self):
        state = _tmp_tree(self)
        answers = _Answers(self)
        self.assertEqual(confirm_visible(state, SandboxFile(), False), (False, []))
        self.assertEqual(answers.questions, [])

    def test_file_removed_stays_protected_until_confirmed(self):
        state = _tmp_tree(self)
        Grants(state).set("see", ["src"])
        answers = _Answers(self, None, "n", "y")
        # No terminal, then no: still protected, with what was confirmed.
        self.assertEqual(confirm_visible(state, SandboxFile(), True), (True, ["src"]))
        self.assertEqual(confirm_visible(state, SandboxFile(), True), (True, ["src"]))
        # Yes: no longer protected, and never asked again.
        self.assertEqual(confirm_visible(state, SandboxFile(), True), (False, []))
        self.assertEqual(confirm_visible(state, SandboxFile(), True), (False, []))
        self.assertEqual(len(answers.questions), 3)
        self.assertEqual(Grants(state).get("see"), [])

    def test_file_back_after_unprotecting(self):
        state = _tmp_tree(self)
        Grants(state).set("unprotected", [""])
        answers = _Answers(self, "y")
        self.assertEqual(confirm_visible(state, _file("[allowed-git]\nsrc\n"), True), (True, ["src"]))
        self.assertEqual(len(answers.questions), 1)
        self.assertEqual(Grants(state).get("unprotected"), [])


class ConfirmAccessTest(unittest.TestCase):
    def test_asked_once_and_dropped_with_its_entry(self):
        state = _tmp_tree(self)
        answers = _Answers(self, "y", "n")
        self.assertEqual(confirm_access(state, ["/opt/sdk"], ["registry.npmjs.org"]),
                         (["/opt/sdk"], ["registry.npmjs.org"]))
        self.assertEqual(confirm_access(state, ["/opt/sdk"], ["registry.npmjs.org"]),
                         (["/opt/sdk"], ["registry.npmjs.org"]))
        self.assertEqual(len(answers.questions), 1)
        # A new entry refused: the confirmed ones stay, the new one is not granted.
        self.assertEqual(confirm_access(state, ["/opt/sdk"], ["registry.npmjs.org", "pypi.org"]),
                         (["/opt/sdk"], ["registry.npmjs.org"]))
        # An entry gone from the file loses its confirmation.
        self.assertEqual(confirm_access(state, [], ["registry.npmjs.org"]), ([], ["registry.npmjs.org"]))
        self.assertEqual(Grants(state).get("read"), [])

    def test_no_terminal_grants_nothing_new(self):
        state = _tmp_tree(self)
        _Answers(self, None)
        self.assertEqual(confirm_access(state, ["/opt/sdk"], []), ([], []))


class CheckAccessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = _tmp_tree(self)
        self.protected = os.path.join(self.tmp, "protected")
        os.makedirs(os.path.join(self.protected, "src"))
        os.symlink(self.protected, os.path.join(self.tmp, "link"))
        git = mock.Mock()
        git.value.return_value = "remote.origin.url git@git.corp.example:org/repo.git"
        self.ctx = SimpleNamespace(git_dev=git, protected_elsewhere=[self.protected], protected=False,
                                   dev_root=os.path.join(self.tmp, "here"), dev_git_common="",
                                   state_root=os.path.join(self.tmp, "state"))

    def refused(self, paths=(), domains=()) -> str:
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err, self.assertRaises(SystemExit):
            check_access(self.ctx, list(paths), list(domains))
        return err.getvalue()

    def test_git_hosts_and_remotes(self):
        for domain in ("github.com", "api.github.com", "*.github.com", "*.com", "raw.githubusercontent.com",
                       "api.githubcopilot.com", "git.corp.example", "git.corp.example:443"):
            with self.subTest(domain=domain):
                self.assertIn("git host", self.refused(domains=[domain]))

    def test_this_computer(self):
        for domain in ("localhost:5037", "127.1", "[::1]:8080", "169.254.169.254"):
            with self.subTest(domain=domain):
                self.assertIn("this computer", self.refused(domains=[domain]))

    def test_protected_repository_named(self):
        for path in (self.protected, os.path.join(self.protected, "src"), self.tmp,
                     os.path.join(self.tmp, "link")):
            with self.subTest(path=path):
                self.assertIn(f"overlaps {self.protected}", self.refused(paths=[path]))

    def test_secrets(self):
        home = os.environ["HOME"]
        for path in (f"{home}/.ssh", f"{home}/.claude", f"{home}/.claude.json", "/tmp",
                     self.ctx.state_root):
            with self.subTest(path=path):
                self.assertIn("not allowed", self.refused(paths=[path]))

    def test_developer_repository_when_protected(self):
        self.ctx.protected = True
        self.assertIn(f"overlaps {self.ctx.dev_root}", self.refused(paths=[self.ctx.dev_root]))

    def test_allowed(self):
        check_access(self.ctx, ["/usr/share"], ["registry.npmjs.org", "*.example.com:8443"])


class ScanTest(unittest.TestCase):
    def setUp(self):
        self.home = _tmp_tree(self)
        for d in ("p", "work/Library/repo", "a/b/deep"):
            _touch(os.path.join(self.home, d, ".claudesandbox"))
        _touch(os.path.join(self.home, "old", ".claudeprotected"))
        for d in ("Library/app", "x/node_modules/pkg", "x/.git/y"):
            _touch(os.path.join(self.home, d, ".claudesandbox"))
        self.expected = {os.path.join(self.home, d) for d in ("p", "work/Library/repo", "a/b/deep", "old")}

    def found(self, tools) -> set:
        real_which = shutil.which
        with mock.patch.dict(os.environ, {"HOME": self.home}), \
                mock.patch("claude_secure_sandbox.scan.shutil.which",
                           side_effect=lambda t: real_which(t) if t in tools else None):
            return {os.path.dirname(f) for f in find_sandbox_files(self.home, one_file_system=True)}

    def test_every_tool_finds_them(self):
        for tools in (("fd", "fdfind"), ("rg",), ()):
            if tools and not any(shutil.which(t) for t in tools):
                continue
            with self.subTest(tools=tools):
                found = self.found(tools)
                # A repository in a directory named Library is found: only
                # ~/Library may be skipped, and only by fd.
                self.assertTrue(self.expected <= found, found)
                self.assertFalse({f for f in found if "/node_modules/" in f or "/.git/" in f})

    def test_known_repositories_and_state(self):
        state_root = os.path.join(self.home, "state")
        elsewhere = os.path.join(_tmp_tree(self), "remote-mount")
        _touch(os.path.join(elsewhere, ".claudesandbox"))
        no_longer = os.path.join(self.home, "unprotected")
        os.makedirs(no_longer)
        _touch(os.path.join(state_root, "r1", "state", "repository"), elsewhere + "\n")
        _touch(os.path.join(state_root, "r2", "state", "repository"), no_longer + "\n")
        # The wrapper's copies hold a .claudesandbox too.
        _touch(os.path.join(state_root, "r1", "work", ".claudesandbox"))
        with mock.patch.dict(os.environ, {"HOME": self.home}):
            repos = protected_repositories(state_root, [self.home])
        self.assertIn(elsewhere, repos)
        self.assertNotIn(no_longer, repos)
        self.assertFalse([r for r in repos if path_within(state_root, r)])
        self.assertTrue(self.expected <= set(repos))


class SecretsTest(unittest.TestCase):
    def test_protected_code_first(self):
        ctx = SimpleNamespace(protected_elsewhere=["/p1", "/p2"], protected=True, dev_root="/dev",
                              dev_git_common="/dev/.git", state_root="/state")
        self.assertEqual(secrets(ctx)[:4], ["/p1", "/p2", "/dev", "/dev/.git"])


class SandboxRulesTest(unittest.TestCase):
    """The srt rules for a session, built on a real directory tree."""

    def setUp(self):
        self.tmp = _tmp_tree(self)
        home = os.path.join(self.tmp, "home")
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.env = mock.patch.dict(os.environ, {"HOME": home, "PATH": "/usr/bin:/bin"})
        self.env.start()
        self.addCleanup(self.env.stop)
        os.makedirs(os.path.join(home, ".ssh"))
        state_root = os.path.join(home, ".cache", "csb")
        self.other = os.path.join(home, "work", "other")
        _touch(os.path.join(self.other, ".claudesandbox"))
        ctx = Context(root, state_root)
        ctx.dev_root = os.path.join(home, "work", "repo")
        ctx.dev_git_common = os.path.join(ctx.dev_root, ".git")
        os.makedirs(ctx.dev_git_common)
        ctx.setup_paths()
        _touch(os.path.join(state_root, "other-repo", "claude-home", "projects", "x.jsonl"))
        os.makedirs(ctx.claude_home)
        os.makedirs(ctx.copy_git)
        ctx.real_claude = _touch(os.path.join(self.tmp, "opt", "claude", "bin", "claude"))
        ctx.srt = os.path.join(root, "node_modules", ".bin", "srt")
        ctx.srt_tmp = os.path.join(self.tmp, "srt-tmp")
        ctx.protected_elsewhere = [self.other]
        ctx.protected = True
        ctx.hidden, ctx.hidden_dirs = ["core"], ["core"]
        _touch(os.path.join(ctx.work, ".claudesandbox"))
        self.ctx = ctx

    def rules(self):
        from claude_secure_sandbox.sandbox import _read_rules, _write_rules
        with mock.patch("claude_secure_sandbox.access.user_temp_dirs", return_value=[]), \
                mock.patch("claude_secure_sandbox.sandbox.user_temp_dirs", return_value=[]):
            deny_read, allow_read = _read_rules(self.ctx)
            allow_write, deny_write = _write_rules(self.ctx)
        return deny_read, allow_read, allow_write, deny_write

    def test_hidden_whatever_allows_them(self):
        ctx = self.ctx
        deny_read, allow_read, allow_write, _ = self.rules()
        for path in (self.other, ctx.dev_root, ctx.dev_git_common, ctx.state, ctx.archive,
                     os.path.join(ctx.state_root, "other-repo"), os.environ["HOME"],
                     os.path.join(os.environ["HOME"], ".ssh")):
            with self.subTest(path=path):
                self.assertIn(path, deny_read)
        # Nothing re-allowed lies in protected code or in the wrapper's state.
        for path in allow_read + allow_write:
            for secret in (self.other, ctx.dev_root, ctx.state, ctx.archive,
                           os.path.join(ctx.state_root, "other-repo")):
                with self.subTest(path=path, secret=secret):
                    self.assertFalse(path_within(secret, path))
        self.assertTrue({ctx.work, ctx.tmp, ctx.claude_home} <= set(allow_read))

    def test_written_only_in_the_session(self):
        ctx = self.ctx
        _, _, allow_write, deny_write = self.rules()
        self.assertEqual(sorted(allow_write), sorted([ctx.work, ctx.tmp, ctx.claude_home]))
        self.assertIn(os.path.join(ctx.work, ".claudesandbox"), deny_write)
        self.assertIn(os.path.join(ctx.copy_git, "hooks"), deny_write)
        self.assertIn(os.path.join(ctx.work, "core"), deny_write)

    def test_protected_repository_below_the_directory(self):
        ctx = self.ctx
        ctx.protected = False
        ctx.work = os.path.dirname(self.other)
        _, _, _, deny_write = self.rules()
        self.assertIn(self.other, deny_write)

    def test_wrapper_not_on_the_session_path(self):
        from claude_secure_sandbox.sandbox import _sandbox_env
        ctx = self.ctx
        wrapper = os.path.join(ctx.root, "bin")
        with mock.patch.dict(os.environ, {"PATH": f"{wrapper}:/usr/bin:{wrapper}/:/bin",
                                          "SSH_AUTH_SOCK": "/x"}):
            env = _sandbox_env(ctx)
        self.assertNotIn(os.path.realpath(wrapper), [os.path.realpath(d) for d in env["PATH"].split(":")])
        self.assertIn("/usr/bin", env["PATH"].split(":"))
        self.assertNotIn("SSH_AUTH_SOCK", env)
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], ctx.claude_home)


class LinkSafetyTest(unittest.TestCase):
    """The wrapper writes where Claude can plant links: it never follows them."""

    def test_regular_file(self):
        d = _tmp_tree(self)
        f = _touch(os.path.join(d, "f"))
        os.symlink(f, os.path.join(d, "link"))
        os.link(f, os.path.join(d, "hard"))
        self.assertFalse(regular_file(os.path.join(d, "link")))
        self.assertFalse(regular_file(f))
        self.assertFalse(regular_file(os.path.join(d, "missing")))
        self.assertTrue(regular_file(_touch(os.path.join(d, "g"))))

    def test_install_file_replaces_a_planted_link(self):
        d = _tmp_tree(self)
        target = _touch(os.path.join(d, "victim"), "ORIGINAL")
        dst = os.path.join(d, "dst")
        os.symlink(target, dst)
        install_file(_touch(os.path.join(d, "new"), "NEW"), dst)
        self.assertFalse(os.path.islink(dst))
        self.assertEqual(read_text(target), "ORIGINAL")
        self.assertEqual(read_text(dst), "NEW")

    def test_ensure_real_dir_replaces_a_link(self):
        d = _tmp_tree(self)
        elsewhere = os.path.join(d, "elsewhere")
        os.makedirs(elsewhere)
        path = os.path.join(d, "dir")
        os.symlink(elsewhere, path)
        with mock.patch("sys.stderr", new_callable=io.StringIO):
            ensure_real_dir(path)
        self.assertTrue(os.path.isdir(path) and not os.path.islink(path))
        self.assertTrue(os.path.isdir(elsewhere))

    def test_overlaps(self):
        self.assertTrue(overlaps("/a", "/a/b"))
        self.assertTrue(overlaps("/a/b", "/a"))
        self.assertFalse(overlaps("/a/b", "/a/bc"))


if __name__ == "__main__":
    unittest.main()
