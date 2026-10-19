# kef-skill-agent

Keeps a continuous copy of the Claude Code skills, commands and agents you build on your Mac in your private KEF backup repository on GitHub. You keep working exactly as you do now; nothing needs to be shared by hand.

## What it copies

Once an hour it copies these three folders, and nothing else on your Mac:

- `~/.claude/skills/`
- `~/.claude/commands/`
- `~/.claude/agents/`

They land in this Mac's own folder (`mac-<id>/skills`, `/commands`, `/agents`) of your backup repository. `CLAUDE.md` files and project `.claude` folders are not copied. It skips junk (`.DS_Store`, `__pycache__`, `node_modules`, virtual environments, nested `.git` folders), any file over 20 MB, and links that do not point at a skill folder (or, inside `agents/`, at an agent `.md` file).

A file holding one of the recognised key formats (API keys, tokens, private keys, database passwords), or that GitHub refuses, is left out. The folder's `BLOCKED` file names it by path, line and type, never its content; once the key is removed the file is copied on the next run. The scan recognises key formats; it does not catch every secret. `BLOCKED` also lists anything that cannot be copied right now (unreadable, over a size limit, or linked into the backup clone), so a copy going stale is visible. A large deletion (a whole folder emptied, or more than 10 files at once) is held for 14 days before it is recorded.

## Install (once per Mac)

Accept the GitHub invitation to your backup repository and make sure `gh auth status` shows you signed in, then run:

```
curl -fsSL https://raw.githubusercontent.com/Nibenedi/kef-skill-agent/v1.0.0/install.sh | bash -s -- <your-id>
```

It ends with one line starting `SKILL_BACKUP=READY`. Running it again is safe.

## Check that it ran

```
tail -n 5 ~/.kef-skills/.git/kef-sync.log
launchctl print gui/$(id -u)/com.kef.skill-backup | grep -E 'state|runs|last exit'
```

Each run logs one line. On GitHub, `mac-<id>/LAST_SEEN` in your backup repository shows the last day this Mac checked in. The files you see in `~/.kef-skills` are a snapshot from setup time; to restore anything, take it from GitHub.

## Tests

```
/usr/bin/python3 -m unittest discover -s tests -v
```
