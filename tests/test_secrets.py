"""Secret scan. Every token here is generated at run time, so this file holds none."""

import base64
import hashlib
import json
import os
import random
import string
import unittest
import uuid

from helpers import BLOCKED_KEYS, MACHINE, World, git, load_module

P = MACHINE + "/"
RNG = random.Random(20261019)
ALNUM = string.ascii_letters + string.digits
UPPER = string.ascii_uppercase + string.digits
URLSAFE = ALNUM + "_-"


def rand(n, alphabet=ALNUM):
    return "".join(RNG.choice(alphabet) for _ in range(n))


HEX = "0123456789abcdef"
KEY = "PRIVATE " + "KEY"  # never written out whole, so no scanner flags this file


def private_key(label="RSA " + KEY, headers=()):
    body = base64.b64encode(bytes(RNG.randrange(256) for _ in range(600))).decode()
    lines = [body[i:i + 64] for i in range(0, len(body), 64)]
    head = "".join(h + "\n" for h in headers) + ("\n" if headers else "")
    return ("-----BEGIN " + label + "-----\n" + head + "\n".join(lines)
            + "\n-----END " + label + "-----\n")


def key_formats():
    """Private keys as they really sit in files: raw, escaped in JSON or .env, one line, PGP."""
    pem = private_key(KEY)
    service_account = json.dumps({
        "type": "service_account", "project_id": "demo-project",
        "private_key_id": rand(40, HEX), "private_key": pem,
        "client_email": "robot@demo-project.iam.gserviceaccount.com"}, indent=2)
    pgp_headers = ("Version: GnuPG v2", "Comment: Jane Doe <jane@example.invalid>")
    pem_headers = ("Proc-Type: 4,ENCRYPTED", "DEK-Info: AES-128-CBC," + rand(32, "0123456789ABCDEF"))
    return [
        ("raw PEM", pem),
        ("service-account JSON", service_account),
        (".env with escaped newlines", 'PRIVATE_KEY="' + pem.replace("\n", "\\n") + '"\n'),
        (".env on one line with spaces", "PRIVATE_KEY='" + pem.replace("\n", " ").strip() + "'\n"),
        ("PGP with a Comment header", private_key("PGP " + KEY + " BLOCK", pgp_headers)),
        ("encrypted PEM with Proc-Type", private_key("RSA " + KEY, pem_headers)),
        ("PGP on one line with headers", private_key("PGP " + KEY + " BLOCK", pgp_headers).replace("\n", " ")),
        ("encrypted PEM on one line", private_key("RSA " + KEY, pem_headers).replace("\n", " ")),
        ("JSON inside JSON (double escaped)", json.dumps({"inner": json.dumps({"private_key": pem})})),
        ("armor headers longer than 600 characters", private_key("PGP " + KEY + " BLOCK", ("Comment: " + "note " * 200,))),
    ]


def a_uuid():
    return str(uuid.UUID(int=RNG.getrandbits(128)))


def samples():
    """One generated value per pattern; prefixes are split so this file holds no token."""
    return [
        ("aws_access_key_id", "AK" + "IA" + rand(16, UPPER)),
        ("aws_access_key_id", "AS" + "IA" + rand(16, UPPER)),
        ("github_token", "gh" + "p_" + rand(36)),
        ("github_token", "gh" + "o_" + rand(36)),
        ("github_token", "gh" + "u_" + rand(36)),
        ("github_token", "gh" + "s_" + rand(36)),
        ("github_token", "gh" + "r_" + rand(36)),
        ("github_fine_grained_token", "github" + "_pat_" + rand(22) + "_" + rand(59)),
        ("slack_token", "xo" + "xb-" + rand(12, string.digits) + "-" + rand(13, string.digits) + "-" + rand(24)),
        ("slack_token", "xo" + "xp-" + rand(11, string.digits) + "-" + rand(11, string.digits) + "-"
         + rand(12, string.digits) + "-" + rand(32, HEX)),
        ("slack_webhook_url", "https://hooks.slack.com/" + "services/T" + rand(9, UPPER) + "/B"
         + rand(10, UPPER) + "/" + rand(24)),
        ("slack_app_token", "xa" + "pp-1-A" + rand(10, UPPER) + "-" + rand(13, string.digits) + "-" + rand(64, HEX)),
        ("anthropic_api_key", "sk-" + "ant-api03-" + rand(93, URLSAFE)),
        ("openai_api_key", "sk-" + "proj-" + rand(156, URLSAFE)),
        ("openai_api_key", "sk-" + rand(20) + "T3Blbk" + "FJ" + rand(20)),
        ("google_api_key", "AI" + "za" + "Sy" + rand(33, URLSAFE)),
        ("google_oauth_client_secret", "GOC" + "SPX-" + rand(28, URLSAFE)),
        ("google_oauth_refresh_token", "1/" + "/0" + rand(98, URLSAFE)),
        ("stripe_live_key", "sk" + "_live_" + rand(99)),
        ("stripe_live_key", "rk" + "_live_" + rand(24)),
        ("notion_token", "nt" + "n_" + rand(46)),
        ("notion_token", "sec" + "ret_" + rand(43)),
        ("gitlab_token", "gl" + "pat-" + rand(20, URLSAFE)),
        ("npm_token", "np" + "m_" + rand(36)),
        ("sendgrid_api_key", "S" + "G." + rand(22, URLSAFE) + "." + rand(43, URLSAFE)),
        ("hubspot_private_app_token", "pat" + "-na1-" + a_uuid()),
        ("hubspot_private_app_token", "pat" + "-eu1-" + a_uuid()),
        ("airtable_token", "pa" + "t" + rand(14) + "." + rand(64, HEX)),
        ("linear_api_key", "lin" + "_api_" + rand(40)),
        ("aws_secret_access_key", "aws_secret_" + "access_key = " + rand(40, ALNUM + "/+")),
        ("aws_secret_access_key", "AWS_SECRET_" + "ACCESS_KEY=\"" + rand(40, ALNUM + "/+") + "\""),
        ("database_url_password", "postgres" + "ql://app:" + rand(20) + "@db.internal:5432/app"),
        ("database_url_password", "my" + "sql://svc:" + rand(16) + "@10.0.0.5/app"),
        ("private_key_block", private_key()),
    ]


