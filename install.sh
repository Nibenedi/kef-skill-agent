#!/bin/bash
# KEF skill backup: one-time setup on a Mac (bash 3.2 compatible).
#
#   curl -fsSL https://raw.githubusercontent.com/Nibenedi/kef-skill-agent/<ref>/install.sh | bash -s -- <id>
#
# Safe to run again: same computer folder, same clone, agent moved to AGENT_REF,
# launch agent replaced. Test overrides: KEF_AGENT_REPO_URL, KEF_AGENT_REF,
# KEF_PERSON_REPO_URL, KEF_LAUNCHCTL, KEF_SKIP_IOREG (a fixed hardware UUID).

AGENT_REF="v1.0.0"

main() {
  set -u
  exec </dev/null

  local id="${1:-}"
  local ref="${KEF_AGENT_REF:-$AGENT_REF}"
  local agent_url="${KEF_AGENT_REPO_URL:-https://github.com/Nibenedi/kef-skill-agent.git}"
  local person_url="${KEF_PERSON_REPO_URL:-https://github.com/Nibenedi/kef-skills-$id.git}"
  local launchctl_bin="${KEF_LAUNCHCTL:-/bin/launchctl}"
  local python="/usr/bin/python3"
  local label="com.kef.skill-backup"
  local agent_dir="$HOME/.kef-skill-agent"
  local clone="$HOME/.kef-skills"
  local plist="$HOME/Library/LaunchAgents/$label.plist"

  local id_re='^[a-z][a-z0-9-]{1,30}$'
  [[ "$id" =~ $id_re ]] \
    || fail "usage: bash install.sh <id> (lowercase letters, digits and dashes, starting with a letter)."
  [ "$(uname -s 2>/dev/null)" = "Darwin" ] || fail "this setup is for macOS only."
  git --version >/dev/null 2>&1 \
    || fail "git is not usable on this Mac. Install Apple's Command Line Tools with: xcode-select --install, then run this again."
  "$python" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1 \
    || fail "python3 3.9 or newer is not usable on this Mac. Install Apple's Command Line Tools with: xcode-select --install, then run this again."

  # Nothing changes until every check has passed.
  check_existing "$agent_dir" "$agent_url"
  check_existing "$clone" "$person_url"
  GIT_TERMINAL_PROMPT=0 git ls-remote "$person_url" >/dev/null 2>&1 \
    || fail "cannot reach $person_url. Accept the GitHub invitation at https://github.com/Nibenedi/kef-skills-$id/invitations, check the GitHub sign-in with: gh auth status (if needed run: gh auth login --hostname github.com --git-protocol https --web, then: gh auth setup-git), then run this again."

  # The agent, pinned to the ref.
  if [ ! -d "$agent_dir/.git" ]; then
    GIT_TERMINAL_PROMPT=0 git clone -q --no-checkout "$agent_url" "$agent_dir" >/dev/null 2>&1 \
      || fail "could not download the backup agent from $agent_url."
  fi
  GIT_TERMINAL_PROMPT=0 git -C "$agent_dir" fetch -q --force --tags origin '+refs/heads/*:refs/remotes/origin/*' >/dev/null 2>&1 \
    || fail "could not update the backup agent in $agent_dir."
  local sha
  sha="$(git -C "$agent_dir" rev-parse -q --verify "refs/tags/$ref^{commit}" 2>/dev/null \
    || git -C "$agent_dir" rev-parse -q --verify "refs/remotes/origin/$ref^{commit}" 2>/dev/null \
    || git -C "$agent_dir" rev-parse -q --verify "$ref^{commit}" 2>/dev/null)" \
    || fail "agent version $ref was not found in $agent_url."
  git -C "$agent_dir" -c advice.detachedHead=false checkout -q -f --detach "$sha" >/dev/null 2>&1 \
    && git -C "$agent_dir" clean -q -f -d -x >/dev/null 2>&1 \
    || fail "could not check out agent version $ref in $agent_dir."
  [ -f "$agent_dir/kef_skill_sync.py" ] || fail "agent version $ref has no kef_skill_sync.py."

  # The person's backup clone, this Mac's id and the repo-local identity.
  if [ ! -d "$clone/.git" ]; then
    GIT_TERMINAL_PROMPT=0 git clone -q "$person_url" "$clone" >/dev/null 2>&1 \
      || fail "could not clone $person_url into $clone."
  fi
  local uuid
  if [ -n "${KEF_SKIP_IOREG:-}" ]; then
    uuid="$KEF_SKIP_IOREG"
  else
    uuid="$(ioreg -rd1 -c IOPlatformExpertDevice 2>/dev/null | awk -F'"' '/"IOPlatformUUID"/ {print $4; exit}')"
  fi
  [ -n "$uuid" ] || fail "could not read this Mac's hardware UUID from ioreg."
  local hex
  hex="$("$python" -c 'import hashlib, sys; print(hashlib.sha256(sys.argv[1].encode()).hexdigest()[:12])' "$uuid")" \
    || fail "could not compute this Mac's id."
  local machine="mac-$hex"
  printf '%s\n' "$machine" > "$clone/.git/kef-machine-id" || fail "could not write $clone/.git/kef-machine-id."
  git -C "$clone" config user.name "$id skill backup" && \
    git -C "$clone" config user.email "kef-skill-backup@users.noreply.github.com" \
    || fail "could not set the backup identity in $clone."

  # First sync, synchronously, before the launch agent exists (a run already in
  # progress is waited for). It counts only if this run logged a success and the
  # server's main now holds this Mac's folder.
  local log="$clone/.git/kef-sync.log" attempt before last landed=""
  for attempt in 1 2 3; do
    before="$({ wc -c < "$log"; } 2>/dev/null | tr -d ' ')"
    "$python" "$agent_dir/kef_skill_sync.py" --repo "$clone" --wait 1800 >/dev/null 2>&1
    if [ "$({ wc -c < "$log"; } 2>/dev/null | tr -d ' ')" != "$before" ]; then
      last="$(tail -n 1 "$log" 2>/dev/null | cut -d' ' -f2-)"
      if backup_landed "$clone" "$machine" "$last"; then
        landed=1
        break
      fi
    fi
    [ "$attempt" = 3 ] || sleep 5
  done
  [ -n "$landed" ] || fail "the first backup did not reach GitHub ($(tail -n 1 "$log" 2>/dev/null | cut -c1-200)). Run this again in a few minutes."

  # The launch agent: hourly, and once at login.
  mkdir -p "$HOME/Library/LaunchAgents" || fail "could not create $HOME/Library/LaunchAgents."
  local tmp="$plist.tmp.$$"
  cat > "$tmp" <<EOF || fail "could not write $plist."
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$label</string>
  <key>ProgramArguments</key>
  <array>
    <string>$python</string>
    <string>$(xml "$agent_dir/kef_skill_sync.py")</string>
    <string>--repo</string>
    <string>$(xml "$clone")</string>
  </array>
  <key>StartInterval</key>
  <integer>3600</integer>
  <key>RunAtLoad</key>
  <true/>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>HOME</key>
    <string>$(xml "$HOME")</string>
  </dict>
  <key>StandardOutPath</key>
  <string>$(xml "$clone/.git/kef-sync.launchd.log")</string>
  <key>StandardErrorPath</key>
  <string>$(xml "$clone/.git/kef-sync.launchd.log")</string>
  <key>ProcessType</key>
  <string>Background</string>
  <key>LowPriorityIO</key>
  <true/>
</dict>
</plist>
EOF
  if command -v plutil >/dev/null 2>&1; then
    plutil -lint "$tmp" >/dev/null 2>&1 || { rm -f "$tmp"; fail "the launch agent file did not pass plutil -lint."; }
  fi
  mv -f "$tmp" "$plist" || fail "could not write $plist."

  local uid
  uid="$(id -u)"
  "$launchctl_bin" bootout "gui/$uid/$label" >/dev/null 2>&1 || true
  local loaded=""
  for attempt in 1 2 3; do
    if "$launchctl_bin" bootstrap "gui/$uid" "$plist" >/dev/null 2>&1; then
      loaded=1
      break
    fi
    sleep 1
  done
  [ -n "$loaded" ] || fail "launchctl could not load $plist."

  echo "Setup finished. Paste the next line into your setup task:"
  echo "SKILL_BACKUP=READY $id $machine $(git -C "$clone" rev-parse --short HEAD)"
  exit 0
}

