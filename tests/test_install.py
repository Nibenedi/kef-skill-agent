"""install.sh end to end: fake HOME, local bare repos, a stub launchctl."""

import fcntl
import hashlib
import os
import plistlib
import re
import shutil
import stat
import subprocess
import tempfile
import time
import unittest

from helpers import INSTALLER, ROOT, World, clean_env, git, snapshot

FAKE_UUID = "11111111-2222-3333-4444-555555555555"
STUB = """import datetime, os, sys
repo = sys.argv[sys.argv.index("--repo") + 1]
with open(os.path.join(repo, ".git", "kef-sync.log"), "a") as handle:
    handle.write(datetime.datetime.now().astimezone().isoformat(timespec="seconds") + " %s\\n")
"""
MACHINE = "mac-" + hashlib.sha256(FAKE_UUID.encode()).hexdigest()[:12]
READY = re.compile(r"^SKILL_BACKUP=READY tester (mac-[0-9a-f]{12}) ([0-9a-f]{7,})$")


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="kefinstall-"))
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(os.path.join(self.home, ".claude", "skills", "demo"))
        with open(os.path.join(self.home, ".claude", "skills", "demo", "SKILL.md"), "w") as handle:
            handle.write("# demo\n")
        # The agent repo, tagged, built from this working copy.
        self.agent_origin = os.path.join(self.tmp, "kef-skill-agent.git")
        git("init", "-q", "--bare", "-b", "main", self.agent_origin)
        work = os.path.join(self.tmp, "agent-work")
        git("clone", "-q", self.agent_origin, work)
        for name in ("kef_skill_sync.py", "install.sh", "README.md"):
            shutil.copy(os.path.join(ROOT, name), os.path.join(work, name))
        git("add", ".", cwd=work)
        git("commit", "-q", "-m", "agent", cwd=work)
        git("tag", "test-ref", cwd=work)
        git("push", "-q", "origin", "HEAD:refs/heads/main", "refs/tags/test-ref", cwd=work)
        shutil.rmtree(work)
        # The person repo: README only.
        self.world = World(readme=True)
        self.person_origin = self.world.origin
        # launchctl stub that records its arguments; bootout "fails" like a first install.
        self.calls = os.path.join(self.tmp, "launchctl.calls")
        self.stub = os.path.join(self.tmp, "launchctl")
        with open(self.stub, "w") as handle:
            handle.write('#!/bin/bash\necho "$@" >> "%s"\n[ "$1" = bootout ] && exit 3\nexit 0\n' % self.calls)
        os.chmod(self.stub, 0o755)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.world.cleanup()

    def env(self, **over):
        env = clean_env(self.home)
        env.update({"KEF_AGENT_REPO_URL": self.agent_origin, "KEF_AGENT_REF": "test-ref",
                    "KEF_PERSON_REPO_URL": self.person_origin, "KEF_LAUNCHCTL": self.stub,
                    "KEF_SKIP_IOREG": FAKE_UUID})
        env.update(over)
        return env

    def install(self, ident="tester", **over):
        # As the README runs it: the script arrives on stdin.
        with open(INSTALLER, "rb") as script:
            code = script.read()
        return subprocess.run(["/bin/bash", "-s", "--", ident], input=code, env=self.env(**over),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300)

    def lines(self, result):
        return result.stdout.decode().strip().splitlines()

    def plist_path(self):
        return os.path.join(self.home, "Library", "LaunchAgents", "com.kef.skill-backup.plist")

    def test_end_to_end_ready_plist_and_launchctl(self):
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        lines = self.lines(result)
        self.assertEqual(len(lines), 2, lines)
        self.assertEqual(lines[-2], "Setup finished. Paste the next line into your setup task:")
        match = READY.match(lines[-1])
        self.assertIsNotNone(match, lines)
        self.assertEqual(match.group(1), MACHINE)
        self.assertEqual(len([l for l in lines if l.startswith("SKILL_BACKUP=")]), 1)
        clone = os.path.join(self.home, ".kef-skills")
        agent = os.path.join(self.home, ".kef-skill-agent")
        head = git("rev-parse", "HEAD", cwd=clone).strip()
        self.assertTrue(head.startswith(match.group(2)))
        self.assertEqual(self.world.head(), head)
        self.assertIn(MACHINE + "/skills/demo/SKILL.md", self.world.files())
        with open(os.path.join(clone, ".git", "kef-machine-id")) as handle:
            self.assertEqual(handle.read(), MACHINE + "\n")
        self.assertEqual(git("config", "--local", "--get", "user.name", cwd=clone).strip(), "tester skill backup")
        self.assertEqual(git("config", "--local", "--get", "user.email", cwd=clone).strip(),
                         "kef-skill-backup@users.noreply.github.com")
        tag = git("rev-parse", "test-ref^{commit}", cwd=agent).strip()
        self.assertEqual(git("rev-parse", "HEAD", cwd=agent).strip(), tag)

        with open(self.plist_path(), "rb") as handle:
            plist = plistlib.load(handle)
        self.assertEqual(plist, {
            "Label": "com.kef.skill-backup",
            "ProgramArguments": ["/usr/bin/python3", os.path.join(agent, "kef_skill_sync.py"), "--repo", clone],
            "StartInterval": 3600,
            "RunAtLoad": True,
            "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
                                     "HOME": self.home},
            "StandardOutPath": os.path.join(clone, ".git", "kef-sync.launchd.log"),
            "StandardErrorPath": os.path.join(clone, ".git", "kef-sync.launchd.log"),
            "ProcessType": "Background",
            "LowPriorityIO": True,
        })
        if shutil.which("plutil"):
            lint = subprocess.run(["plutil", "-lint", self.plist_path()], stdout=subprocess.PIPE)
            self.assertEqual(lint.returncode, 0)
        uid = os.getuid()
        with open(self.calls) as handle:
            calls = handle.read().splitlines()
        self.assertEqual(calls, ["bootout gui/%d/com.kef.skill-backup" % uid,
                                 "bootstrap gui/%d %s" % (uid, self.plist_path())])
        self.assertFalse([n for n in os.listdir(os.path.dirname(self.plist_path())) if ".tmp." in n])

    def test_second_run_is_idempotent(self):
        first = self.install()
        self.assertEqual(first.returncode, 0, first.stderr.decode())
        with open(self.plist_path(), "rb") as handle:
            plist_before = handle.read()
        # A new agent version on the server, picked up by re-running with the new ref.
        work = os.path.join(self.tmp, "agent-work2")
        git("clone", "-q", self.agent_origin, work)
        with open(os.path.join(work, "VERSION"), "w") as handle:
            handle.write("2\n")
        git("add", "VERSION", cwd=work)
        git("commit", "-q", "-m", "v2", cwd=work)
        git("tag", "test-ref-2", cwd=work)
        git("push", "-q", "origin", "HEAD:refs/heads/main", "refs/tags/test-ref-2", cwd=work)
        second = self.install(KEF_AGENT_REF="test-ref-2")
        self.assertEqual(second.returncode, 0, second.stderr.decode())
        m1 = READY.match(self.lines(first)[-1])
        m2 = READY.match(self.lines(second)[-1])
        self.assertEqual(m1.group(1), m2.group(1))
        agent = os.path.join(self.home, ".kef-skill-agent")
        self.assertTrue(os.path.isfile(os.path.join(agent, "VERSION")))
        with open(self.plist_path(), "rb") as handle:
            self.assertEqual(handle.read(), plist_before)
        machines = set(p.split("/")[0] for p in self.world.files() if p.startswith("mac-"))
        self.assertEqual(machines, {MACHINE})
        with open(self.calls) as handle:
            calls = handle.read().splitlines()
        self.assertEqual([c.split()[0] for c in calls], ["bootout", "bootstrap", "bootout", "bootstrap"])

    def test_empty_person_repo_first_push(self):
        empty = os.path.join(self.tmp, "kef-skills-empty.git")
        git("init", "-q", "--bare", "-b", "main", empty)
        result = self.install(KEF_PERSON_REPO_URL=empty)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        match = READY.match(self.lines(result)[-1])
        self.assertIsNotNone(match)
        files = git("--git-dir", empty, "ls-tree", "-r", "--name-only", "main").split()
        self.assertEqual(files, [MACHINE + "/LAST_SEEN", MACHINE + "/skills/demo/SKILL.md"])

    def push_agent_tag(self, tag, files):
        work = os.path.join(self.tmp, "agent-" + tag)
        git("clone", "-q", self.agent_origin, work)
        for name, text in files.items():
            with open(os.path.join(work, name), "w") as handle:
                handle.write(text)
        git("add", ".", cwd=work)
        git("commit", "-q", "-m", tag, cwd=work)
        git("tag", tag, cwd=work)
        git("push", "-q", "origin", "refs/tags/" + tag, cwd=work)
        shutil.rmtree(work)

    def test_sync_that_does_nothing_gives_no_ready_line(self):
        # The clone of a README-only repo already equals the server before any sync,
        # so "local HEAD equals the server" alone would wrongly pass.
        self.push_agent_tag("broken", {"kef_skill_sync.py": "import sys\nsys.exit(0)\n"})
        result = self.install(KEF_AGENT_REF="broken")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(b"SKILL_BACKUP=READY", result.stdout)
        err = result.stderr.decode().strip().splitlines()
        self.assertEqual(len(err), 1)
        self.assertIn("did not reach GitHub", err[0])
        self.assertEqual(self.world.files(), ["README.md"])

    def test_sync_claiming_success_without_pushing_gives_no_ready_line(self):
        # The log line alone is not proof: the server must hold this Mac's folder.
        for tag, outcome in (("says-no-changes", "no changes"), ("says-pushed", "pushed abc1234 (1 added)")):
            self.push_agent_tag(tag, {"kef_skill_sync.py": STUB % outcome})
            result = self.install(KEF_AGENT_REF=tag)
            self.assertNotEqual(result.returncode, 0, tag)
            self.assertNotIn(b"SKILL_BACKUP=READY", result.stdout)
            self.assertIn("did not reach GitHub", result.stderr.decode())
            self.assertIn(outcome, result.stderr.decode())
            self.assertEqual(self.world.files(), ["README.md"])

    def test_push_rejected_gives_no_ready_line(self):
        hook = os.path.join(self.person_origin, "hooks", "pre-receive")
        with open(hook, "w") as handle:
            handle.write("#!/bin/sh\necho rejected by test >&2\nexit 1\n")
        os.chmod(hook, 0o755)
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(b"SKILL_BACKUP=READY", result.stdout)
        self.assertIn("did not reach GitHub", result.stderr.decode())
        self.assertEqual(self.world.files(), ["README.md"])

    def test_rerun_waits_for_a_run_in_progress(self):
        self.assertEqual(self.install().returncode, 0)
        skill = os.path.join(self.home, ".claude", "skills", "second", "SKILL.md")
        os.makedirs(os.path.dirname(skill))
        with open(skill, "w") as handle:
            handle.write("second\n")
        lock = open(os.path.join(self.home, ".kef-skills", ".git", "kef-sync.lock"), "a")
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        script = open(INSTALLER, "rb")
        self.addCleanup(script.close)
        proc = subprocess.Popen(["/bin/bash", "-s", "--", "tester"], stdin=script,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env())
        time.sleep(4)  # the hourly run "holds the lock" for a while
        self.assertIsNone(proc.poll())
        lock.close()
        out, err = proc.communicate(timeout=300)
        self.assertEqual(proc.returncode, 0, err.decode())
        self.assertIsNotNone(READY.match(out.decode().strip().splitlines()[-1]))
        self.assertIn(MACHINE + "/skills/second/SKILL.md", self.world.files())

    def test_rerun_with_a_url_rewrite_in_git_config(self):
        # Staff who rewrite GitHub URLs (url.<base>.insteadOf) must be able to re-run.
        served = os.path.join(self.tmp, "served")
        os.makedirs(served)
        git("clone", "-q", "--bare", self.person_origin, os.path.join(served, "kef-skills-tester.git"))
        with open(os.path.join(self.home, ".gitconfig"), "w") as handle:
            handle.write('[url "%s/"]\n\tinsteadOf = https://example.invalid/\n' % served)
        url = "https://example.invalid/kef-skills-tester.git"
        first = self.install(KEF_PERSON_REPO_URL=url)
        self.assertEqual(first.returncode, 0, first.stderr.decode())
        second = self.install(KEF_PERSON_REPO_URL=url)
        self.assertEqual(second.returncode, 0, second.stderr.decode())
        self.assertIsNotNone(READY.match(self.lines(second)[-1]))

    def test_wrong_remote_stops_without_changes(self):
        other = os.path.join(self.tmp, "other.git")
        git("init", "-q", "--bare", "-b", "main", other)
        git("clone", "-q", other, os.path.join(self.home, ".kef-skills"))
        before = snapshot(self.tmp)
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(b"SKILL_BACKUP=READY", result.stdout)
        err = result.stderr.decode().strip().splitlines()
        self.assertEqual(len(err), 1)
        self.assertIn("already exists and points at", err[0])
        self.assertEqual(snapshot(self.tmp), before)

    def test_unreachable_person_repo_stops_without_changes(self):
        before = snapshot(self.tmp)
        result = self.install(KEF_PERSON_REPO_URL=os.path.join(self.tmp, "no-such-repo.git"))
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(b"SKILL_BACKUP=READY", result.stdout)
        err = result.stderr.decode().strip().splitlines()
        self.assertEqual(len(err), 1)
        self.assertIn("https://github.com/Nibenedi/kef-skills-tester/invitations", err[0])
        self.assertIn("gh auth status", err[0])
        self.assertIn("gh auth setup-git", err[0])
        self.assertEqual(snapshot(self.tmp), before)

    def test_invalid_id_stops(self):
        before = snapshot(self.tmp)
        for bad in ("", "Bad", "1abc", "a", "has space", "x" * 40, "ab\nBAD ID", "tester\n"):
            result = self.install(ident=bad)
            self.assertNotEqual(result.returncode, 0, bad)
            self.assertIn(b"usage:", result.stderr)
        self.assertEqual(snapshot(self.tmp), before)

    def test_shell_syntax(self):
        self.assertEqual(subprocess.run(["/bin/bash", "-n", INSTALLER]).returncode, 0)
        mode = os.stat(INSTALLER).st_mode
        self.assertTrue(mode & stat.S_IRUSR)


if __name__ == "__main__":
    unittest.main()
