import os
import time
import unittest

from helpers import MACHINE, MACHINE2, World, git, load_module

P = MACHINE + "/"
T1 = "2026-10-19T10:00:00-04:00"
T2 = "2026-10-19T11:00:00-04:00"


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.world = World()
        self.mac = self.world.mac()
        self.mac.write("skills/a/SKILL.md", "a\n")
        self.mac.sync()

    def tearDown(self):
        self.world.cleanup()

    def gitdir(self, *parts):
        return os.path.join(self.mac.clone, ".git", *parts)

    def conflicting_branches(self):
        """Two local side branches that conflict on one file outside <machine>/."""
        m = self.mac
        m.git("reset", "-q", "--hard")  # the person first brings the clone's files up to date
        m.git("checkout", "-q", "-b", "side1")
        with open(os.path.join(m.clone, "conflict.txt"), "w") as handle:
            handle.write("one\n")
        m.git("add", "conflict.txt")
        m.git("commit", "-q", "-m", "side1")
        m.git("checkout", "-q", "-b", "side2", "main")
        with open(os.path.join(m.clone, "conflict.txt"), "w") as handle:
            handle.write("two\n")
        m.git("add", "conflict.txt")
        m.git("commit", "-q", "-m", "side2")

    def assert_next_change_lands(self):
        self.mac.write("skills/a/new.md", "new\n")
        result = self.mac.sync(now=T1)
        self.assertEqual(result.returncode, 0)
        self.assertIn(P + "skills/a/new.md", self.world.files(P))
        self.assertEqual(self.world.head(), self.mac.head())
        self.assertEqual(self.mac.git("symbolic-ref", "HEAD").strip(), "refs/heads/main")
        self.assertNotIn("conflict.txt", self.world.files())

    def test_rebase_in_progress_is_aborted(self):
        self.conflicting_branches()
        self.mac.git("rebase", "side1", check=False)
        self.assertTrue(os.path.isdir(self.gitdir("rebase-merge")) or os.path.isdir(self.gitdir("rebase-apply")))
        self.assert_next_change_lands()
        self.assertFalse(os.path.exists(self.gitdir("rebase-merge")))
        self.assertFalse(os.path.exists(self.gitdir("rebase-apply")))

    def test_merge_in_progress_is_aborted(self):
        self.conflicting_branches()
        self.mac.git("merge", "side1", check=False)
        self.assertTrue(os.path.exists(self.gitdir("MERGE_HEAD")))
        self.assert_next_change_lands()
        self.assertFalse(os.path.exists(self.gitdir("MERGE_HEAD")))

    def test_cherry_pick_in_progress_is_aborted(self):
        self.conflicting_branches()
        self.mac.git("cherry-pick", "side1", check=False)
        self.assertTrue(os.path.exists(self.gitdir("CHERRY_PICK_HEAD")))
        self.assert_next_change_lands()
        self.assertFalse(os.path.exists(self.gitdir("CHERRY_PICK_HEAD")))

    def test_stale_index_lock_removed(self):
        lock = self.gitdir("index.lock")
        with open(lock, "w"):
            pass
        old = time.time() - 300
        os.utime(lock, (old, old))
        self.assert_next_change_lands()
        self.assertFalse(os.path.exists(lock))

    def test_fresh_index_lock_left_alone_backup_still_lands(self):
        lock = self.gitdir("index.lock")
        with open(lock, "w"):
            pass
        self.assert_next_change_lands()
        self.assertTrue(os.path.exists(lock))
        os.unlink(lock)

    def test_detached_head_never_costs_an_edit_in_the_clone(self):
        # A person left the clone on a detached HEAD and edited a file in it.
        m = self.mac
        m.write("skills/a/SKILL.md", "v2\n")
        m.sync(now=T1)
        m.git("checkout", "-q", "-f", "--detach", "HEAD~1")
        live = os.path.join(m.clone, MACHINE, "skills", "a", "SKILL.md")
        with open(live, "w") as handle:
            handle.write("edited by hand, saved nowhere else\n")
        m.write("skills/a/other.md", "other\n")
        self.assertEqual(m.sync(now=T2).returncode, 0)
        self.assertEqual(self.mac.git("symbolic-ref", "HEAD").strip(), "refs/heads/main")
        self.assertIn(P + "skills/a/other.md", self.world.files(P))
        with open(live) as handle:
            self.assertEqual(handle.read(), "edited by hand, saved nowhere else\n")

    def test_unpushed_commit_from_failed_push_is_pushed_next_run(self):
        m = self.mac
        origin = self.world.origin
        hidden = origin + ".away"
        os.rename(origin, hidden)
        m.write("skills/a/offline.md", "made while offline\n")
        self.assertEqual(m.sync(now=T1).returncode, 0)
        local = m.head()
        self.assertIn("will retry", m.log()[-1])
        os.rename(hidden, origin)
        self.assertNotEqual(self.world.head(), local)
        self.assertEqual(m.sync(now=T2).returncode, 0)  # no source change this run
        self.assertEqual(self.world.head(), local)
        self.assertIn(P + "skills/a/offline.md", self.world.files(P))
        self.assertTrue(m.log()[-1].startswith(T2 + " pushed"))