def false_positives():
    image = base64.b64encode(b"\x89PNG\r\n\x1a\n" + bytes(RNG.randrange(256) for _ in range(6000))).decode()
    fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(rand(30).encode()).digest()).decode().rstrip("=")
    sha256 = hashlib.sha256(rand(10).encode()).hexdigest()
    sha1 = hashlib.sha1(rand(10).encode()).hexdigest()
    return [
        "AKIA" + "IOSFODNN7" + "EXAMPLE",
        "aws_access_key_id = " + "AKIA" + "X" * 16,
        "aws_access_key_id = " + "AKIA" + "0" * 16,
        "export OPENAI_API_KEY=" + "sk-...",
        "OpenAI keys start with sk- and are long; never paste one into a skill.",
        "ANTHROPIC_API_KEY=" + "sk-ant-" + "x" * 30,
        "ANTHROPIC_API_KEY=" + "sk-ant-api03-" + "your-key-here-0123456789",
        "OPENAI_API_KEY=" + "sk-proj-" + "your-key-goes-here-abcdefghijklmnopqrstuvwxyz",
        "OPENAI_API_KEY=" + "<sk-your-key>",
        "token: " + "gh" + "p_" + "x" * 36,
        "token: <your-github-token>",
        "SLACK_BOT_TOKEN=" + "xoxb-your-bot-token",
        "SLACK_BOT_TOKEN=" + "xo" + "xb-000000000000-0000000000000-" + "x" * 24,
        "SLACK_APP_TOKEN=" + "xa" + "pp-your-app-token",
        "https://hooks.slack.com/" + "services/T00000000/B00000000/" + "X" * 24,
        "sk-" + "a" * 48,
        "AIza" + "x" * 35,
        "GOOGLE_CLIENT_SECRET=" + "GOC" + "SPX-" + "x" * 28,
        "sk_live_" + "0" * 24,
        "sk_live_...",
        "NOTION_TOKEN=" + "sec" + "ret_" + "your_notion_integration_token",
        "the sec" + "ret_" + "a" * 43 + " setting",
        "Set the secret_key and secret_access_key options.",
        "np" + "m_config_registry=https://registry.npmjs.org",
        "process.env.np" + "m_package_version",
        "S" + "G.xxxxxxxx.yyyyyyyy",
        "pat" + "-na1-" + "00000000-0000-0000-0000-000000000000",
        "lin" + "_api_" + "x" * 40,
        "aws_secret_" + "access_key = " + "wJalrXUtnFEMI/K7MDENG/bPxRfiCY" + "EXAMPLEKEY",
        "postgres" + "://user:password@localhost:5432/app",
        "postgres" + "ql://postgres:postgres@db/app",
        "my" + "sql://root:${MYSQL_PASSWORD}@db/app",
        "postgres" + "://user:<password>@host/db",
        "postgres" + "ql://app:pass@localhost/app",
        "DATABASE_URL=" + "postgres" + "://app:changeme@db/app",
        "my" + "sql://user:****@host/db",
        # Local or example hosts and stock passwords in docs and compose files.
        "postgres" + "://app:" + "Xy7pQ2rLm9vB@localhost:5432/app",
        "postgres" + "ql://user:" + "password123@127.0.0.1/db",
        "my" + "sql://root:" + "Kd83jfLq0zP@0.0.0.0:3306/app",
        "postgres" + "://admin:" + "admin123@host.docker.internal/app",
        "postgres" + "ql://app:" + "Tq5wE8rYu2iO@db.example.com/app",
        "postgres" + "://app:" + "Tq5wE8rYu2iO@[::1]:5432/app",
        "postgres" + "://app:" + "mysecretpassword@db/app",
        "postgres" + "://app:" + "secret123@db/app",
        "postgres" + "://app:" + "changeit@db/app",
        "my" + "sql://app:" + "admin123@db/app",
        "postgres" + "ql://app:" + "password123@db/app",
        "postgres" + "ql://appuser:" + "appuser@db/app",
        # Near misses of real formats.
        "x" + "pat-na1-" + a_uuid(),
        "aws_secret_" + "access_key = " + rand(41, ALNUM + "/+"),
        "sec" + "ret_" + rand(43, string.ascii_lowercase + string.digits),
        "sec" + "ret_" + rand(43, string.ascii_letters),
        "-----BEGIN " + KEY + "-----\n...\n-----END " + KEY + "-----\n",
        '"private_key": "-----BEGIN ' + KEY + '-----\\n...\\n-----END ' + KEY + '-----\\n"',
        "-----BEGIN " + KEY + "-----\n" + "X" * 64 + "\n-----END " + KEY + "-----\n",
        "1//0" + "x" * 40,
        "id: " + a_uuid(),
        "sha256 " + sha256,
        "commit " + sha1,
        "![chart](data:image/png;base64," + image + ")",
        "Post it in the channel C0123ABCDEF (team T0123ABCD).",
        "the task-" + "a1B2c3D4e5" * 5 + " is unrelated",
        # A BEGIN marker mentioned in prose, then a fingerprint or a hash, with no END marker.
        "Keys start with -----BEGIN OPENSSH " + KEY + "----- ... 256 " + fingerprint + " ...",
        '"-----BEGIN ' + KEY + '-----" refuse it.\nPinned commit: ' + sha1,
        "-----BEGIN " + KEY + "----- is the header.\nsha256 " + sha256,
    ]


class ScannerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load_module()

    def test_each_pattern_detected(self):
        kinds = set()
        for kind, token in samples():
            hits = self.mod.scan_secrets(("line one\nkey = " + token + "\n").encode())
            self.assertEqual(hits, [(2, kind)], kind)
            kinds.add(kind)
        self.assertEqual(kinds, set(k for k, _ in self.mod.SECRET_PATTERNS))

    def test_private_key_formats_detected(self):
        for label, text in key_formats():
            hits = self.mod.scan_secrets(("first line\n" + text).encode())
            self.assertEqual([k for _, k in hits], ["private_key_block"], label)
            line = text.split("-----BEGIN")[0].count("\n") + 2
            self.assertEqual(hits[0][0], line, label)

    def test_false_positives_ignored(self):
        for text in false_positives():
            self.assertEqual(self.mod.scan_secrets(text.encode()), [], text[:60])

    def test_utf16_text_scanned(self):
        token = "AK" + "IA" + rand(16, UPPER)
        data = ("first\r\naws = " + token + "\r\n").encode("utf-16")
        for tail in (b"", b"\n", b"\x00\xd8"):  # clean, odd length, a lone surrogate
            self.assertEqual(self.mod.scan_secrets(data + tail), [(2, "aws_access_key_id")], tail)

    def test_binary_scanned_as_latin1(self):
        token = "gh" + "p_" + rand(36)
        data = bytes(range(256)) + b"\xff\xfe" + token.encode() + b"\x00\x81"
        # bytes(range(256)) holds one newline, so the token sits on line 2.
        self.assertEqual(self.mod.scan_secrets(data), [(2, "github_token")])


