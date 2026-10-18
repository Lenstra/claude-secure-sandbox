"""Unit tests for the pure functions of claude-secure-sandbox."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from claude_secure_sandbox.common import path_within  # noqa: E402
from claude_secure_sandbox.access import domains_overlap, local_destination, path_tools, url_host  # noqa: E402
from claude_secure_sandbox.sandbox_file import (SandboxFile, hidden_roots, is_visible,  # noqa: E402
                                                leads_to_visible, normalize_visible, sparse_escape)


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
            ".": "outside the repository root",
            "./": "the whole repository",
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
        f = SandboxFile("# comment\n[visible]\nsrc/\n\n[path]\n~/.m2\n/opt/sdk/\n"
                        "[domain]\nRegistry.npmjs.org\n*.example.com:8443\n")
        self.assertEqual(f.errors, [])
        self.assertTrue(f.has_visible)
        self.assertEqual(f.visible, {"src"})
        self.assertEqual(f.paths, {os.path.expanduser("~/.m2"), "/opt/sdk"})
        self.assertEqual(f.domains, {"registry.npmjs.org", "*.example.com:8443"})

    def test_errors(self):
        cases = {
            "secret\n": "outside a section",
            "[network]\nx.com\n": "unknown section",
            "[path]\nrelative/dir\n": "absolute path or ~/",
            "[path]\n~\n": "too broad",
            "[path]\n/\n": "too broad",
            "[path]\n~/a/../b\n": ". and .. not allowed",
            "[path]\n~/*.jar\n": "pattern not supported",
            "[domain]\n*\n": "not allowed",
            "[domain]\nhttps://x.com/\n": "invalid domain",
            "[visible]\n/etc\n": "absolute path",
            "[protected]\nsecret\n": "replaced by [visible]",
        }
        for content, message in cases.items():
            with self.subTest(content=content):
                errors = SandboxFile(content).errors
                self.assertEqual(len(errors), 1)
                self.assertIn(message, errors[0])


class VisibleTest(unittest.TestCase):
    def test_empty_section_hides_everything(self):
        f = SandboxFile("[visible]\n")
        self.assertTrue(f.has_visible)
        self.assertEqual(f.visible, set())
        self.assertFalse(SandboxFile("[domain]\nx.org\n").has_visible)

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


class PathToolsTest(unittest.TestCase):
    def test_path_tools(self):
        from types import SimpleNamespace
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        ctx = SimpleNamespace(root=root, run_dir="/nonexistent/run", state_root="/nonexistent/state",
                              protected_elsewhere=[], directory_mode=True)
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


if __name__ == "__main__":
    unittest.main()
