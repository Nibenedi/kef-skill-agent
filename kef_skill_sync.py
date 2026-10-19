#!/usr/bin/env python3
"""KEF skill backup agent.

Copies ~/.claude/skills, ~/.claude/commands and ~/.claude/agents into this
computer's folder of a private backup repository, commits, and pushes.
Runs hourly from launchd. Python 3.9 standard library only.

    /usr/bin/python3 kef_skill_sync.py --repo ~/.kef-skills [--verbose] [--wait SECONDS]

Test overrides: --home DIR (instead of ~), --now ISO-8601 (instead of the clock).

It writes only inside <clone>/.git/ (the lock, the log, the machine id, its
state and the commits); the clone's files are left as setup checked them out.
Every run exits 0; the next hour retries.
"""

import argparse
import bisect
import datetime
import errno
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import unicodedata

KINDS = ("skills", "commands", "agents")
MAX_FILE = 20 * 1024 * 1024
LINK_CAP = 25 * 1024 * 1024
HOLD_DAYS = 14
HOLD = datetime.timedelta(days=HOLD_DAYS)
DELETE_LIMIT = 10
STALE_LOCK_SECONDS = 120
LOG_CAP = 1024 * 1024
LIST_CAP = 50
STRAND_KEEP = 10
SECRET_HITS_PER_FILE = 20
LOCAL_TIMEOUT = 600
FETCH_TIMEOUT = 900
PUSH_TIMEOUT = 2700
BRANCH = "refs/heads/main"
TRACKING = "refs/remotes/origin/main"
ZERO = "0" * 40
GONE = (errno.ENOENT, errno.ENOTDIR)
FALLBACK_NAME = "kef skill backup"
FALLBACK_EMAIL = "kef-skill-backup@users.noreply.github.com"
JUNK_NAMES = ("__pycache__", "node_modules", ".venv", "venv")
MACHINE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
GIT_CONFIG = [
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.fsmonitor=false",
    "-c", "commit.gpgSign=false",
    "-c", "gc.autoDetach=false",
    "-c", "maintenance.autoDetach=false",
    "-c", "advice.detachedHead=false",
]
LOCKS_TO_CLEAR = (
    "index.lock", "kef-sync.index.lock", "HEAD.lock", "packed-refs.lock",
    "refs/heads/main.lock", "refs/remotes/origin/main.lock",
)

# Secret patterns: (kind, regex). The named group "b" is the variable part,
# used by the placeholder filter. Patterns are specific on purpose.
SECRET_PATTERNS = [
    ("aws_access_key_id",
     r"(?<![A-Z0-9])(?:AKIA|ASIA)(?P<b>[A-Z0-9]{16})(?![A-Z0-9])"),
    ("github_token",
     r"(?<![A-Za-z0-9_])gh[pousr]_(?P<b>[A-Za-z0-9]{36,255})(?![A-Za-z0-9])"),
    ("github_fine_grained_token",
     r"(?<![A-Za-z0-9_])github_pat_(?P<b>[A-Za-z0-9_]{82,255})"),
    ("slack_token",
     r"(?<![A-Za-z0-9])xox[abposr]-(?P<b>(?:[0-9]{1,20}-){1,4}[A-Za-z0-9]{8,255})"),
    ("slack_webhook_url",
     r"https://hooks\.slack\.com/services/(?P<b>T[A-Z0-9]{6,12}/B[A-Z0-9]{6,12}/[A-Za-z0-9]{20,64})"),
    ("anthropic_api_key",
     r"(?<![A-Za-z0-9_-])sk-ant-(?P<b>[A-Za-z0-9_-]{20,255})"),
    ("openai_api_key",
     r"(?<![A-Za-z0-9_-])sk-(?:proj|svcacct|admin)-(?P<b>[A-Za-z0-9_-]{40,255})"),
    ("openai_api_key",
     r"(?<![A-Za-z0-9_-])sk-(?P<b>[A-Za-z0-9]{40,255})(?![A-Za-z0-9_-])"),
    ("google_api_key",
     r"(?<![A-Za-z0-9_-])AIza(?P<b>[0-9A-Za-z_-]{35})(?![0-9A-Za-z_-])"),
    ("stripe_live_key",
     r"(?<![A-Za-z0-9_])(?:sk|rk)_live_(?P<b>[0-9A-Za-z]{24,255})"),
    # PEM or PGP armor: only the BEGIN marker here; scan_secrets then needs an
    # -----END marker within 64 KB and a 40+ character base64 run between them,
    # in any layout (raw or escaped line breaks as in JSON or .env, one line,
    # armor headers of any length).
    ("private_key_block",
     r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----(?P<b>)"),
    ("google_oauth_refresh_token",
     r"(?<![A-Za-z0-9/_-])1//0(?P<b>[0-9A-Za-z_-]{30,})"),
    ("slack_app_token",
     r"(?<![A-Za-z0-9])xapp-(?P<b>[0-9]+-[A-Z0-9]{8,}-[0-9]{6,}-[A-Za-z0-9]{20,})"),
    ("google_oauth_client_secret",
     r"(?<![A-Za-z0-9_-])GOCSPX-(?P<b>[A-Za-z0-9_-]{24,40})(?![A-Za-z0-9_-])"),
    ("notion_token",
     r"(?<![A-Za-z0-9_])ntn_(?P<b>[A-Za-z0-9]{40,60})(?![A-Za-z0-9])"),
    ("notion_token",
     r"(?<![A-Za-z0-9_])secret_(?P<b>[A-Za-z0-9]{43})(?![A-Za-z0-9_])"),
    ("gitlab_token",
     r"(?<![A-Za-z0-9_])glpat-(?P<b>[A-Za-z0-9_-]{20,})"),
    ("npm_token",
     r"(?<![A-Za-z0-9_])npm_(?P<b>[A-Za-z0-9]{36})(?![A-Za-z0-9])"),
    ("sendgrid_api_key",
     r"(?<![A-Za-z0-9_.-])SG\.(?P<b>[A-Za-z0-9_-]{22}\.[A-Za-z0-9_-]{43})(?![A-Za-z0-9_-])"),
    ("hubspot_private_app_token",
     r"(?<![A-Za-z0-9_-])pat-(?:na1|eu1)-(?P<b>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?![0-9a-f])"),
    ("airtable_token",
     r"(?<![A-Za-z0-9_])pat(?P<b>[A-Za-z0-9]{14}\.[a-f0-9]{64})(?![A-Za-z0-9])"),
    ("linear_api_key",
     r"(?<![A-Za-z0-9_])lin_api_(?P<b>[A-Za-z0-9]{40})(?![A-Za-z0-9])"),
    ("aws_secret_access_key",
     r"(?i:aws_?secret_?access_?key)[\"']?\s*[:=]\s*[\"']?(?P<b>[A-Za-z0-9/+]{40})(?![A-Za-z0-9/+=])"),
    # postgres://user:password@host and the like, when the password is real.
    ("database_url_password",
     r"(?<![A-Za-z0-9+.-])(?:postgres(?:ql)?|mysql|mariadb)(?:\+[a-z0-9]+)?://"
     r"(?P<u>[^:/@\s\"'<>]+):(?P<b>[^@/\s\"'<>]+)@(?:\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9._-]+)"),
]
# Bodies that must mix upper case, lower case and digits to count (random tokens do).
MIXED_REQUIRED = ("sk-", "secret_", "npm_", "lin_api_")
DB_PLACEHOLDERS = ("password", "pass", "passwd", "pwd", "secret", "changeme", "root", "postgres",
                   "mysql", "admin", "test", "user", "username", "mypassword", "yourpassword",
                   "dbpass", "db_password", "pw", "mysecretpassword", "password123", "secret123",
                   "admin123", "changeit")
DB_LOCAL_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "::1", "host.docker.internal")
SECRET_RES = [(kind, re.compile(rx)) for kind, rx in SECRET_PATTERNS]
KEY_BODY_RE = re.compile(r"[A-Za-z0-9+/=]{40,}")
KEY_WINDOW = 65536
PLACEHOLDER_WORDS = ("example", "xxxx", "your-", "your_", "<", ">", "...",
                     "placeholder", "redacted", "dummy", "sample")