class SecretSyncTests(unittest.TestCase):
    def setUp(self):
        self.world = World()
        self.mac = self.world.mac()

    def tearDown(self):
        self.world.cleanup()

    def test_hit_leaves_only_that_file_out_and_records_no_content(self):
        m = self.mac
        token = "gh" + "p_" + rand(36)
        m.write("skills/a/SKILL.md", "a\n")
        m.write("skills/a/config.md", "line 1\nline 2\nGITHUB_TOKEN=" + token + "\n")
        m.sync()
        files = self.world.files(P)
        self.assertIn(P + "skills/a/SKILL.md", files)
        self.assertNotIn(P + "skills/a/config.md", files)
        raw = self.world.show(P + "BLOCKED")
        self.assertNotIn(token.encode(), raw)
        self.assertNotIn(b"GITHUB_TOKEN", raw)
        blocked = self.world.blocked()
        self.assertEqual(set(blocked), BLOCKED_KEYS)
        self.assertIsNone(blocked["held_deletions"])
        self.assertEqual(blocked["secrets"], [{"path": P + "skills/a/config.md", "line": 3, "kind": "github_token"}])
        self.assertEqual(blocked["since"], "2026-10-19T09:00:00-04:00")
        message = self.world.message()
        self.assertIn(P + "skills/a/config.md", message)
        self.assertNotIn(token, message)
        # The secret never lands in the clone either.
        log = self.mac.git("log", "--all", "-p", "--format=%B")
        self.assertNotIn(token, log)

    def test_changed_file_keeps_old_version_then_commits_when_cleaned(self):
        m = self.mac
        m.write("agents/helper.md", "safe v1\n")
        m.sync()
        token = "AK" + "IA" + rand(16, UPPER)
        m.write("agents/helper.md", "safe v2\naws = " + token + "\n")
        m.write("agents/other.md", "other\n")
        m.sync(now="2026-10-19T10:00:00-04:00")
        self.assertEqual(self.world.show(P + "agents/helper.md"), b"safe v1\n")
        self.assertIn(P + "agents/other.md", self.world.files(P))
        self.assertEqual(self.world.blocked()["secrets"],
                         [{"path": P + "agents/helper.md", "line": 2, "kind": "aws_access_key_id"}])
        # Unchanged secret next hour: no new commit.
        count = self.world.count()
        m.sync(now="2026-10-19T11:00:00-04:00")
        self.assertEqual(self.world.count(), count)
        # Secret removed: the file commits and BLOCKED goes away.
        m.write("agents/helper.md", "safe v3\n")
        m.sync(now="2026-10-19T12:00:00-04:00")
        self.assertEqual(self.world.show(P + "agents/helper.md"), b"safe v3\n")
        self.assertIsNone(self.world.blocked())

    def test_service_account_json_left_out(self):
        text = dict(key_formats())["service-account JSON"]
        self.mac.write("skills/a/SKILL.md", "a\n")
        self.mac.write("skills/a/sa.json", text)
        self.mac.sync()
        self.assertNotIn(P + "skills/a/sa.json", self.world.files(P))
        self.assertEqual(self.world.blocked()["secrets"][0]["kind"], "private_key_block")
        self.assertNotIn(b"BEGIN", self.world.show(P + "BLOCKED"))

    def test_hand_commit_inside_machine_folder_with_a_secret_never_reaches_the_server(self):
        m = self.mac
        m.write("skills/a/SKILL.md", "a\n")
        m.sync()
        token = "gh" + "p_" + rand(36)
        path = os.path.join(m.clone, MACHINE, "skills", "k", "creds.md")
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as handle:
            handle.write("token = " + token + "\n")
        m.git("add", P + "skills/k/creds.md")
        m.git("commit", "-q", "-m", "hand commit")
        m.write("skills/a/later.md", "later\n")
        self.assertEqual(m.sync(now="2026-10-19T10:00:00-04:00").returncode, 0)
        history = git("--git-dir", self.world.origin, "log", "-p", "--all")
        self.assertNotIn(token, history)
        self.assertIn(P + "skills/a/later.md", self.world.files(P))
        self.assertNotIn(P + "skills/k/creds.md", self.world.files(P))
        self.assertIn("possible secret in " + P + "skills/k/creds.md", m.log()[-1])
        self.assertNotIn(token, "\n".join(m.log()))

    def test_hand_commit_message_with_a_secret_never_reaches_the_server(self):
        m = self.mac
        m.write("skills/a/SKILL.md", "a\n")
        m.sync()
        token = "gl" + "pat-" + rand(20, URLSAFE)
        path = os.path.join(m.clone, MACHINE, "skills", "k", "notes.md")
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as handle:
            handle.write("harmless notes\n")
        m.git("add", P + "skills/k/notes.md")
        m.git("commit", "-q", "-m", "notes, token " + token)
        m.write("skills/a/later.md", "later\n")
        self.assertEqual(m.sync(now="2026-10-19T10:00:00-04:00").returncode, 0)
        self.assertNotIn(token, git("--git-dir", self.world.origin, "log", "--all", "--format=%B"))
        self.assertIn(P + "skills/a/later.md", self.world.files(P))
        self.assertIn("commit message holds a possible secret", m.log()[-1])
        self.assertNotIn(token, "\n".join(m.log()))

    def test_binary_file_with_key_left_out(self):
        token = "sk" + "_live_" + rand(30)
        self.mac.write("skills/a/SKILL.md", "a\n")
        self.mac.write("skills/a/blob.bin", b"\x00\x01\xfe" + token.encode() + b"\xff")
        self.mac.sync()
        self.assertNotIn(P + "skills/a/blob.bin", self.world.files(P))
        self.assertEqual(self.world.blocked()["secrets"][0]["kind"], "stripe_live_key")


if __name__ == "__main__":
    unittest.main()
