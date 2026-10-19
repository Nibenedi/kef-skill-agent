import errno
import fcntl
import os
import random
import re
import string
import subprocess
import unittest
from unittest import mock

from helpers import MACHINE, MACHINE2, NOW, World, clean_env, git, load_module, snapshot

P = MACHINE + "/"


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.world = World()
        self.mac = self.world.mac()

    def tearDown(self):
        self.world.cleanup()

    def test_copies_three_kinds_skips_junk_keeps_exec_bit(self):
        m = self.mac
        m.write("skills/demo/SKILL.md", "# demo\n")
        m.write("skills/demo/scripts/run.sh", "#!/bin/sh\necho hi\n", 0o755)
        m.write("commands/report.md", "report\n")
        m.write("agents/helper.md", "helper\n")
        for junk in ("skills/demo/.DS_Store", "skills/demo/__pycache__/a.cpython-39.pyc",
                     "skills/demo/lib.pyc", "skills/demo/node_modules/x/index.js",
                     "skills/demo/.venv/bin/python", "skills/demo/venv/lib/x.py",
                     "skills/demo/.git/config", "skills/demo/sub/.git/HEAD"):
            m.write(junk, "junk\n")
        self.assertEqual(m.sync().returncode, 0)
        self.assertEqual(self.world.files(), [
            P + "LAST_SEEN", P + "agents/helper.md", P + "commands/report.md",
            P + "skills/demo/SKILL.md", P + "skills/demo/scripts/run.sh"])
        self.assertEqual(self.world.mode(P + "skills/demo/scripts/run.sh"), "100755")
        self.assertEqual(self.world.mode(P + "skills/demo/SKILL.md"), "100644")
        self.assertNotIn("junk", self.world.message())
        self.assertTrue(m.log()[-1].startswith(NOW + " pushed "), m.log())
        # The clone's own files are never written after setup.
        self.assertFalse(os.path.exists(os.path.join(m.clone, MACHINE)))

    def test_edits_and_small_deletions_commit(self):
        m = self.mac
        m.write("skills/a/SKILL.md", "v1\n")
        m.write("skills/a/notes.md", "n\n")
        m.sync()
        m.write("skills/a/SKILL.md", "v2\n")
        m.remove("skills/a/notes.md")
        m.sync(now="2026-10-19T10:00:00-04:00")
        self.assertEqual(self.world.show(P + "skills/a/SKILL.md"), b"v2\n")
        self.assertNotIn(P + "skills/a/notes.md", self.world.files())
        self.assertIn("1 changed, 1 deleted", self.world.message())

    def test_missing_source_folders_are_skipped(self):
        self.mac.write("skills/a/SKILL.md", "a\n")
        self.assertEqual(self.mac.sync().returncode, 0)
        self.assertEqual(self.world.files(), [P + "LAST_SEEN", P + "skills/a/SKILL.md"])
        self.assertIsNone(self.world.blocked())

    def test_oversized_file_skipped_listed_rest_commits(self):
        m = self.mac
        m.write("skills/a/SKILL.md", "a\n")
        big = m.write("skills/a/model.bin", b"")
        with open(big, "r+b") as handle:
            handle.truncate(21 * 1024 * 1024)
        m.sync()
        self.assertEqual(self.world.files(), [P + "BLOCKED", P + "LAST_SEEN", P + "skills/a/SKILL.md"])
        self.assertIn("over 20 MB", self.world.message())
        self.assertIn(P + "skills/a/model.bin", self.world.message())
        self.assertEqual(self.world.blocked()["secrets"],
                         [{"path": P + "skills/a/model.bin", "line": 0, "kind": "over_size_cap"}])
        self.assertIn("1 kept stale", m.log()[-1])

    def test_lock_held_exits_zero_and_changes_nothing(self):
        m = self.mac
        m.write("skills/a/SKILL.md", "a\n")
        lock_path = os.path.join(m.clone, ".git", "kef-sync.lock")
        with open(lock_path, "a") as holder:
            fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            before = snapshot(m.home)
            result = m.sync()
            after = snapshot(m.home)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(before, after)
        self.assertIsNone(self.world.head())
        self.assertIsNone(m.head())

    def test_last_seen_once_per_local_day(self):
        m = self.mac
        m.write("skills/a/SKILL.md", "a\n")
        m.sync(now="2026-10-19T09:00:00-04:00")
        self.assertEqual(self.world.show(P + "LAST_SEEN"), b"2026-10-19T09:00:00-04:00\n")
        count = self.world.count()
        # Later the same local day (02:00 UTC on the 20th is 22:00 on the 19th in New York).
        m.sync(now="2026-10-20T02:00:00+00:00")
        self.assertEqual(self.world.count(), count)
        m.sync(now="2026-10-19T23:59:00-04:00")
        self.assertEqual(self.world.count(), count)
        # Next local day.
        m.sync(now="2026-10-20T00:30:00-04:00")
        self.assertEqual(self.world.count(), count + 1)
        last_seen = self.world.show(P + "LAST_SEEN").decode()
        self.assertEqual(last_seen, "2026-10-20T00:30:00-04:00\n")
        self.assertRegex(last_seen, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d\n$")
        self.assertIn("check-in", self.world.message())

    def test_last_seen_uses_offset_of_local_zone(self):
        self.mac.write("skills/a/SKILL.md", "a\n")
        self.mac.sync(now="2026-10-19T13:00:00+00:00")
        self.assertEqual(self.world.show(P + "LAST_SEEN"), b"2026-10-19T09:00:00-04:00\n")

    def test_offline_origin_exits_zero_and_logs(self):
        m = self.mac
        m.write("skills/a/SKILL.md", "a\n")
        m.git("remote", "set-url", "origin", os.path.join(self.world.tmp, "missing.git"))
        result = m.sync()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")
        self.assertEqual(len(m.log()), 1)
        self.assertIn("will retry", m.log()[0])
        self.assertIsNotNone(m.head())  # committed locally, pushed later

    def test_log_is_capped(self):
        m = self.mac
        m.write("skills/a/SKILL.md", "a\n")
        log_path = os.path.join(m.clone, ".git", "kef-sync.log")
        with open(log_path, "w") as handle:
            for i in range(30000):
                handle.write("2026-10-01T00:00:00-04:00 filler line %05d\n" % i)
        self.assertGreater(os.path.getsize(log_path), 1024 * 1024)
        m.sync()
        self.assertLess(os.path.getsize(log_path), 1024 * 1024)
        lines = m.log()
        self.assertTrue(lines[0].startswith("2026-10-01T00:00:00-04:00 filler line"))
        self.assertTrue(lines[-1].startswith(NOW + " pushed"))

    def test_nothing_written_outside_clone(self):
        m = self.mac
        m.write("skills/a/SKILL.md", "a\n")
        m.write("agents/b.md", "b\n")
        for now in ("2026-10-21T09:00:00-04:00", "2026-10-22T09:00:00-04:00"):
            m.write("skills/a/SKILL.md", now + "\n")
            in_home = snapshot(m.home, exclude=m.clone)
            outside = snapshot(self.world.tmp, exclude=m.home)
            self.assertEqual(m.sync(now=now).returncode, 0)
            self.assertTrue(m.log()[-1].startswith(now + " pushed"))
            self.assertEqual(snapshot(m.home, exclude=m.clone), in_home)
            after = snapshot(self.world.tmp, exclude=m.home)
            # Beyond the clone, only the origin (written by the push) may change.
            changed = set(k for k in set(after) | set(outside) if after.get(k) != outside.get(k))
            self.assertTrue(changed)
            self.assertTrue(all(k.startswith("origin.git") for k in changed), sorted(changed)[:5])

    def test_git_runs_without_any_way_to_prompt(self):
        mod = load_module()
        env = mod.Git(self.mac.clone, False).env
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(env["GIT_ASKPASS"], "")
        self.assertEqual(env["GCM_INTERACTIVE"], "never")
        for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
            self.assertNotIn(key, env)

    def test_clone_files_never_written_after_setup(self):
        # Another computer's work arrives, deletions are held, a secret is left
        # out: none of it may touch a file in the clone outside .git.
        world = World(readme=True)
        self.addCleanup(world.cleanup)
        other = world.mac(MACHINE2, "other")
        other.write("skills/o/SKILL.md", "o\n")
        other.sync()
        mac = world.mac(MACHINE, "this")
        for i in range(12):
            mac.write("skills/s%02d/SKILL.md" % i, "%d\n" % i)
        mac.sync()
        before = snapshot(mac.clone, exclude=os.path.join(mac.clone, ".git"))
        other.write("skills/o/more.md", "more\n")
        other.sync(now="2026-10-19T10:00:00-04:00")
        for i in range(11):
            mac.remove("skills/s%02d" % i)
        rng = random.Random(7)
        token = "gh" + "p_" + "".join(rng.choice(string.ascii_letters + string.digits) for _ in range(36))
        mac.write("skills/s11/SKILL.md", token + "\n")
        for now in ("2026-10-19T11:00:00-04:00", "2026-10-20T11:00:00-04:00"):
            self.assertEqual(mac.sync(now=now).returncode, 0)
        self.assertIn(MACHINE2 + "/skills/o/more.md", world.files())
        self.assertIsNotNone(world.blocked())
        self.assertEqual(snapshot(mac.clone, exclude=os.path.join(mac.clone, ".git")), before)

    def run_in_process(self, mod, now):
        env = clean_env(self.mac.home)
        with mock.patch.dict(os.environ, env, clear=True):
            return mod.main(["--repo", self.mac.clone, "--home", self.mac.home, "--now", now])

    def test_file_vanishing_between_scan_and_read_is_an_ordinary_deletion(self):
        m = self.mac
        m.write("skills/a/SKILL.md", "a\n")
        m.sync()
        count = self.world.count()
        m.write("skills/a/.SKILL.md.swp", "editor temp\n")
        mod = load_module()
        real = mod.read_capped

        def vanishing(path):
            if path.endswith(".swp") and os.path.exists(path):
                os.unlink(path)  # the editor closes and removes its temp file mid-run
            return real(path)

        mod.read_capped = vanishing
        self.assertEqual(self.run_in_process(mod, "2026-10-19T10:00:00-04:00"), 0)
        self.assertIsNone(self.world.blocked())
        self.assertEqual(self.world.count(), count)
        self.assertTrue(m.log()[-1].startswith("2026-10-19T10:00:00-04:00 no changes"), m.log()[-1])

    def test_file_vanishing_during_the_scan_is_an_ordinary_deletion(self):
        m = self.mac
        m.write("skills/a/SKILL.md", "a\n")
        m.sync()
        count = self.world.count()
        m.write("skills/a/.SKILL.md.swp", "editor temp\n")
        mod = load_module()
        real_scandir = os.scandir

        class Gone(object):
            """A directory entry whose file is deleted before it can be examined."""

            def __init__(self, entry):
                self.name, self.path = entry.name, entry.path

            def is_symlink(self):
                return False

            def is_dir(self, follow_symlinks=True):
                return False

            def is_file(self, follow_symlinks=True):
                return True

            def stat(self, follow_symlinks=True):
                raise FileNotFoundError(errno.ENOENT, "No such file or directory", self.path)

        class Listing(object):
            def __init__(self, path):
                self.entries = list(real_scandir(path))

            def __enter__(self):
                return [Gone(e) if e.name.endswith(".swp") else e for e in self.entries]

            def __exit__(self, *exc):
                return False

        with mock.patch.object(mod.os, "scandir", Listing):
            self.assertEqual(self.run_in_process(mod, "2026-10-19T10:00:00-04:00"), 0)
        self.assertIsNone(self.world.blocked())
        self.assertEqual(self.world.count(), count)

    def test_unreadable_single_file_keeps_its_copy_and_is_reported(self):
        m = self.mac
        path = m.write("skills/a/notes.md", "v1\n")
        m.write("skills/a/SKILL.md", "a\n")
        m.sync()
        with open(path, "w") as handle:
            handle.write("v2\n")
        os.chmod(path, 0)
        try:
            if os.access(path, os.R_OK):
                self.skipTest("running as a user who can read anything")
            m.write("skills/a/SKILL.md", "a2\n")
            m.sync(now="2026-10-19T10:00:00-04:00")
        finally:
            os.chmod(path, 0o644)
        self.assertEqual(self.world.show(P + "skills/a/notes.md"), b"v1\n")
        self.assertEqual(self.world.show(P + "skills/a/SKILL.md"), b"a2\n")
        self.assertEqual(self.world.blocked()["secrets"],
                         [{"path": P + "skills/a/notes.md", "line": 0, "kind": "unreadable"}])

    def test_index_follows_main_but_files_do_not(self):
        world = World(readme=True)
        self.addCleanup(world.cleanup)
        other = world.mac(MACHINE2, "other")
        other.write("skills/o/SKILL.md", "o\n")
        mac = world.mac(MACHINE, "this")
        mac.write("skills/a/SKILL.md", "a\n")
        other.sync()
        mac.sync()  # replays onto the other Mac's commit
        clean = subprocess.run(["git", "diff-index", "--cached", "--quiet", "HEAD", "--"], cwd=mac.clone,
                               env=clean_env(mac.home))
        self.assertEqual(clean.returncode, 0)
        self.assertFalse(os.path.exists(os.path.join(mac.clone, MACHINE)))
        self.assertFalse(os.path.exists(os.path.join(mac.clone, MACHINE2)))

    def test_verbose_reports_on_stderr_only(self):
        self.mac.write("skills/a/SKILL.md", "a\n")
        result = self.mac.sync(extra=["--verbose"])
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")
        self.assertIn(b"pushed", result.stderr)

    def test_identity_fallback_without_any_git_identity(self):
        world = World()
        try:
            mac = world.mac(identity=False)
            mac.write("skills/a/SKILL.md", "a\n")
            mac.sync()
            author = git("--git-dir", world.origin, "log", "-1", "--format=%an <%ae>", "main").strip()
            self.assertEqual(author, "kef skill backup <kef-skill-backup@users.noreply.github.com>")
        finally:
            world.cleanup()


class StagingTests(unittest.TestCase):
    """Commits only ever touch <machine>/."""

    def setUp(self):
        self.world = World(readme=True)
        self.mac = self.world.mac()

    def tearDown(self):
        self.world.cleanup()

    def changed_paths(self, rev):
        out = git("--git-dir", self.world.origin, "diff-tree", "-r", "--root", "--name-only",
                  "--no-commit-id", "-z", rev)
        return [p for p in out.split("\0") if p]

    def test_first_push_into_readme_only_repo_keeps_readme(self):
        self.mac.write("skills/a/SKILL.md", "a\n")
        self.mac.sync()
        self.assertEqual(self.world.files(), ["README.md", P + "LAST_SEEN", P + "skills/a/SKILL.md"])
        self.assertEqual(self.world.count(), 2)

    def test_stray_and_modified_files_in_clone_are_never_staged(self):
        m = self.mac
        with open(os.path.join(m.clone, "stray.txt"), "w") as handle:
            handle.write("stray\n")
        with open(os.path.join(m.clone, "README.md"), "a") as handle:
            handle.write("local edit\n")
        m.git("add", "README.md")  # even something already staged stays out
        m.write("skills/a/SKILL.md", "a\n")
        m.sync()
        for rev in git("--git-dir", self.world.origin, "rev-list", "main").split():
            if self.world.message(rev).startswith("readme"):
                continue
            paths = self.changed_paths(rev)
            self.assertTrue(paths)
            self.assertTrue(all(p.startswith(P) for p in paths), paths)
        self.assertEqual(self.world.show("README.md"), b"backup\n")
        self.assertNotIn("stray.txt", self.world.files())
        self.assertTrue(os.path.exists(os.path.join(m.clone, "stray.txt")))


if __name__ == "__main__":
    unittest.main()
