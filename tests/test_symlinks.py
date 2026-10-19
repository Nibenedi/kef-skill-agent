import os
import shutil
import unittest

from helpers import MACHINE, World

P = MACHINE + "/"


class SymlinkTests(unittest.TestCase):
    def setUp(self):
        self.world = World()
        self.addCleanup(self.world.cleanup)  # runs after any permission-restoring cleanup
        self.mac = self.world.mac()
        self.ext = os.path.join(self.world.tmp, "elsewhere")
        os.makedirs(self.ext)
        self.mac.write("skills/plain/SKILL.md", "plain\n")

    def ext_file(self, rel, content="x\n", size=None):
        path = os.path.join(self.ext, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(content.encode())
            if size:
                handle.truncate(size)
        return path

    def link(self, target, rel):
        path = self.mac.path(rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        os.symlink(target, path)

    def skipped(self):
        return self.world.message()

    def test_link_to_skill_folder_followed(self):
        self.ext_file("shared-skill/SKILL.md", "shared\n")
        self.ext_file("shared-skill/scripts/go.py", "print(1)\n")
        self.link(os.path.join(self.ext, "shared-skill"), "skills/shared")
        self.mac.sync()
        files = self.world.files(P)
        self.assertIn(P + "skills/shared/SKILL.md", files)
        self.assertIn(P + "skills/shared/scripts/go.py", files)
        self.assertEqual(self.world.show(P + "skills/shared/SKILL.md"), b"shared\n")

    def test_link_to_non_skill_folder_skipped_and_listed(self):
        self.ext_file("project/README.md", "not a skill\n")
        self.link(os.path.join(self.ext, "project"), "skills/project")
        self.mac.sync()
        self.assertNotIn(P + "skills/project/README.md", self.world.files(P))
        self.assertIn("Skipped:", self.skipped())
        self.assertIn(P + "skills/project (link to a folder that is not a skill)", self.skipped())
        self.assertIn(P + "skills/plain/SKILL.md", self.world.files(P))

    def test_dangling_link_skipped_and_listed(self):
        self.link(os.path.join(self.ext, "gone"), "skills/gone")
        self.mac.sync()
        self.assertIn(P + "skills/gone (link target missing)", self.skipped())

    def test_loops_are_cut(self):
        # A skill that links to itself, two skills that link to each other, and a link cycle.
        self.link("..", "skills/plain/up")
        self.ext_file("one/SKILL.md", "one\n")
        self.ext_file("two/SKILL.md", "two\n")
        os.symlink(os.path.join(self.ext, "two"), os.path.join(self.ext, "one", "other"))
        os.symlink(os.path.join(self.ext, "one"), os.path.join(self.ext, "two", "back"))
        self.link(os.path.join(self.ext, "one"), "skills/one")
        self.link(self.mac.path("skills/cyc-b"), "skills/cyc-a")
        self.link(self.mac.path("skills/cyc-a"), "skills/cyc-b")
        result = self.mac.sync()
        self.assertEqual(result.returncode, 0)
        files = self.world.files(P)
        self.assertIn(P + "skills/one/SKILL.md", files)
        self.assertIn(P + "skills/one/other/SKILL.md", files)
        message = self.skipped()
        self.assertIn(P + "skills/plain/up (link loop)", message)
        self.assertIn(P + "skills/one/other/back (link loop)", message)
        self.assertIn(P + "skills/cyc-a (link loop)", message)

    def test_oversized_followed_target_skipped(self):
        self.ext_file("huge/SKILL.md", "huge\n")
        self.ext_file("huge/a.bin", size=13 * 1024 * 1024)
        self.ext_file("huge/b.bin", size=13 * 1024 * 1024)
        self.link(os.path.join(self.ext, "huge"), "skills/huge")
        self.mac.sync()
        self.assertNotIn(P + "skills/huge/SKILL.md", self.world.files(P))
        self.assertIn(P + "skills/huge (linked skill over 25 MB, last copy kept)", self.skipped())

    def captured_linked_skill(self):
        self.ext_file("linked/SKILL.md", "linked\n")
        self.ext_file("linked/ref.md", "ref\n")
        self.ext_file("linked/script.py", "print(1)\n")
        self.link(os.path.join(self.ext, "linked"), "skills/linked")
        self.mac.sync()
        expected = [P + "skills/linked/SKILL.md", P + "skills/linked/ref.md", P + "skills/linked/script.py"]
        for path in expected:
            self.assertIn(path, self.world.files(P))
        return expected

    def assert_kept(self, expected):
        files = self.world.files(P)
        for path in expected:
            self.assertIn(path, files)
        self.assertTrue(self.world.message().split("\n")[0].endswith(", 0 deleted"), self.world.message())

    def test_linked_skill_growing_past_cap_keeps_its_backup(self):
        expected = self.captured_linked_skill()
        self.ext_file("linked/big.bin", size=26 * 1024 * 1024)
        self.mac.write("skills/plain/more.md", "more\n")
        self.mac.sync(now="2026-10-19T10:00:00-04:00")
        self.assert_kept(expected)
        self.assertIn(P + "skills/linked (linked skill over 25 MB, last copy kept)", self.skipped())
        self.assertEqual(self.world.blocked()["secrets"],
                         [{"path": P + "skills/linked", "line": 0, "kind": "over_size_cap"}])

    def test_unreadable_link_target_keeps_its_backup(self):
        expected = self.captured_linked_skill()
        os.chmod(self.ext, 0)  # the target's parent: stat through the link fails with EACCES
        self.addCleanup(os.chmod, self.ext, 0o755)
        if os.access(os.path.join(self.ext, "linked", "SKILL.md"), os.R_OK):
            self.skipTest("running as a user who can read anything")
        self.mac.write("skills/plain/more.md", "more\n")
        self.mac.sync(now="2026-10-19T10:00:00-04:00")
        self.assert_kept(expected)
        self.assertIn(P + "skills/linked (unreadable)", self.skipped())
        self.assertNotIn("link target missing", self.skipped())

    def test_unreadable_linked_skill_is_reported_while_it_goes_stale(self):
        # Stand-in for macOS privacy rules keeping launchd's python out of ~/Documents.
        self.captured_linked_skill()
        self.ext_file("linked/SKILL.md", "v2, never captured\n")
        os.chmod(self.ext, 0)
        self.addCleanup(os.chmod, self.ext, 0o755)
        if os.access(os.path.join(self.ext, "linked", "SKILL.md"), os.R_OK):
            self.skipTest("running as a user who can read anything")
        for now in ("2026-10-20T09:00:00-04:00", "2026-10-21T09:00:00-04:00", "2026-10-22T09:00:00-04:00"):
            self.assertEqual(self.mac.sync(now=now).returncode, 0)
            self.assertEqual(self.world.show(P + "LAST_SEEN"), (now + "\n").encode())
            blocked = self.world.blocked()
            self.assertEqual(blocked["secrets"], [{"path": P + "skills/linked", "line": 0, "kind": "unreadable"}])
            self.assertEqual(blocked["since"], "2026-10-20T09:00:00-04:00")
            self.assertIn("1 kept stale", self.mac.log()[-1])
        self.assertEqual(self.world.show(P + "skills/linked/SKILL.md"), b"linked\n")

    def test_skill_md_unreadable_keeps_its_backup(self):
        expected = self.captured_linked_skill()
        target = os.path.join(self.ext, "linked")
        os.chmod(target, 0o444)  # listable, but SKILL.md cannot be opened
        self.addCleanup(os.chmod, target, 0o755)
        if os.access(os.path.join(target, "SKILL.md"), os.R_OK):
            self.skipTest("running as a user who can read anything")
        self.mac.write("skills/plain/more.md", "more\n")
        self.mac.sync(now="2026-10-19T10:00:00-04:00")
        self.assert_kept(expected)
        self.assertIn(P + "skills/linked (unreadable)", self.skipped())
        self.assertNotIn("not a skill", self.skipped())

    def test_gone_link_target_is_a_normal_deletion(self):
        self.captured_linked_skill()
        shutil.rmtree(os.path.join(self.ext, "linked"))
        self.mac.sync(now="2026-10-19T10:00:00-04:00")
        self.assertNotIn(P + "skills/linked/SKILL.md", self.world.files(P))
        self.assertIn("3 deleted", self.world.message())

    def test_link_into_clone_skipped(self):
        self.mac.sync()
        self.mac.git("reset", "-q", "--hard")  # as if setup had checked this folder out
        self.link(self.mac.clone, "skills/clone")
        self.link(os.path.join(self.mac.clone, MACHINE, "skills", "plain"), "skills/plain-again")
        self.mac.write("skills/plain/more.md", "more\n")
        self.mac.sync(now="2026-10-19T10:00:00-04:00")
        message = self.skipped()
        self.assertIn(P + "skills/clone (link into the backup clone)", message)
        self.assertIn(P + "skills/plain-again (link into the backup clone)", message)
        self.assertEqual([f for f in self.world.files(P) if "again" in f or "/clone/" in f], [])
        self.assertEqual(self.world.blocked()["secrets"], [
            {"path": P + "skills/clone", "line": 0, "kind": "inside_backup_clone"},
            {"path": P + "skills/plain-again", "line": 0, "kind": "inside_backup_clone"}])

    def test_agent_file_link_followed_other_file_links_skipped(self):
        self.ext_file("agents/reviewer.md", "reviewer\n")
        self.ext_file("notes.txt", "notes\n")
        self.link(os.path.join(self.ext, "agents", "reviewer.md"), "agents/reviewer.md")
        self.link(os.path.join(self.ext, "notes.txt"), "agents/notes.md")
        self.link(os.path.join(self.ext, "agents", "reviewer.md"), "commands/reviewer.md")
        self.link(os.path.join(self.ext, "notes.txt"), "skills/plain/notes.txt")
        self.mac.sync()
        files = self.world.files(P)
        self.assertIn(P + "agents/reviewer.md", files)
        self.assertEqual(self.world.show(P + "agents/reviewer.md"), b"reviewer\n")
        self.assertNotIn(P + "agents/notes.md", files)
        self.assertNotIn(P + "commands/reviewer.md", files)
        self.assertNotIn(P + "skills/plain/notes.txt", files)
        message = self.skipped()
        self.assertIn(P + "commands/reviewer.md (link to a file)", message)
        self.assertIn(P + "skills/plain/notes.txt (link to a file)", message)
        self.assertIn(P + "agents/notes.md (link to a file)", message)

    def test_skills_linking_to_each_other_are_copied_once(self):
        names = ["s%d" % i for i in range(7)]
        for name in names:
            self.ext_file("web/%s/SKILL.md" % name, name + "\n")
        for name in names:
            for other in names:
                if other != name:
                    os.symlink(os.path.join(self.ext, "web", other), os.path.join(self.ext, "web", name, "to-" + other))
            self.link(os.path.join(self.ext, "web", name), "skills/" + name)
        result = self.mac.sync()
        self.assertEqual(result.returncode, 0)
        files = [f for f in self.world.files(P) if f.endswith("SKILL.md")]
        self.assertEqual(len(files), 8, files)  # 7 linked skills + plain, never a combinatorial blow-up
        for name in names:
            self.assertIn(P + "skills/%s/SKILL.md" % name, files)

    def test_kind_folder_linked_into_the_clone_keeps_backup_and_live_files(self):
        m = self.mac
        m.write("skills/s0/SKILL.md", "s0\n")
        m.write("skills/s1/SKILL.md", "s1\n")
        m.sync(now="2026-10-01T10:00:00-04:00")
        backed_up = self.world.files(P + "skills/")
        m.git("reset", "-q", "--hard")  # as if setup had checked this folder out
        live = os.path.join(m.clone, MACHINE, "skills")
        shutil.rmtree(m.path("skills"))
        os.symlink(live, m.path("skills"))  # the user's live folder is now inside the clone
        with open(os.path.join(live, "s0", "SKILL.md"), "w") as handle:
            handle.write("edited in place\n")
        for now in ("2026-10-01T11:00:00-04:00", "2026-10-16T11:00:00-04:00", "2026-10-30T11:00:00-04:00"):
            m.write("agents/tick.md", now + "\n")
            self.assertEqual(m.sync(now=now).returncode, 0)
            self.assertEqual(self.world.files(P + "skills/"), backed_up)
            blocked = self.world.blocked()
            self.assertIsNone(blocked["held_deletions"])
            self.assertEqual(blocked["secrets"], [{"path": P + "skills", "line": 0, "kind": "inside_backup_clone"}])
            self.assertEqual(blocked["since"], "2026-10-01T11:00:00-04:00")
            with open(os.path.join(live, "s0", "SKILL.md")) as handle:
                self.assertEqual(handle.read(), "edited in place\n")
            self.assertTrue(os.path.isfile(os.path.join(live, "s1", "SKILL.md")))
        self.assertIn(P + "skills (points into the backup clone, last copy kept)", self.world.message())

    def test_kind_folder_that_is_itself_a_link_is_copied(self):
        self.ext_file("dotfiles/agents/writer.md", "writer\n")
        os.symlink(os.path.join(self.ext, "dotfiles", "agents"), self.mac.path("agents"))
        self.mac.sync()
        self.assertIn(P + "agents/writer.md", self.world.files(P))

    def test_unreadable_folder_skipped_listed_not_fatal(self):
        locked = self.mac.path("skills/locked")
        self.mac.write("skills/locked/SKILL.md", "locked\n")
        self.mac.sync()
        self.assertIn(P + "skills/locked/SKILL.md", self.world.files(P))
        os.chmod(locked, 0)
        try:
            if os.access(locked, os.R_OK):
                self.skipTest("running as a user who can read anything")
            self.mac.write("skills/plain/new.md", "new\n")
            result = self.mac.sync(now="2026-10-19T10:00:00-04:00")
            self.assertEqual(result.returncode, 0)
            files = self.world.files(P)
            self.assertIn(P + "skills/plain/new.md", files)
            self.assertIn(P + "skills/locked/SKILL.md", files)  # kept, not deleted
            self.assertIn(P + "skills/locked (unreadable)", self.world.message())
        finally:
            os.chmod(locked, 0o755)


if __name__ == "__main__":
    unittest.main()
