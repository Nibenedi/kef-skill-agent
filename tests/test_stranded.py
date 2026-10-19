"""Local history that cannot go on top of the server's is set aside, and the backup
carries on from the server's tip, rebuilt from the source files."""

import os
import shutil
import unittest

from helpers import MACHINE, MACHINE2, Mac, World, clean_env, git

P = MACHINE + "/"
T0 = "2026-10-19T09:00:00-04:00"
T1 = "2026-10-19T10:00:00-04:00"
T2 = "2026-10-20T10:00:00-04:00"
T3 = "2026-10-21T10:00:00-04:00"


class StrandedTests(unittest.TestCase):
    def setUp(self):
        self.world = World(readme=True)
        self.admin = os.path.join(self.world.tmp, "admin")
        git("clone", "-q", self.world.origin, self.admin, env=clean_env(self.world.tmp))

    def tearDown(self):
        self.world.cleanup()

    def admin_git(self, *args):
        return git(*args, cwd=self.admin, env=clean_env(self.world.tmp))

    def stranded(self, mac):
        out = mac.git("for-each-ref", "--format=%(refname)", "refs/kef/")
        return [r for r in out.split() if r.startswith("refs/kef/stranded-")]

    def assert_lands(self, mac, rel, now):
        """New work written now reaches the server on this run."""
        mac.write(rel, "new work %s\n" % now)
        self.assertEqual(mac.sync(now=now).returncode, 0)
        path = mac.machine + "/" + rel
        self.assertEqual(self.world.show(path), ("new work %s\n" % now).encode(), mac.log()[-1])
        self.assertEqual(self.world.head(), mac.head())
        return mac.log()[-1]

    def test_server_side_edit_in_machine_folder(self):
        a = self.world.mac()
        a.write("skills/x/SKILL.md", "x\n")
        a.write("skills/x/leak.md", "leak\n")
        a.sync(now=T0)
        self.admin_git("pull", "-q", "origin", "main")
        self.admin_git("rm", "-q", P + "skills/x/leak.md")
        self.admin_git("commit", "-q", "-m", "admin removes a file")
        self.admin_git("push", "-q", "origin", "HEAD:main")
        line = self.assert_lands(a, "skills/new/SKILL.md", T1)
        self.assertIn("set aside as refs/kef/stranded-", line)
        self.assertEqual(len(self.stranded(a)), 1)
        self.assertIn("admin removes a file", git("--git-dir", self.world.origin, "log", "--format=%s", "main"))
        # Later runs are ordinary again.
        line = self.assert_lands(a, "skills/later/SKILL.md", T2)
        self.assertNotIn("set aside", line)
        self.assertEqual(len(self.stranded(a)), 1)

    def test_history_rewrite_on_the_server(self):
        a = self.world.mac(MACHINE, "laptop")
        b = self.world.mac(MACHINE2, "desktop")
        a.write("skills/x/SKILL.md", "x\n")
        b.write("skills/y/SKILL.md", "y\n")
        a.sync(now=T0)
        b.sync(now=T0)
        # An admin replaces the whole history with one fresh commit and force-pushes.
        self.admin_git("fetch", "-q", "origin")
        self.admin_git("checkout", "-q", "--orphan", "fresh", "origin/main")
        self.admin_git("commit", "-q", "-m", "history rewritten")
        self.admin_git("push", "-q", "--force", "origin", "fresh:main")
        self.assertEqual(self.world.count(), 1)
        self.assertIn("set aside", self.assert_lands(a, "skills/x/a2.md", T1))
        self.assertIn("set aside", self.assert_lands(b, "skills/y/b2.md", T1))
        self.assert_lands(a, "skills/x/a3.md", T2)
        files = self.world.files()
        for path in (MACHINE + "/skills/x/a2.md", MACHINE2 + "/skills/y/b2.md", MACHINE + "/skills/x/a3.md"):
            self.assertIn(path, files)

    def test_hand_commit_in_the_clone(self):
        a = self.world.mac()
        a.write("skills/x/SKILL.md", "x\n")
        a.sync(now=T0)
        with open(os.path.join(a.clone, "README.md"), "w") as handle:
            handle.write("changed by hand\n")
        a.git("commit", "-q", "-am", "hand edit")
        line = self.assert_lands(a, "skills/new/SKILL.md", T1)
        self.assertIn("outside " + MACHINE, line)
        self.assertEqual(self.world.show("README.md"), b"backup\n")
        kept = self.stranded(a)
        self.assertEqual(len(kept), 1)
        self.assertEqual(a.git("show", kept[0] + "~1:README.md"), "changed by hand\n")
        self.assert_lands(a, "skills/later/SKILL.md", T2)

    def test_duplicated_clone_with_the_same_machine_id(self):
        a = self.world.mac(MACHINE, "old-mac")
        a.write("skills/x/SKILL.md", "x\n")
        a.sync(now=T0)
        # The clone is copied to another Mac (same machine id), which has its own skills.
        b_home = os.path.join(self.world.tmp, "new-mac", "home")
        os.makedirs(os.path.join(b_home, ".claude", "skills", "x"))
        with open(os.path.join(b_home, ".claude", "skills", "x", "SKILL.md"), "w") as handle:
            handle.write("x\n")
        shutil.copytree(a.clone, os.path.join(b_home, ".kef-skills"), symlinks=True)
        b = Mac.adopt(self.world, b_home, MACHINE)
        self.assert_lands(b, "skills/b1/SKILL.md", T1)
        self.assertIn("set aside", self.assert_lands(a, "skills/a1/SKILL.md", T1))
        self.assertIn("set aside", self.assert_lands(b, "skills/b2/SKILL.md", T2))
        self.assert_lands(a, "skills/a2/SKILL.md", T3)

    def refuse_flagged(self, name_the_path):
        """A server hook that refuses any commit holding flagged.md, optionally
        naming it the way GitHub push protection does ("path: <file>:<line>")."""
        hook = os.path.join(self.world.origin, "hooks", "pre-receive")
        with open(hook, "w") as handle:
            handle.write("#!/bin/sh\n"
                         "while read old new ref; do\n"
                         "  for c in $(git rev-list \"$new\" --not --all); do\n"
                         "    p=$(git ls-tree -r --name-only \"$c\" | grep 'flagged.md$' | head -n 1)\n"
                         "    if [ -n \"$p\" ]; then\n"
                         "      echo 'error: GH013: Repository rule violations found for refs/heads/main.' >&2\n"
                         + ("      echo \"         - commit: $c\" >&2\n"
                            "      echo \"           path: $p:1\" >&2\n" if name_the_path else "") +
                         "      exit 1\n"
                         "    fi\n"
                         "  done\n"
                         "done\n")
        os.chmod(hook, 0o755)

    def test_file_the_server_names_is_left_out_and_backups_continue(self):
        self.refuse_flagged(name_the_path=True)
        a = self.world.mac()
        a.write("skills/x/SKILL.md", "x\n")
        a.sync(now=T0)
        a.write("skills/x/flagged.md", "the server refuses this\n")
        line = self.assert_lands(a, "skills/x/one.md", T1)
        self.assertIn("left out: " + P + "skills/x/flagged.md", line)
        self.assertEqual(self.world.blocked()["secrets"],
                         [{"path": P + "skills/x/flagged.md", "line": 1, "kind": "server_refused"}])
        for i, now in enumerate((T2, T3, "2026-10-22T10:00:00-04:00")):
            self.assert_lands(a, "skills/x/more%d.md" % i, now)
        self.assertNotIn(P + "skills/x/flagged.md", self.world.files())
        self.assertEqual(len(self.stranded(a)), 1)
        # Changed content is offered again; once it is gone the hold clears.
        a.remove("skills/x/flagged.md")
        self.assert_lands(a, "skills/x/last.md", "2026-10-23T10:00:00-04:00")
        self.assertIsNone(self.world.blocked())

    def test_named_file_with_spaces_in_its_path_is_left_out(self):
        self.refuse_flagged(name_the_path=True)
        a = self.world.mac()
        a.write("skills/x/SKILL.md", "x\n")
        a.sync(now=T0)
        a.write("skills/x/my notes flagged.md", "the server refuses this\n")
        self.assert_lands(a, "skills/x/one.md", T1)
        self.assertEqual(self.world.blocked()["secrets"],
                         [{"path": P + "skills/x/my notes flagged.md", "line": 1, "kind": "server_refused"}])
        self.assert_lands(a, "skills/x/two.md", T2)

    def test_server_lock_race_is_retried_not_set_aside(self):
        a = self.world.mac()
        a.write("skills/x/SKILL.md", "x\n")
        a.sync(now=T0)
        lock = os.path.join(self.world.origin, "refs", "heads", "main.lock")
        open(lock, "w").close()  # the server is busy updating main
        a.write("skills/x/two.md", "two\n")
        self.assertEqual(a.sync(now=T1).returncode, 0)
        self.assertIn("push rejected three times, will retry", a.log()[-1])
        self.assertEqual(self.stranded(a), [])
        os.unlink(lock)
        self.assert_lands(a, "skills/x/three.md", T2)
        self.assertIn(P + "skills/x/two.md", self.world.files())
        self.assertEqual(self.stranded(a), [])

    def test_unnamed_refusal_does_not_pile_up_set_aside_refs(self):
        self.refuse_flagged(name_the_path=False)
        a = self.world.mac()
        a.write("skills/x/SKILL.md", "x\n")
        a.sync(now=T0)
        a.write("skills/x/flagged.md", "the server refuses this\n")
        for i in range(6):
            a.write("skills/x/n%d.md" % i, "%d\n" % i)
            self.assertEqual(a.sync(now="2026-10-%02dT10:00:00-04:00" % (20 + i)).returncode, 0)
            self.assertIn("refused", a.log()[-1])
        self.assertEqual(len(self.stranded(a)), 1)
        a.remove("skills/x/flagged.md")
        self.assert_lands(a, "skills/x/after.md", "2026-10-27T10:00:00-04:00")

    def test_server_refusing_a_commit_does_not_block_later_backups(self):
        self.refuse_flagged(name_the_path=False)
        a = self.world.mac()
        a.write("skills/x/SKILL.md", "x\n")
        a.sync(now=T0)
        a.write("skills/x/flagged.md", "the server refuses this\n")
        self.assertEqual(a.sync(now=T1).returncode, 0)
        self.assertIn("refused", a.log()[-1])
        self.assertIn("GH013", a.log()[-1])
        a.remove("skills/x/flagged.md")
        line = self.assert_lands(a, "skills/x/later.md", T2)
        self.assertNotIn("flagged.md", "\n".join(self.world.files()))
        self.assert_lands(a, "skills/x/after.md", T3)

    def test_set_aside_refs_are_capped(self):
        a = self.world.mac()
        a.write("skills/x/SKILL.md", "x\n")
        a.sync(now=T0)
        for i in range(12):
            with open(os.path.join(a.clone, "README.md"), "w") as handle:
                handle.write("hand edit %d\n" % i)
            a.git("commit", "-q", "-am", "hand edit %d" % i)
            self.assert_lands(a, "skills/n%d/SKILL.md" % i, T1)
        self.assertEqual(len(self.stranded(a)), 10)


if __name__ == "__main__":
    unittest.main()
