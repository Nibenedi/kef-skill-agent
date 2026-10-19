"""Shared fixtures: temp dirs, a local bare repo as origin, a fake HOME per Mac."""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "kef_skill_sync.py")
INSTALLER = os.path.join(ROOT, "install.sh")
MACHINE = "mac-aaaaaaaaaaaa"
MACHINE2 = "mac-bbbbbbbbbbbb"
NOW = "2026-10-19T09:00:00-04:00"
ID_ARGS = ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid"]
BLOCKED_KEYS = {"since", "held_deletions", "secrets", "updated"}
HOLD_KEYS = {"reason", "count", "paths", "since", "release_after"}


def load_module():
    spec = importlib.util.spec_from_file_location("kef_skill_sync", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def clean_env(home):
    env = dict(os.environ)
    for key in list(env):
        if key.startswith("GIT_") or key in ("XDG_CONFIG_HOME", "KEF_AGENT_REF"):
            env.pop(key)
    env.update({"HOME": home, "TZ": "America/New_York", "GIT_CONFIG_NOSYSTEM": "1",
                "LC_ALL": "C", "PYTHONDONTWRITEBYTECODE": "1"})
    return env


def git(*args, **kw):
    cwd = kw.get("cwd")
    env = kw.get("env") or clean_env(kw.get("home", "/nonexistent-home"))
    proc = subprocess.run(["git"] + ID_ARGS + list(args), cwd=cwd, env=env,
                          input=kw.get("input"), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if kw.get("check", True) and proc.returncode != 0:
        raise AssertionError("git %s failed: %s" % (" ".join(args), proc.stderr.decode()))
    return proc.stdout.decode("utf-8", "replace")


def snapshot(top, exclude=None):
    """Every path under top with its type, size and mtime (exclude: one subtree)."""
    seen = {}
    for dirpath, dirnames, filenames in os.walk(top):
        if exclude and (dirpath == exclude or dirpath.startswith(exclude + os.sep)):
            dirnames[:] = []
            continue
        for name in dirnames + filenames:
            path = os.path.join(dirpath, name)
            if exclude and path == exclude:
                continue
            st = os.lstat(path)
            seen[os.path.relpath(path, top)] = (st.st_mode, st.st_size, st.st_mtime_ns)
    return seen


class World(object):
    """One person: a bare origin and any number of Macs."""

    def __init__(self, readme=False):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="kefsync-"))
        self.origin = os.path.join(self.tmp, "origin.git")
        git("init", "-q", "--bare", "-b", "main", self.origin)
        if readme:
            seed = os.path.join(self.tmp, "seed")
            git("clone", "-q", self.origin, seed)
            with open(os.path.join(seed, "README.md"), "w") as handle:
                handle.write("backup\n")
            git("add", "README.md", cwd=seed)
            git("commit", "-q", "-m", "readme", cwd=seed)
            git("push", "-q", "origin", "HEAD:refs/heads/main", cwd=seed)
            shutil.rmtree(seed)

    def cleanup(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def mac(self, machine=MACHINE, name="mac1", identity=True):
        return Mac(self, machine, name, identity)

    def head(self):
        out = git("--git-dir", self.origin, "rev-parse", "-q", "--verify", "refs/heads/main", check=False)
        return out.strip() or None

    def files(self, prefix=""):
        if not self.head():
            return []
        out = git("--git-dir", self.origin, "ls-tree", "-r", "--name-only", "-z", "main")
        return sorted(p for p in out.split("\0") if p and p.startswith(prefix))

    def show(self, path, rev="main"):
        proc = subprocess.run(["git", "--git-dir", self.origin, "cat-file", "blob", "%s:%s" % (rev, path)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=clean_env(self.tmp))
        return proc.stdout if proc.returncode == 0 else None

    def mode(self, path):
        out = git("--git-dir", self.origin, "ls-tree", "main", "--", path)
        return out.split(" ")[0] if out else None

    def message(self, rev="main"):
        return git("--git-dir", self.origin, "log", "-1", "--format=%B", rev)

    def count(self):
        if not self.head():
            return 0
        return int(git("--git-dir", self.origin, "rev-list", "--count", "main").strip())

    def blocked(self, machine=MACHINE):
        raw = self.show(machine + "/BLOCKED")
        return None if raw is None else json.loads(raw.decode("utf-8"))


class Mac(object):
    def __init__(self, world, machine, name, identity):
        self.world = world
        self.machine = machine
        self.home = os.path.join(world.tmp, name, "home")
        os.makedirs(os.path.join(self.home, ".claude"))
        self.clone = os.path.join(self.home, ".kef-skills")
        git("clone", "-q", world.origin, self.clone)
        with open(os.path.join(self.clone, ".git", "kef-machine-id"), "w") as handle:
            handle.write(machine + "\n")
        if identity:
            git("config", "user.name", "test skill backup", cwd=self.clone)
            git("config", "user.email", "kef-skill-backup@users.noreply.github.com", cwd=self.clone)

    @classmethod
    def adopt(cls, world, home, machine):
        """A Mac whose clone already exists (for example copied from another Mac)."""
        mac = cls.__new__(cls)
        mac.world, mac.machine, mac.home = world, machine, home
        mac.clone = os.path.join(home, ".kef-skills")
        return mac

    def path(self, rel):
        return os.path.join(self.home, ".claude", rel)

    def write(self, rel, content="x\n", mode=None):
        path = self.path(rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data = content.encode("utf-8") if isinstance(content, str) else content
        with open(path, "wb") as handle:
            handle.write(data)
        if mode is not None:
            os.chmod(path, mode)
        return path

    def remove(self, rel):
        path = self.path(rel)
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        else:
            os.unlink(path)

    def sync(self, now=NOW, extra=()):
        cmd = [sys.executable, SCRIPT, "--repo", self.clone, "--home", self.home]
        if now:
            cmd += ["--now", now]
        cmd += list(extra)
        return subprocess.run(cmd, env=clean_env(self.home), stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=300)

    def git(self, *args, **kw):
        kw.setdefault("cwd", self.clone)
        return git(*args, **kw)

    def head(self):
        return self.git("rev-parse", "-q", "--verify", "refs/heads/main", check=False).strip() or None

    def log(self):
        try:
            with open(os.path.join(self.clone, ".git", "kef-sync.log")) as handle:
                return handle.read().splitlines()
        except OSError:
            return []