REPEAT_RE = re.compile(r"(.)\1{7,}")


# ---------------------------------------------------------------- helpers

def now_from(arg):
    if arg:
        parsed = parse_iso(arg)
        if parsed is None:
            raise SystemExit("--now must be an ISO-8601 time, e.g. 2026-10-19T05:36:56-04:00")
        return parsed
    return datetime.datetime.now().astimezone()


def parse_iso(text):
    if not isinstance(text, str):
        return None
    text = text.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        value = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.astimezone()
    return value


def iso(value):
    return value.astimezone().isoformat(timespec="seconds")


def nfc(name):
    return unicodedata.normalize("NFC", name)


def inside(path, root):
    return path == root or path.startswith(root.rstrip("/") + "/")


def blob_sha(data):
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def capped(items, cap=LIST_CAP):
    items = list(items)
    if len(items) <= cap:
        return items
    return items[:cap] + ["... and %d more" % (len(items) - cap)]


def looks_placeholder(kind, match, body):
    """Only the variable part (body) is judged, never headers around it."""
    core = re.sub(r"[^A-Za-z0-9]", "", body)
    if len(set(core)) <= 4:
        return True
    if kind == "private_key_block":
        # A real key body is long random base64: judge words case-sensitively.
        return any(w in body for w in ("XXXX", "xxxx", "EXAMPLE", "example", "Example"))
    low = body.lower()
    for word in PLACEHOLDER_WORDS:
        if word in low:
            return True
    if REPEAT_RE.search(core):
        return True
    if match.startswith(MIXED_REQUIRED) and not match.startswith(("sk-proj-", "sk-svcacct-", "sk-admin-", "sk-ant-")):
        # These bodies are random: require upper case, lower case and digits.
        if not (re.search(r"[A-Z]", core) and re.search(r"[a-z]", core) and re.search(r"[0-9]", core)):
            return True
    if kind == "database_url_password":
        user = re.search(r"://([^:/@]*):", match)
        host = match.rsplit("@", 1)[-1].strip("[]").lower()
        if (low in DB_PLACEHOLDERS or len(body) < 6 or body[:1] in "${<%*[("
                or "${" in body or "{{" in body or "*" in body
                or (user is not None and user.group(1).lower() == low)
                or host in DB_LOCAL_HOSTS or "example" in host.split(".")):
            return True
    return False


