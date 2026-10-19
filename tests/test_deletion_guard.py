import datetime
import os
import shutil
import unittest
from unittest import mock

from helpers import BLOCKED_KEYS, HOLD_KEYS, MACHINE, World, clean_env, load_module

P = MACHINE + "/"
T0 = "2026-10-19T09:00:00-04:00"
T1 = "2026-10-19T10:00:00-04:00"


def parse(text):
    return datetime.datetime.fromisoformat(text)


class DeletionGuardTests(unittest.TestCase):
    def setUp(self):
        self.world = World()
        self.mac = self.world.mac()

    def tearDown(self):
        self.world.cleanup()

    def seed(self, skills=12, commands=2, agents=1):
        m = self.mac
        for i in range(skills):
            m.write("skills/s%02d/SKILL.md" % i, "skill %d\n" % i)
        for i in range(commands):
            m.write("commands/c%d.md" % i, "command %d\n" % i)
        for i in range(agents):
            m.write("agents/a%d.md" % i, "agent %d\n" % i)
        m.sync(now=T0)
        return self.world.files(P)

    def assert_blocked_schema(self, blocked):
        self.assertEqual(set(blocked), BLOCKED_KEYS)
        self.assertIsInstance(blocked["secrets"], list)
        for key in ("since", "updated"):
            self.assertIsNotNone(parse(blocked[key]).utcoffset())
        hold = blocked["held_deletions"]
        if hold is not None:
            self.assertEqual(set(hold), HOLD_KEYS)
            self.assertIsInstance(hold["count"], int)
            self.assertLessEqual(len(hold["paths"]), 50)
            self.assertTrue(all(p.startswith(P) for p in hold["paths"]))
            self.assertTrue(hold["reason"].endswith("."))
            delta = parse(hold["release_after"]) - parse(hold["since"])
            self.assertEqual(delta, datetime.timedelta(days=14))

    def test_emptied_folder_holds_even_one_file(self):
        before = self.seed()
        self.mac.remove("agents/a0.md")  # agents folder now empty
        self.mac.sync(now=T1)
        self.assertEqual(self.world.files(P), sorted(before + [P + "BLOCKED"]))
        blocked = self.world.blocked()
        self.assert_blocked_schema(blocked)
        hold = blocked["held_deletions"]
        self.assertEqual(hold["count"], 1)
        self.assertEqual(hold["paths"], [P + "agents/a0.md"])
        self.assertIn("agents folder", hold["reason"])
        self.assertEqual(hold["since"], T1)
        self.assertEqual(blocked["since"], T1)
        self.assertIn("Held deletions: 1", self.world.message())

    def test_missing_source_folder_holds(self):
        before = self.seed()
        self.mac.remove("commands")
        self.mac.sync(now=T1)
        self.assertEqual(self.world.files(P), sorted(before + [P + "BLOCKED"]))
        hold = self.world.blocked()["held_deletions"]
        self.assertEqual(hold["count"], 2)
        self.assertIn("commands folder", hold["reason"])

    def test_eleven_deletions_hold(self):
        before = self.seed()
        for i in range(11):
            self.mac.remove("skills/s%02d" % i)
        self.mac.sync(now=T1)
        self.assertEqual(self.world.files(P), sorted(before + [P + "BLOCKED"]))
        blocked = self.world.blocked()
        self.assert_blocked_schema(blocked)
        self.assertEqual(blocked["held_deletions"]["count"], 11)
        self.assertIn("11 files would be deleted", blocked["held_deletions"]["reason"])

    def test_three_deletions_commit(self):
        self.seed()
        for i in range(3):
            self.mac.remove("skills/s%02d" % i)
        self.mac.sync(now=T1)
        files = self.world.files(P)
        self.assertNotIn(P + "BLOCKED", files)
        for i in range(3):
            self.assertNotIn(P + "skills/s%02d/SKILL.md" % i, files)
        self.assertIn(P + "skills/s03/SKILL.md", files)
        self.assertIn("3 deleted", self.world.message())

    def test_hold_releases_after_fourteen_days(self):
        self.seed()
        for i in range(11):
            self.mac.remove("skills/s%02d" % i)
        self.mac.sync(now=T1)
        hold = self.world.blocked()["held_deletions"]
        self.assertEqual(hold["release_after"], "2026-11-02T09:00:00-05:00")
        # Day 13: still held, the hold keeps its start.
        self.mac.sync(now="2026-11-01T10:00:00-05:00")
        self.assertEqual(self.world.blocked()["held_deletions"]["since"], T1)
        self.assertIn(P + "skills/s00/SKILL.md", self.world.files(P))
        # Day 14: released.
        self.mac.sync(now="2026-11-02T10:01:00-05:00")
        files = self.world.files(P)
        self.assertNotIn(P + "BLOCKED", files)
        for i in range(11):
            self.assertNotIn(P + "skills/s%02d/SKILL.md" % i, files)
        self.assertIn(P + "skills/s11/SKILL.md", files)
        self.assertIn("Released held deletions: 11", self.world.message())
        # The old files stay in history.
        self.assertIsNotNone(self.world.show(P + "skills/s00/SKILL.md", rev="main~1"))

    def test_files_returning_clears_the_hold(self):
        self.seed()
        self.mac.remove("commands")
        self.mac.sync(now=T1)
        self.assertIsNotNone(self.world.blocked())
        for i in range(2):
            self.mac.write("commands/c%d.md" % i, "command %d\n" % i)
        self.mac.sync(now="2026-10-19T11:00:00-04:00")
        self.assertIsNone(self.world.blocked())
        self.assertIn(P + "commands/c0.md", self.world.files(P))

    def test_additions_and_edits_commit_while_deletions_are_held(self):
        before = self.seed()
        for i in range(11):
            self.mac.remove("skills/s%02d" % i)
        self.mac.write("skills/new/SKILL.md", "new\n")
        self.mac.write("skills/s11/SKILL.md", "edited\n")
        self.mac.sync(now=T1)
        files = self.world.files(P)
        self.assertIn(P + "skills/new/SKILL.md", files)
        self.assertEqual(self.world.show(P + "skills/s11/SKILL.md"), b"edited\n")
        for path in before:
            self.assertIn(path, files)
        self.assertEqual(self.world.blocked()["held_deletions"]["count"], 11)
        self.assertIn("1 added, 1 changed, 0 deleted", self.world.message())

    def test_each_held_path_keeps_its_own_fourteen_days(self):
        self.seed(skills=12, commands=30)
        for i in range(11):
            self.mac.remove("skills/s%02d" % i)
        self.mac.sync(now=T1)  # 11 held from day 0
        day13 = "2026-11-01T10:00:00-05:00"
        self.mac.remove("commands")
        self.mac.sync(now=day13)  # 30 more held from day 13
        hold = self.world.blocked()["held_deletions"]
        self.assertEqual((hold["count"], hold["since"]), (41, T1))
        self.mac.sync(now="2026-11-02T10:01:00-05:00")  # day 14: only the first 11 are due
        files = self.world.files(P)
        for i in range(11):
            self.assertNotIn(P + "skills/s%02d/SKILL.md" % i, files)
        for i in range(30):
            self.assertIn(P + "commands/c%d.md" % i, files)
        blocked = self.world.blocked()
        self.assert_blocked_schema(blocked)
        self.assertEqual(blocked["held_deletions"]["count"], 30)
        self.assertEqual(blocked["held_deletions"]["since"], day13)
        self.assertEqual(blocked["held_deletions"]["release_after"], "2026-11-15T10:00:00-05:00")
        message = self.world.message()
        self.assertIn("Released held deletions: 11 (each held 14 days or more)", message)
        self.assertIn("0 added, 0 changed, 11 deleted", message)
        self.mac.sync(now="2026-11-15T10:01:00-05:00")  # day 27: the rest are due
        self.assertEqual([f for f in self.world.files(P) if "/commands/" in f], [])
        self.assertIsNone(self.world.blocked())
        self.assertIn("Released held deletions: 30", self.world.message())

    def test_folders_replaced_by_same_named_files_stay_held(self):
        m = self.mac
        for i in range(6):
            for j in range(4):
                m.write("skills/s%d/f%d.md" % (i, j), "%d %d\n" % (i, j))
        m.write("skills/keep/SKILL.md", "keep\n")
        m.sync(now=T0)
        for i in range(6):
            m.remove("skills/s%d" % i)
            m.write("skills/s%d" % i, "now a file\n")
        m.sync(now=T1)
        files = self.world.files(P)
        for i in range(6):
            self.assertNotIn(P + "skills/s%d" % i, files)
            for j in range(4):
                self.assertIn(P + "skills/s%d/f%d.md" % (i, j), files)
        blocked = self.world.blocked()
        self.assert_blocked_schema(blocked)
        self.assertEqual(blocked["held_deletions"]["count"], 24)
        message = self.world.message()
        self.assertIn("Held back with the deletions", message)
        self.assertIn(P + "skills/s0\n", message)
        # After 14 days the folders go and the files arrive in the same commit.
        m.sync(now="2026-11-02T10:01:00-05:00")
        files = self.world.files(P)
        for i in range(6):
            self.assertIn(P + "skills/s%d" % i, files)
            self.assertNotIn(P + "skills/s%d/f0.md" % i, files)
        self.assertIsNone(self.world.blocked())

    def test_hold_continues_after_the_folder_gets_a_new_file(self):
        self.seed(skills=1, commands=0, agents=8)
        self.mac.remove("agents")
        self.mac.sync(now=T1)
        self.assertEqual(self.world.blocked()["held_deletions"]["count"], 8)
        self.mac.write("agents/fresh.md", "fresh\n")  # the folder is no longer empty
        self.mac.sync(now="2026-10-19T11:00:00-04:00")
        files = self.world.files(P)
        self.assertIn(P + "agents/fresh.md", files)
        for i in range(8):
            self.assertIn(P + "agents/a%d.md" % i, files)
        blocked = self.world.blocked()
        self.assert_blocked_schema(blocked)
        self.assertEqual(blocked["held_deletions"]["count"], 8)
        self.assertEqual(blocked["held_deletions"]["since"], T1)
        self.assertIn("still waiting out their 14 days", blocked["held_deletions"]["reason"])
        self.assertIn("1 added, 0 changed, 0 deleted", self.world.message())

    def test_hold_continues_after_the_count_drops_to_ten_or_fewer(self):
        self.seed(skills=17, commands=0, agents=0)
        for i in range(11):
            self.mac.remove("skills/s%02d" % i)
        self.mac.sync(now="2026-10-01T10:00:00-04:00")
        for i in range(11, 16):
            self.mac.remove("skills/s%02d" % i)
        self.mac.sync(now="2026-10-06T10:00:00-04:00")  # 16 held
        self.mac.sync(now="2026-10-15T10:01:00-04:00")  # the first 11 are due
        self.assertEqual(self.world.blocked()["held_deletions"]["count"], 5)
        self.mac.sync(now="2026-10-16T10:00:00-04:00")  # 5 is under the limit, still not due
        files = self.world.files(P)
        for i in range(11, 16):
            self.assertIn(P + "skills/s%02d/SKILL.md" % i, files)
        self.assertEqual(self.world.blocked()["held_deletions"]["release_after"], "2026-10-20T10:00:00-04:00")
        self.mac.sync(now="2026-10-20T10:01:00-04:00")
        self.assertNotIn(P + "skills/s11/SKILL.md", self.world.files(P))
        self.assertIsNone(self.world.blocked())
        self.assertIn("Released held deletions: 5", self.world.message())

    def test_lost_state_file_restarts_holds_never_shortens_them(self):
        self.seed(skills=23, commands=0, agents=0)
        for i in range(11):
            self.mac.remove("skills/s%02d" % i)
        self.mac.sync(now="2026-10-01T10:00:00-04:00")
        for i in range(11, 22):
            self.mac.remove("skills/s%02d" % i)
        self.mac.sync(now="2026-10-12T10:00:00-04:00")
        os.unlink(os.path.join(self.mac.clone, ".git", "kef-sync-state.json"))
        self.mac.sync(now="2026-10-15T10:01:00-04:00")
        files = self.world.files(P)
        for i in range(22):
            self.assertIn(P + "skills/s%02d/SKILL.md" % i, files)
        hold = self.world.blocked()["held_deletions"]
        self.assertEqual(hold["count"], 22)
        self.assertEqual(hold["since"], "2026-10-15T10:01:00-04:00")

    def test_folder_deleted_mid_run_is_held_like_one_deleted_before(self):
        self.seed(skills=2, commands=0, agents=0)
        mod = load_module()
        real = mod.read_capped
        skills = self.mac.path("skills")

        def folder_goes(path):
            if os.path.isdir(skills):
                shutil.rmtree(skills)  # the whole folder disappears while the run reads it
            return real(path)

        mod.read_capped = folder_goes
        with mock.patch.dict(os.environ, clean_env(self.mac.home), clear=True):
            self.assertEqual(mod.main(["--repo", self.mac.clone, "--home", self.mac.home, "--now", T1]), 0)
        hold = self.world.blocked()["held_deletions"]
        self.assertEqual(hold["count"], 2)
        self.assertIn("The skills folder on this computer is empty or missing.", hold["reason"])
        for i in range(2):
            self.assertIn(P + "skills/s%02d/SKILL.md" % i, self.world.files(P))
        self.assertTrue(self.world.message().startswith(MACHINE + ": check-in\n"), self.world.message())
        self.assertIn("Held deletions: 2", self.world.message())

    def test_hold_lists_at_most_fifty_paths(self):
        m = self.mac
        for i in range(60):
            m.write("skills/big/f%02d.md" % i, "%d\n" % i)
        m.write("skills/keep/SKILL.md", "keep\n")
        m.sync(now=T0)
        m.remove("skills/big")
        m.sync(now=T1)
        hold = self.world.blocked()["held_deletions"]
        self.assertEqual(hold["count"], 60)
        self.assertEqual(len(hold["paths"]), 50)

    def test_unchanged_hold_does_not_commit_every_hour(self):
        self.seed()
        self.mac.remove("commands")
        self.mac.sync(now=T1)
        count = self.world.count()
        self.mac.sync(now="2026-10-19T11:00:00-04:00")
        self.mac.sync(now="2026-10-19T12:00:00-04:00")
        self.assertEqual(self.world.count(), count)


if __name__ == "__main__":
    unittest.main()