fail() {
  echo "KEF skill backup setup stopped: $1" >&2
  exit 1
}

backup_landed() {
  local clone="$1" machine="$2" last="$3" here there
  case "$last" in
    "pushed "*|"up to date"*|"no changes"*) ;;
    *) return 1 ;;
  esac
  GIT_TERMINAL_PROMPT=0 git -C "$clone" fetch -q origin '+refs/heads/main:refs/remotes/origin/main' >/dev/null 2>&1 || return 1
  here="$(git -C "$clone" rev-parse -q --verify refs/heads/main 2>/dev/null)"
  there="$(git -C "$clone" rev-parse -q --verify refs/remotes/origin/main 2>/dev/null)"
  [ -n "$here" ] && [ "$here" = "$there" ] || return 1
  git -C "$clone" cat-file -e "refs/remotes/origin/main:$machine/LAST_SEEN" 2>/dev/null
}

xml() {
  printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'
}

norm_url() {
  local u="$1"
  u="${u%/}"
  u="${u%.git}"
  printf '%s' "$u" | tr 'A-Z' 'a-z'
}

check_existing() {
  local dir="$1" want="$2" have
  [ -e "$dir" ] || return 0
  [ -d "$dir/.git" ] || fail "$dir already exists and is not a git clone. Move it aside, then run this again."
  # The configured URL, not the one after any url.<base>.insteadOf rewrite.
  have="$(git -C "$dir" config --get remote.origin.url 2>/dev/null)"
  if [ -z "$have" ] || [ "$(norm_url "$have")" != "$(norm_url "$want")" ]; then
    fail "$dir already exists and points at ${have:-no remote}, not $want. Move it aside, then run this again."
  fi
}

main "$@"