def scan_secrets(data):
    """Return [(line, kind)] for likely secrets in data (bytes). Never the text."""
    texts = [data.decode("latin-1")]
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):  # UTF-16 with a byte-order mark
        texts.append(data[:len(data) // 2 * 2].decode("utf-16", "replace"))
    hits = set()
    for text in texts:
        for kind, rx in SECRET_RES:
            ends = None
            covered = -1
            for m in rx.finditer(text):
                body = m.group("b")
                if kind == "private_key_block":
                    # Linear on any input: END positions are found once, and a
                    # BEGIN inside a stretch already examined is not examined again.
                    if m.start() < covered:
                        continue
                    if ends is None:
                        ends = [e.start() for e in re.finditer("-----END", text)]
                    i = bisect.bisect_left(ends, m.end())
                    if i == len(ends) or ends[i] - m.end() > KEY_WINDOW:
                        continue
                    covered = ends[i]
                    found = KEY_BODY_RE.search(text, m.end(), ends[i])
                    if not found:
                        continue
                    body = found.group(0)
                if looks_placeholder(kind, m.group(0), body):
                    continue
                hits.add((text.count("\n", 0, m.start()) + 1, kind))
    return sorted(hits)[:SECRET_HITS_PER_FILE]


def is_junk(name):
    if name.lower() == ".git" or name == ".DS_Store" or name.endswith(".pyc"):
        return True
    return name in JUNK_NAMES


# ---------------------------------------------------------------- git

class GitResult(object):
    def __init__(self, code, out, err):
        self.code = code
        self.out = out
        self.err = err

    @property
    def ok(self):
        return self.code == 0

    def text(self):
        return self.out.decode("utf-8", "surrogateescape")

    def first_error(self):
        for line in (self.err + b"\n" + self.out).decode("utf-8", "replace").splitlines():
            line = line.strip()
            if line:
                return line[:200]
        return "exit %d" % self.code


class Git(object):
    def __init__(self, repo, verbose):
        self.repo = repo
        self.verbose = verbose
        env = dict(os.environ)
        for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE", "GIT_PREFIX",
                    "GIT_CEILING_DIRECTORIES", "GIT_COMMON_DIR"):
            env.pop(key, None)
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_ASKPASS"] = ""  # empty: never run a GUI askpass, never prompt
        env["GCM_INTERACTIVE"] = "never"
        env["LC_ALL"] = "C"
        self.env = env
        self.config = list(GIT_CONFIG)
        if not self.run("config", "user.name").text().strip():
            self.config += ["-c", "user.name=" + FALLBACK_NAME]
        if not self.run("config", "user.email").text().strip():
            self.config += ["-c", "user.email=" + FALLBACK_EMAIL]

    def run(self, *args, **kw):
        stdin = kw.get("stdin")
        if isinstance(stdin, str):
            stdin = stdin.encode("utf-8", "surrogateescape")
        env = self.env
        if kw.get("env"):
            env = dict(self.env)
            env.update(kw["env"])
        cmd = ["git"] + getattr(self, "config", list(GIT_CONFIG)) + list(args)
        try:
            proc = subprocess.run(cmd, cwd=self.repo, env=env, input=stdin,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  stdin=None if stdin is not None else subprocess.DEVNULL,
                                  timeout=kw.get("timeout", LOCAL_TIMEOUT))
            result = GitResult(proc.returncode, proc.stdout, proc.stderr)
        except subprocess.TimeoutExpired:
            result = GitResult(124, b"", b"timed out: git " + " ".join(args[:2]).encode())
        except OSError as exc:
            result = GitResult(127, b"", str(exc).encode())
        if self.verbose:
            sys.stderr.write("git %s -> %d\n" % (" ".join(args[:4]), result.code))
        return result

    def rev(self, name):
        r = self.run("rev-parse", "-q", "--verify", name + "^{commit}")
        return r.text().strip() if r.ok else None

    def subtree(self, commit, path):
        """(ok, sha or None) for a top-level entry; ok is False when git failed."""
        if not commit:
            return True, None
        r = self.run("ls-tree", "-z", commit, "--", path)
        if not r.ok:
            return False, None
        for rec in r.text().split("\0"):
            if rec and rec.split("\t", 1)[1] == path:
                return True, rec.split("\t", 1)[0].split(" ")[2]
        return True, None

    def blob(self, commit, path):
        if not commit:
            return None
        r = self.run("cat-file", "blob", "%s:%s" % (commit, path))
        return r.out if r.ok else None

    def is_ancestor(self, a, b):
        return self.run("merge-base", "--is-ancestor", a, b).ok

    def ls_tree(self, commit, prefix):
        files = {}
        if not commit:
            return files
        r = self.run("ls-tree", "-r", "-z", "--full-tree", commit, "--", prefix)
        if not r.ok:
            raise RuntimeError("ls-tree failed: " + r.first_error())
        for rec in r.text().split("\0"):
            if not rec:
                continue
            meta, path = rec.split("\t", 1)
            mode, kind, sha = meta.split(" ")
            if kind == "blob" and path.startswith(prefix + "/"):
                files[path] = (mode, sha)
        return files

    def write_blob(self, data):
        r = self.run("hash-object", "-w", "--no-filters", "--stdin", stdin=data)
        if not r.ok:
            raise RuntimeError("hash-object failed: " + r.first_error())
        return r.text().strip()


# ---------------------------------------------------------------- source scan

class Source(object):
    """Walks ~/.claude/<kind> and lists what can be copied."""

    def __init__(self, home, clone_real):
        self.home = home
        self.clone_real = clone_real
        self.files = {}          # rel ("skills/x/SKILL.md") -> (path, executable)
        self.oversized = []      # rel
        self.unreadable = []     # rel (file or folder) that could not be read
        self.kept = {}           # rel -> why its last backed-up copy stays as is
        self.skipped = []        # "rel (reason)"
        self.found = dict((k, 0) for k in KINDS)
        self.present = dict((k, False) for k in KINDS)
        self.followed = set()    # realpaths of link targets already copied in this scan
        self.claimed = {}        # realpath -> rel of each top-level entry of a kind folder

    def scan(self):
        # Top-level skills and agents are copied under their own names; a link
        # deeper down that points at one of them is not copied a second time.
        for kind in KINDS:
            root = os.path.join(self.home, ".claude", kind)
            try:
                with os.scandir(root) as it:
                    entries = sorted(it, key=lambda e: (e.is_symlink(), e.name))  # real folders first
                for entry in entries:
                    if not is_junk(entry.name):
                        real = os.path.realpath(entry.path)
                        self.claimed.setdefault(real, kind + "/" + nfc(entry.name))
            except OSError:
                pass
        for kind in KINDS:
            root = os.path.join(self.home, ".claude", kind)
            try:
                st = os.stat(root)
            except OSError as exc:
                if exc.errno not in GONE:
                    self._unreadable(kind, kind)
                continue
            if not stat.S_ISDIR(st.st_mode):
                continue
            real = os.path.realpath(root)
            if inside(real, self.clone_real):
                # Never treated as empty: the last backed-up copy stays.
                self.skipped.append("%s (points into the backup clone, last copy kept)" % kind)
                self.kept[kind] = "inside_backup_clone"
                self._count(kind)
                continue
            self.present[kind] = True
            self._walk(root, kind, kind, frozenset([real]))
        return self

    def _count(self, kind):
        self.found[kind] += 1

    def _unreadable(self, rel, kind):
        self.unreadable.append(rel)
        self.kept[rel] = "unreadable"
        self._count(kind)

    def _walk(self, path, rel, kind, stack):
        try:
            with os.scandir(path) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError as exc:
            if exc.errno not in GONE:  # gone mid-run: the deletion path handles it
                self._unreadable(rel, kind)
            return
        for entry in entries:
            if is_junk(entry.name):
                continue
            crel = rel + "/" + nfc(entry.name)
            try:
                if entry.is_symlink():
                    self._link(entry.path, crel, kind, stack)
                elif entry.is_dir(follow_symlinks=False):
                    real = os.path.realpath(entry.path)
                    self._walk(entry.path, crel, kind, stack | frozenset([real]))
                elif entry.is_file(follow_symlinks=False):
                    self._file(entry.path, crel, kind, entry.stat(follow_symlinks=False))
                else:
                    self.skipped.append("%s (not a regular file)" % crel)
            except OSError as exc:
                if exc.errno not in GONE:  # gone mid-run (an editor's temp file)
                    self._unreadable(crel, kind)

    def _file(self, path, rel, kind, st):
        self._count(kind)
        if st.st_size > MAX_FILE:
            self.oversized.append(rel)
            return
        self.files[rel] = (path, bool(st.st_mode & 0o111))

    def _link(self, path, rel, kind, stack):
        # A target that is gone is skipped (its backup may be deleted, under the
        # deletion guard). A target that exists but cannot be read now, or has
        # grown past the cap, keeps its last backed-up copy.
        target = os.path.realpath(path)
        try:
            st = os.stat(path)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                self.skipped.append("%s (link loop)" % rel)
            elif exc.errno in GONE:
                self.skipped.append("%s (link target missing)" % rel)
            else:
                self._unreadable(rel, kind)
            return
        if inside(target, self.clone_real):
            self.skipped.append("%s (link into the backup clone)" % rel)
            self.kept[rel] = "inside_backup_clone"
            self._count(kind)
            return
        if stat.S_ISDIR(st.st_mode):
            if target in stack or any(inside(s, target) for s in stack):
                self.skipped.append("%s (link loop)" % rel)
                return
            if target in self.followed or self.claimed.get(target, rel) != rel:
                # Each target is copied once per run, so skills that link to each
                # other cannot multiply.
                self.skipped.append("%s (link to a skill copied under another name)" % rel)
                return
            try:
                with open(os.path.join(target, "SKILL.md"), "rb"):
                    pass
            except OSError as exc:
                if exc.errno in GONE or exc.errno == errno.EISDIR:
                    self.skipped.append("%s (link to a folder that is not a skill)" % rel)
                else:
                    self._unreadable(rel, kind)
                return
            if tree_size(target, LINK_CAP) > LINK_CAP:
                self.skipped.append("%s (linked skill over 25 MB, last copy kept)" % rel)
                self.kept[rel] = "over_size_cap"
                self._count(kind)
                return
            self.followed.add(target)
            self._walk(path, rel, kind, stack | frozenset([target]))
        elif stat.S_ISREG(st.st_mode):
            if kind == "agents" and target.lower().endswith(".md"):
                if st.st_size > LINK_CAP:
                    self.skipped.append("%s (linked file over 25 MB, last copy kept)" % rel)
                    self.kept[rel] = "over_size_cap"
                    self._count(kind)
                    return
                self._file(path, rel, kind, st)
            else:
                self.skipped.append("%s (link to a file)" % rel)
        else:
            self.skipped.append("%s (link to something that is not a file or folder)" % rel)


def tree_size(top, cap):
    total = 0
    stack = [top]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                entries = list(it)
        except OSError:
            continue
        for entry in entries:
            if is_junk(entry.name):
                continue
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append(entry.path)
                elif entry.is_file(follow_symlinks=False):
                    total += entry.stat(follow_symlinks=False).st_size
            except OSError:
                continue
            if total > cap:
                return total
    return total


def read_capped(path):
    with open(path, "rb") as handle:
        data = handle.read(MAX_FILE + 1)
    return None if len(data) > MAX_FILE else data


# ---------------------------------------------------------------- the run

class Run(object):
    def __init__(self, repo, home, now, verbose):
        self.repo = repo
        self.gitdir = os.path.join(repo, ".git")
        self.home = home
        self.now = now
        self.verbose = verbose
        self.clean_blobs = set()  # blobs this run scanned or wrote itself

    def say(self, text):
        if self.verbose:
            sys.stderr.write(text + "\n")

    # -- step 2: recover from an interrupted run
    def recover(self, g):
        for name in LOCKS_TO_CLEAR:
            path = os.path.join(self.gitdir, name)
            try:
                age = time.time() - os.lstat(path).st_mtime
            except OSError:
                continue
            if age > STALE_LOCK_SECONDS:
                try:
                    os.unlink(path)
                    self.say("removed stale %s" % name)
                except OSError:
                    pass
        # An operation someone left half done in the clone is ended without
        # touching the clone's files (they may be a person's live, unsaved
        # edits): its state is dropped, HEAD goes back to main, and only the
        # index is reset. main itself is where the operation found it.
        gd = self.gitdir
        if os.path.exists(os.path.join(gd, "rebase-apply", "applying")):
            g.run("am", "--quit")
        if os.path.isdir(os.path.join(gd, "rebase-merge")) or os.path.isdir(os.path.join(gd, "rebase-apply")):
            g.run("rebase", "--quit")
        if os.path.exists(os.path.join(gd, "MERGE_HEAD")):
            g.run("merge", "--quit")
        if os.path.exists(os.path.join(gd, "CHERRY_PICK_HEAD")):
            g.run("cherry-pick", "--quit")
        if os.path.exists(os.path.join(gd, "REVERT_HEAD")):
            g.run("revert", "--quit")
        for name in ("rebase-merge", "rebase-apply", "sequencer"):
            if os.path.isdir(os.path.join(gd, name)):
                shutil.rmtree(os.path.join(gd, name), ignore_errors=True)
        for name in ("MERGE_HEAD", "MERGE_MSG", "MERGE_MODE", "CHERRY_PICK_HEAD", "REVERT_HEAD"):
            try:
                os.unlink(os.path.join(gd, name))
            except OSError:
                pass
        head = g.run("symbolic-ref", "-q", "HEAD")
        if not (head.ok and head.text().strip() == BRANCH):
            g.run("symbolic-ref", "HEAD", BRANCH)
        old = g.rev(BRANCH)
        if old and not g.run("diff-index", "--cached", "--quiet", old, "--").ok:
            g.run("read-tree", "--reset", old)  # the index only; files are left alone

    # -- steps 3 to 7: build this run's commit
    def build(self, g, machine, old):
        head_all = g.ls_tree(old, machine)
        head = dict((p, v) for p, v in head_all.items() if p.split("/")[1:2] and p.split("/")[1] in KINDS)
        src = Source(self.home, os.path.realpath(self.repo)).scan()
        pre = machine + "/"
        state_path = os.path.join(self.gitdir, "kef-sync-state.json")
        state = load_json_file(state_path)
        refused = state.get("server_refused") if isinstance(state.get("server_refused"), dict) else {}

        desired = {}      # repo path -> (mode, sha); entries to write
        added, changed = [], []
        secret_hits = {}
        oversized = list(src.oversized)
        for rel in sorted(src.files):
            path, executable = src.files[rel]
            rp = pre + rel
            try:
                data = read_capped(path)
            except OSError as exc:
                if exc.errno in GONE:
                    # Gone since the scan: an ordinary deletion, and no longer
                    # counted, so a folder emptied mid-run trips the guard as usual.
                    del src.files[rel]
                    src.found[rel.split("/", 1)[0]] -= 1
                    continue
                src.unreadable.append(rel)
                src.kept[rel] = "unreadable"
                continue
            if data is None:
                oversized.append(rel)
                continue
            entry = ("100755" if executable else "100644", blob_sha(data))
            old_entry = head.get(rp)
            if old_entry == entry:
                continue
            rec = refused.get(rp)
            if isinstance(rec, dict) and rec.get("sha") == entry[1]:
                # The server refused exactly this content: leave it out like a secret.
                line = rec.get("line") if isinstance(rec.get("line"), int) else 1
                secret_hits[rp] = [(line, "server_refused")]
                continue
            if old_entry is None or old_entry[1] != entry[1]:
                hits = scan_secrets(data)
                if hits:
                    secret_hits[rp] = hits
                    continue
                if g.write_blob(data) != entry[1]:
                    raise RuntimeError("object hash mismatch for " + rp)
                self.clean_blobs.add(entry[1])
            desired[rp] = entry
            (added if old_entry is None else changed).append(rp)

        # Files in the backup with no copyable source. Unreadable and oversized
        # sources keep their last backed-up copy.
        present = set(pre + r for r in src.files) | set(secret_hits)
        stale = dict((pre + r, why) for r, why in src.kept.items())
        stale.update((pre + r, "over_size_cap") for r in oversized)
        keep_exact = set(stale)
        keep_prefix = [pre + r + "/" for r in src.kept]
        deletions = []
        for rp in sorted(head):
            if rp in present or rp in keep_exact or any(rp.startswith(k) for k in keep_prefix):
                continue
            deletions.append(rp)

        # Step 4: deletion guard.
        kinds_committed = set(p.split("/")[1] for p in head)
        empty = [k for k in KINDS if k in kinds_committed and src.found[k] == 0]
        reasons = []
        if empty:
            names = " and ".join(empty)
            reasons.append("The %s folder%s on this computer %s empty or missing." % (
                names, "s" if len(empty) > 1 else "", "are" if len(empty) > 1 else "is"))
        if len(deletions) > DELETE_LIMIT:
            reasons.append("%d files would be deleted in one run (the limit is %d)." % (len(deletions), DELETE_LIMIT))

        prev = self.load_blocked(g, old, machine)
        state_known = isinstance(state.get("held_since"), dict)
        known_since = state.get("held_since") if state_known else {}
        prev_hold = prev.get("held_deletions") if isinstance(prev.get("held_deletions"), dict) else {}
        prev_hold_paths = set(p for p in (prev_hold.get("paths") or []) if isinstance(p, str))
        prev_hold_count = prev_hold.get("count") if isinstance(prev_hold.get("count"), int) else None
        prev_hold_partial = bool(prev_hold) and (prev_hold_count is None or prev_hold_count > len(prev_hold_paths))
        hold = None
        released = []
        withheld = []
        held_since = {}
        applied = []
        for d in deletions:
            # A path stays held until its own 14 days are up or it comes back,
            # whether or not this run trips the guard again.
            since = parse_iso(known_since.get(d))
            if since is None and not state_known and (d in prev_hold_paths or prev_hold_partial):
                since = self.now  # held before, own start unknown: restart (the safe side)
            if since is None:
                if not reasons:
                    applied.append(d)  # an ordinary deletion
                    continue
                since = self.now
            if self.now >= since + HOLD:
                applied.append(d)
                released.append(d)
            else:
                held_since[d] = since
        if held_since:
            # A new file or folder on the path of a held deletion waits with it.
            held_dirs = set()
            for d in held_since:
                parts = d.split("/")
                held_dirs.update("/".join(parts[:i]) for i in range(1, len(parts)))
            for n in sorted(desired):
                parts = n.split("/")
                if n in held_dirs or any("/".join(parts[:i]) in held_since for i in range(1, len(parts))):
                    withheld.append(n)
                    del desired[n]
                    if n in added:
                        added.remove(n)
                    if n in changed:
                        changed.remove(n)
            first = min(held_since.values())
            why = " ".join(reasons) or "Deletions held earlier are still waiting out their 14 days."
            hold = {
                "reason": why + " Each held deletion applies 14 days after it was first held, unless the files come back.",
                "count": len(held_since),
                "paths": sorted(held_since)[:LIST_CAP],
                "since": iso(first),
                "release_after": iso(first + HOLD),
            }

        # Step 5 bookkeeping: when each secret file was first held.
        first_held = state.get("secret_since") if isinstance(state.get("secret_since"), dict) else {}
        prev_secret_paths = set()
        if isinstance(prev.get("secrets"), list):
            prev_secret_paths = set(s.get("path") for s in prev["secrets"] if isinstance(s, dict))
        new_first = {}
        for rp in sorted(set(secret_hits) | set(stale)):
            when = parse_iso(first_held.get(rp))
            if when is None and rp in prev_secret_paths:
                when = parse_iso(prev.get("since"))
            new_first[rp] = iso(when or self.now)
        new_state = dict(state)
        new_state["secret_since"] = new_first
        new_state["held_since"] = dict((d, iso(t)) for d, t in held_since.items())
        new_state["server_refused"] = dict((rp, rec) for rp, rec in refused.items()
                                           if secret_hits.get(rp, [(0, "")])[0][1] == "server_refused")
        if new_state != state:
            write_json_file(state_path, new_state)

        entries = []
        for rp in applied:
            entries.append("0 %s\t%s" % (ZERO, rp))

        # BLOCKED
        blocked_path = pre + "BLOCKED"
        # Sources that cannot be copied now (unreadable, inside the clone, over a
        # size cap) are listed too, with line 0: their backup is going stale.
        listed = dict((rp, list(hits)) for rp, hits in secret_hits.items())
        for rp, why in stale.items():
            listed.setdefault(rp, []).append((0, why))
        secrets = [{"path": rp, "line": line, "kind": kind}
                   for rp in sorted(listed) for (line, kind) in sorted(listed[rp])]
        if hold or secrets:
            starts = [parse_iso(v) for v in new_first.values()]
            if hold:
                starts.append(parse_iso(hold["since"]))
            body = {"since": iso(min(starts)), "held_deletions": hold, "secrets": secrets}
            same = all(prev.get(k) == body[k] for k in body) and "updated" in prev
            if not same:
                body["updated"] = iso(self.now)
                text = json.dumps(body, indent=2, ensure_ascii=False) + "\n"
                desired[blocked_path] = ("100644", g.write_blob(text.encode("utf-8")))
                self.clean_blobs.add(desired[blocked_path][1])
        elif head_all.get(blocked_path):
            entries.append("0 %s\t%s" % (ZERO, blocked_path))

        # Step 7: LAST_SEEN once per local day.
        last_seen_path = pre + "LAST_SEEN"
        seen = g.blob(old, last_seen_path)
        seen_at = parse_iso(seen.decode("utf-8", "replace")) if seen else None
        if seen_at is None or seen_at.astimezone().date() != self.now.astimezone().date():
            desired[last_seen_path] = ("100644", g.write_blob((iso(self.now) + "\n").encode("utf-8")))
            self.clean_blobs.add(desired[last_seen_path][1])

        for rp in sorted(desired):
            mode, sha = desired[rp]
            entries.append("%s %s\t%s" % (mode, sha, rp))

        summary = {
            "added": added, "changed": changed, "deleted": list(applied),
            "released": len(released), "hold": hold, "withheld": withheld,
            "secrets": sorted(secret_hits), "skipped": src.skipped,
            "oversized": [pre + r for r in oversized],
            "unreadable": [pre + r for r in src.unreadable],
            "stale": sorted(stale),
        }
        if not entries:
            return old, summary, "no changes"
        commit = self.commit(g, machine, old, entries, summary)
        return commit, summary, None

    def load_blocked(self, g, old, machine):
        raw = g.blob(old, machine + "/BLOCKED")
        if not raw:
            return {}
        try:
            value = json.loads(raw.decode("utf-8"))
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {}

    def commit(self, g, machine, old, entries, summary):
        index = os.path.join(self.gitdir, "kef-sync.index")
        env = {"GIT_INDEX_FILE": index}
        try:
            os.unlink(index)
        except OSError:
            pass
        r = g.run("read-tree", old, env=env) if old else g.run("read-tree", "--empty", env=env)
        if not r.ok:
            raise RuntimeError("read-tree failed: " + r.first_error())
        removals = [e for e in entries if e.startswith("0 ")]
        writes = [e for e in entries if not e.startswith("0 ")]
        for batch in (removals, writes):
            if not batch:
                continue
            r = g.run("update-index", "-z", "--index-info", stdin="\0".join(batch) + "\0", env=env)
            if not r.ok:
                # One path git refuses must not block the rest.
                for one in batch:
                    if not g.run("update-index", "-z", "--index-info", stdin=one + "\0", env=env).ok:
                        refused = one.split("\t", 1)[1][len(machine) + 1:]
                        summary["skipped"].append("%s (git refused the path)" % refused)
        r = g.run("write-tree", env=env)
        try:
            os.unlink(index)
        except OSError:
            pass
        if not r.ok:
            raise RuntimeError("write-tree failed: " + r.first_error())
        tree = r.text().strip()
        if old and tree == g.run("rev-parse", old + "^{tree}").text().strip():
            return old
        args = ["commit-tree", "--no-gpg-sign", tree]
        if old:
            args += ["-p", old]
        r = g.run(*args, stdin=self.message(machine, summary))
        if not r.ok:
            raise RuntimeError("commit-tree failed: " + r.first_error())
        commit = r.text().strip()
        r = self.move_main(g, commit, old)
        if not r.ok:
            raise RuntimeError("update-ref failed: " + r.first_error())
        return commit

    def message(self, machine, s):
        a, c, d = len(s["added"]), len(s["changed"]), len(s["deleted"])
        if a or c or d:
            head = "%s: %d added, %d changed, %d deleted" % (machine, a, c, d)
        else:
            head = "%s: check-in" % machine
        lines = [head, ""]
        if s["hold"]:
            lines.append("Held deletions: %d (%s)" % (s["hold"]["count"], s["hold"]["reason"]))
        if s["released"]:
            lines.append("Released held deletions: %d (each held 14 days or more)" % s["released"])
        if s["withheld"]:
            lines.append("Held back with the deletions (same path as a held deletion):")
            lines += ["  " + p for p in capped(s["withheld"])]
        if s["secrets"]:
            lines.append("Left out, possible secret (see BLOCKED):")
            lines += ["  " + p for p in capped(s["secrets"])]
        if s["oversized"]:
            lines.append("Skipped, over 20 MB:")
            lines += ["  " + p for p in capped(s["oversized"])]
        other = [machine + "/" + x for x in s["skipped"]] + [p + " (unreadable)" for p in s["unreadable"]]
        if other:
            lines.append("Skipped:")
            lines += ["  " + p for p in capped(other)]
        return "\n".join(lines).rstrip() + "\n"

    # -- step 6: fetch, rebase onto origin/main, push
    def publish(self, g, machine, tip):
        if tip is None:
            return "nothing to push"
        if tip == g.rev(TRACKING):
            return "up to date"
        note = ""
        stranded = None
        for _attempt in range(3):
            r = g.run("fetch", "--quiet", "--prune", "--no-tags", "origin",
                      "+refs/heads/*:refs/remotes/origin/*", timeout=FETCH_TIMEOUT)
            if not r.ok:
                return "offline or sign-in problem, will retry: " + r.first_error() + note
            base = g.rev(TRACKING)
            if base == tip:
                return "up to date" + note
            if base and g.is_ancestor(tip, base):
                self.move_main(g, base, tip)
                return "up to date (already on the server)" + note
            problem = self.outside_commits(g, machine, base, tip)
            new_tip = None
            if not problem and base and not g.is_ancestor(base, tip):
                new_tip, problem = self.replay(g, machine, base, tip)
            if not problem:
                problem = self.unscanned_secret(g, base, new_tip or tip)
            if problem:
                # Local history cannot go on top of the server's (the server copy of
                # this folder changed, history was rewritten, or a commit touches
                # other paths). Set it aside, start again from the server's tip and
                # rebuild this run's commit from the source files, which are the truth.
                if stranded:
                    return "not pushed: " + problem + note
                stranded = self.strand(g, tip, base)
                if not stranded:
                    return "not pushed: could not set local history aside: " + problem
                note = "; local history set aside as %s (%s)" % (stranded, problem)
                tip, self.summary, _ = self.build(g, machine, base)
                if tip is None or tip == base:
                    return "up to date" + note
            elif new_tip:
                r = self.move_main(g, new_tip, tip, "kef skill backup (rebase)")
                if not r.ok:
                    return "not pushed: update-ref failed: " + r.first_error() + note
                tip = new_tip
            r = g.run("push", "--quiet", "--porcelain", "--no-verify", "origin",
                      "%s:refs/heads/main" % BRANCH, timeout=PUSH_TIMEOUT)
            if r.ok:
                g.run("update-ref", TRACKING, tip)
                return "pushed %s%s" % (tip[:7], note)
            status = r.out.decode("utf-8", "replace")
            if "[remote rejected]" in status and refusal_is_transient(r):
                continue  # the server was busy updating main: go again
            if "[remote rejected]" in status:
                # The server refused this history (a ruleset, push protection, a
                # hook). Retrying the same commits would fail forever: files the
                # server names are left out from now on (listed in BLOCKED), the
                # local history is set aside, and one commit is rebuilt from the
                # source on the server's tip.
                reason = remote_reason(r)
                named = self.learn_refused(g, machine, tip, r)
                if stranded:
                    return "push refused by the server, will retry: " + reason + note
                if self.only_snapshots_since_refusal(g, machine, base, tip):
                    stranded = "(nothing new to keep)"
                    moved = self.move_main(g, base, tip, "kef skill backup (restart from server)")
                    if not moved.ok:
                        return "push refused by the server: " + reason
                else:
                    stranded = self.strand(g, tip, base)
                    if not stranded:
                        return "push refused by the server, could not set local history aside: " + reason
                note = "; local history set aside as %s (the server refused it: %s%s)" % (
                    stranded, reason, ", left out: " + ", ".join(named) if named else "")
                tip, self.summary, _ = self.build(g, machine, base)
                self.remember_state("refused_rebuild", tip)
                if tip is None or tip == base:
                    return "up to date" + note
                continue
            if "[rejected]" not in status:
                return "push failed, will retry: " + r.first_error() + note
            # [rejected] (fetch first / non-fast-forward): another computer pushed; go again.
        return "push rejected three times, will retry" + note

    def unscanned_secret(self, g, base, tip):
        """Scan every file about to be pushed that this run did not scan itself
        (a hand commit in the clone, a commit from an earlier version)."""
        args = ["rev-list", "--objects", tip] + (["--not", base] if base else [])
        r = g.run(*args)
        if not r.ok:
            return "rev-list failed: " + r.first_error()
        paths = {}
        for line in r.text().split("\n"):
            sha, _sep, path = line.partition(" ")
            if path and sha not in self.clean_blobs:
                paths[sha] = path
        # Commit messages too: a hand commit's message travels with it.
        log = g.run("log", "--format=%H %B%x00", tip, *(["--not", base] if base else []))
        if not log.ok:
            return "log failed: " + log.first_error()
        for rec in log.out.split(b"\x00"):
            rec = rec.strip()
            if rec and scan_secrets(rec.partition(b" ")[2]):
                return "a commit message holds a possible secret (commit %s)" % rec[:7].decode("ascii", "replace")
        if not paths:
            return None
        kinds = g.run("cat-file", "--batch-check", stdin="\n".join(paths) + "\n")
        if not kinds.ok:
            return "cat-file failed: " + kinds.first_error()
        for line in kinds.text().split("\n"):
            parts = line.split()
            if len(parts) < 2 or parts[1] != "blob":
                continue
            blob = g.run("cat-file", "blob", parts[0])
            if not blob.ok:
                return "cat-file failed: " + blob.first_error()
            hits = scan_secrets(blob.out)
            if hits:
                return "a commit this run did not make holds a possible secret in %s (%s)" % (
                    paths[parts[0]], hits[0][1])
            self.clean_blobs.add(parts[0])
        return None

    def learn_refused(self, g, machine, tip, result):
        """Record the files the server's refusal names ("path: <file>:<line>",
        as GitHub push protection prints them), so the rebuild leaves them out."""
        named = []
        state = load_json_file(os.path.join(self.gitdir, "kef-sync-state.json"))
        refused = state.get("server_refused") if isinstance(state.get("server_refused"), dict) else {}
        for line in result.err.decode("utf-8", "replace").splitlines():
            m = re.search(r"\bpath:\s*(.+?):(\d+)\s*$", line)
            if not m or not m.group(1).startswith(machine + "/"):
                continue
            sha = g.run("rev-parse", "-q", "--verify", "%s:%s" % (tip, m.group(1)))
            if sha.ok:
                refused[m.group(1)] = {"sha": sha.text().strip(), "line": int(m.group(2))}
                named.append(m.group(1))
        if named:
            self.remember_state("server_refused", refused)
        return named

    def remember_state(self, key, value):
        path = os.path.join(self.gitdir, "kef-sync-state.json")
        state = load_json_file(path)
        state[key] = value
        write_json_file(path, state)

    def only_snapshots_since_refusal(self, g, machine, base, tip):
        """True when the local history is just this agent's own commits on top
        of the commit it rebuilt after the previous refusal: nothing to keep."""
        state = load_json_file(os.path.join(self.gitdir, "kef-sync-state.json"))
        rebuilt = state.get("refused_rebuild")
        if not rebuilt or not base:
            return False
        r = g.run("rev-list", "--reverse", "--parents", tip, "--not", base)
        commits = [line.split() for line in r.text().split("\n") if line.strip()] if r.ok else []
        if not commits or commits[0][0] != rebuilt or commits[0][1:] != [base]:
            return False
        for parts in commits:
            body = g.run("log", "-1", "--format=%s", parts[0])
            if len(parts) != 2 or not body.ok or not body.text().startswith(machine + ": "):
                return False
        return True

    def move_main(self, g, new, old, message="kef skill backup"):
        """Point main at new (None removes it), then the real index at the same
        tree: the index only, never the clone's files."""
        if new:
            r = g.run("update-ref", "-m", message, BRANCH, new, old or ZERO)
        else:
            r = g.run("update-ref", "-d", BRANCH, old)
        if r.ok:
            g.run("read-tree", "--reset", new) if new else g.run("read-tree", "--empty")
        return r

    def strand(self, g, tip, base):
        """Keep tip under refs/kef/stranded-<UTC stamp>; point main at base."""
        stamp = self.now.astimezone(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        name = "refs/kef/stranded-" + stamp
        n = 1
        while g.rev(name):
            n += 1
            name = "refs/kef/stranded-%s-%02d" % (stamp, n)
        if not g.run("update-ref", "-m", "kef skill backup (set aside)", name, tip).ok:
            return None
        moved = self.move_main(g, base, tip, "kef skill backup (restart from server)")
        if not moved.ok:
            return None
        listing = g.run("for-each-ref", "--format=%(refname)", "refs/kef/").text().split()
        for old in sorted(x for x in listing if x.startswith("refs/kef/stranded-"))[:-STRAND_KEEP]:
            g.run("update-ref", "-d", old)
        return name

    def outside_commits(self, g, machine, base, tip):
        args = ["rev-list", "--parents", tip] + (["--not", base] if base else [])
        r = g.run(*args)
        if not r.ok:
            return "rev-list failed: " + r.first_error()
        for line in r.text().split("\n"):
            parts = line.split()
            if not parts:
                continue
            if len(parts) > 2:
                return "local merge commit %s" % parts[0][:7]
            d = g.run("diff-tree", "-r", "--no-commit-id", "--name-only", "-z", "--root", parts[0])
            if not d.ok:
                return "diff-tree failed: " + d.first_error()
            for path in d.text().split("\0"):
                if path and not path.startswith(machine + "/"):
                    return "local commit %s changes %s, outside %s/" % (parts[0][:7], path, machine)
        return None

    def replay(self, g, machine, base, tip):
        mb = g.run("merge-base", base, tip)
        mb = mb.text().strip() if mb.ok else None
        ok_base, at_base = g.subtree(base, machine)
        ok_mb, at_mb = g.subtree(mb, machine)
        if not (ok_base and ok_mb):
            return None, "ls-tree failed while comparing with the server"
        if at_base != at_mb:
            return None, "the server copy of %s/ changed under this computer (conflict)" % machine
        r = g.run("rev-list", "--reverse", tip, "--not", base)
        if not r.ok:
            return None, "rev-list failed: " + r.first_error()
        parent = base
        for commit in r.text().split():
            listing = g.run("ls-tree", "-z", parent + "^{tree}")
            if not listing.ok:
                return None, "ls-tree failed: " + listing.first_error()
            records = [x for x in listing.text().split("\0") if x and x.split("\t", 1)[1] != machine]
            ok_sub, sub = g.subtree(commit, machine)
            if not ok_sub:
                return None, "ls-tree failed on a local commit"
            if sub:
                records.append("040000 tree %s\t%s" % (sub, machine))
            mk = g.run("mktree", "-z", stdin="\0".join(records) + "\0")
            if not mk.ok:
                return None, "mktree failed: " + mk.first_error()
            raw = g.run("cat-file", "commit", commit)
            if not raw.ok:
                return None, "cat-file failed: " + raw.first_error()
            header, _sep, message = raw.out.partition(b"\n\n")
            author = re.search(rb"^author (.*) <(.*)> (\d+ [+-]\d{4})$", header, re.M)
            env = {}
            if author:
                env = {"GIT_AUTHOR_NAME": author.group(1).decode("utf-8", "replace"),
                       "GIT_AUTHOR_EMAIL": author.group(2).decode("utf-8", "replace"),
                       "GIT_AUTHOR_DATE": author.group(3).decode("ascii")}
            made = g.run("commit-tree", "--no-gpg-sign", mk.text().strip(), "-p", parent,
                         stdin=message, env=env)
            if not made.ok:
                return None, "commit-tree failed: " + made.first_error()
            parent = made.text().strip()
        return parent, None

    def execute(self):
        g = Git(self.repo, self.verbose)
        id_path = os.path.join(self.gitdir, "kef-machine-id")
        try:
            with open(id_path) as handle:
                machine = handle.read().strip()
        except OSError:
            return "error: no machine id at .git/kef-machine-id (run setup again)"
        if not MACHINE_RE.match(machine):
            return "error: machine id is not valid"
        self.recover(g)
        # Everything below writes only inside .git: the commit is built in a
        # private index and the clone's files are never touched after setup, so
        # nothing reaches a file a link may have pointed into the clone.
        start = g.rev(BRANCH)
        tip, self.summary, note = self.build(g, machine, start)
        outcome = self.publish(g, machine, tip)
        summary = self.summary
        counts = "%d added, %d changed, %d deleted" % (
            len(summary["added"]), len(summary["changed"]), len(summary["deleted"]))
        extra = []
        if summary["hold"]:
            extra.append("%d deletions held" % summary["hold"]["count"])
        if summary["withheld"]:
            extra.append("%d new files wait with them" % len(summary["withheld"]))
        if summary["secrets"]:
            extra.append("%d files left out (possible secret)" % len(summary["secrets"]))
        if summary["stale"]:
            extra.append("%d kept stale" % len(summary["stale"]))
        tail = (", " + ", ".join(extra)) if extra else ""
        if note == "no changes" and outcome == "up to date":
            return "no changes" + tail
        return "%s (%s%s)" % (outcome, counts, tail)


def refusal_is_transient(result):
    """A server-side lock or update race is retried, not treated as a refusal."""
    status = [line.lower() for line in result.out.decode("utf-8", "replace").splitlines()
              if "[remote rejected]" in line]
    return bool(status) and all(any(w in line for w in ("cannot lock ref", "failed to update ref",
                                                        "unable to lock", "failed to lock"))
                                for line in status)


def remote_reason(result):
    """The server's own words for a refused push: its first "remote:" line."""
    for line in result.err.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if line.startswith("remote:") and line[7:].strip():
            return line[7:].strip()[:200]
    for line in result.out.decode("utf-8", "replace").splitlines():
        if "[remote rejected]" in line:
            return line.split("[remote rejected]", 1)[1].strip()[:200]
    return result.first_error()


def load_json_file(path):
    try:
        with open(path) as handle:
            value = json.load(handle)
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def write_json_file(path, value):
    tmp = path + ".tmp"
    with open(tmp, "w") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(tmp, path)


def write_log(gitdir, line):
    path = os.path.join(gitdir, "kef-sync.log")
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line.replace("\n", " ") + "\n")
        if os.path.getsize(path) > LOG_CAP:
            with open(path, "rb") as handle:
                handle.seek(-LOG_CAP // 2, os.SEEK_END)
                keep = handle.read()
            keep = keep[keep.find(b"\n") + 1:]
            with open(path, "wb") as handle:
                handle.write(keep)
    except OSError:
        pass


def main(argv=None):
    parser = argparse.ArgumentParser(description="KEF skill backup: copy Claude skills into the backup clone and push.")
    parser.add_argument("--repo", required=True, help="the backup clone, e.g. ~/.kef-skills")
    parser.add_argument("--home", default=None, help="home folder to copy from (default: ~)")
    parser.add_argument("--now", default=None, help="run time override, ISO-8601 with offset (tests)")
    parser.add_argument("--wait", type=int, default=0, metavar="SECONDS",
                        help="if another run holds the lock, wait up to this long (setup uses it)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    repo = os.path.abspath(os.path.expanduser(args.repo))
    home = os.path.abspath(os.path.expanduser(args.home or "~"))
    now = now_from(args.now)
    gitdir = os.path.join(repo, ".git")
    if not os.path.isdir(gitdir):
        if args.verbose:
            sys.stderr.write("not a git clone: %s\n" % repo)
        return 0
    try:
        lock = open(os.path.join(gitdir, "kef-sync.lock"), "a")
    except OSError:
        return 0
    deadline = time.time() + max(args.wait, 0)
    while True:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:
            if time.time() >= deadline:
                if args.verbose:
                    sys.stderr.write("another run holds the lock\n")
                return 0
            time.sleep(1)
    try:
        outcome = Run(repo, home, now, args.verbose).execute()
    except Exception as exc:  # never fail launchd; log and retry next hour
        outcome = "error: %s: %s" % (type(exc).__name__, exc)
        if args.verbose:
            import traceback
            traceback.print_exc()
    write_log(gitdir, "%s %s" % (iso(now), outcome))
    if args.verbose:
        sys.stderr.write(outcome + "\n")
    lock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