class ReplayFailureTests(unittest.TestCase):
    """A git call that fails during the rebase must stop the push, never build a
    commit from a half-read tree (which would drop other folders from the tip)."""

    def fake_git(self, failing):
        mod = load_module()

        class FakeGit(object):
            def run(self, *args, **kw):
                if args[0] == failing:
                    return mod.GitResult(128, b"", b"fatal: simulated failure")
                outputs = {"merge-base": b"m" * 40, "rev-list": b"c" * 40 + b"\n",
                           "ls-tree": b"100644 blob " + b"r" * 40 + b"\tREADME.md\0",
                           "mktree": b"t" * 40, "commit-tree": b"n" * 40,
                           "cat-file": b"tree " + b"t" * 40 + b"\nauthor A <a@b> 1 +0000\n\nmsg\n"}
                return mod.GitResult(0, outputs.get(args[0], b""), b"")

            def subtree(self, commit, path):
                if failing == "subtree" and commit == "c" * 40:
                    return False, None
                return True, None

        return mod, FakeGit()

    def test_failed_listing_or_commit_read_stops_the_replay(self):
        for failing in ("ls-tree", "cat-file", "subtree"):
            mod, fake = self.fake_git(failing)
            run = mod.Run("/nonexistent", "/nonexistent", mod.now_from(None), False)
            tip, problem = run.replay(fake, MACHINE, "b" * 40, "c" * 40)
            self.assertIsNone(tip, failing)
            self.assertIn("failed", problem)

    def test_server_lock_races_are_retried_not_refusals(self):
        mod = load_module()
        race = mod.GitResult(1, b"!\trefs/heads/main:refs/heads/main\t[remote rejected] (cannot lock ref 'refs/heads/main')\n", b"")
        refusal = mod.GitResult(1, b"!\trefs/heads/main:refs/heads/main\t[remote rejected] (pre-receive hook declined)\n",
                                b"remote: error: GH013: Repository rule violations found\n")
        self.assertTrue(mod.refusal_is_transient(race))
        self.assertFalse(mod.refusal_is_transient(refusal))


class TwoMachineTests(unittest.TestCase):
    def setUp(self):
        self.world = World(readme=True)

    def tearDown(self):
        self.world.cleanup()

    def test_two_machines_of_one_person_both_land(self):
        a = self.world.mac(MACHINE, "laptop")
        b = self.world.mac(MACHINE2, "desktop")
        a.write("skills/x/SKILL.md", "from laptop\n")
        b.write("skills/y/SKILL.md", "from desktop\n")
        a.sync()
        b.sync()  # b is behind: it has to rebase onto a's commit
        a.write("skills/x/SKILL.md", "laptop v2\n")
        a.sync(now=T1)
        files = self.world.files()
        self.assertIn(MACHINE + "/skills/x/SKILL.md", files)
        self.assertIn(MACHINE2 + "/skills/y/SKILL.md", files)
        self.assertIn("README.md", files)
        self.assertEqual(self.world.show(MACHINE + "/skills/x/SKILL.md"), b"laptop v2\n")
        self.assertEqual(self.world.head(), a.head())
        # Linear history, every commit touching one folder only.
        merges = git("--git-dir", self.world.origin, "rev-list", "--merges", "main").split()
        self.assertEqual(merges, [])
        self.assertEqual(self.world.count(), 4)

    def test_new_mac_linked_into_old_macs_folder_keeps_its_skills(self):
        old = self.world.mac(MACHINE, "old-mac")
        old.write("skills/x/SKILL.md", "x\n")
        old.write("skills/y/SKILL.md", "y\n")
        old.sync()
        new = self.world.mac(MACHINE2, "new-mac")  # setup checks out the old Mac's folder
        live = os.path.join(new.clone, MACHINE, "skills")
        self.assertTrue(os.path.isfile(os.path.join(live, "x", "SKILL.md")))
        os.symlink(live, new.path("skills"))  # "restore" by pointing at the backup
        new.write("agents/a.md", "a\n")
        new.sync()
        old.remove("skills/x")
        old.sync(now=T1)
        self.assertNotIn(MACHINE + "/skills/x/SKILL.md", self.world.files())
        new.write("agents/b.md", "b\n")
        self.assertEqual(new.sync(now=T2).returncode, 0)
        self.assertIn(MACHINE2 + "/agents/b.md", self.world.files())
        self.assertTrue(os.path.isfile(os.path.join(new.path("skills"), "x", "SKILL.md")))
        self.assertEqual(self.world.blocked(MACHINE2)["secrets"],
                         [{"path": MACHINE2 + "/skills", "line": 0, "kind": "inside_backup_clone"}])


if __name__ == "__main__":
    unittest.main()
