#!/bin/bash
# End-to-end tests for claude-secure-sandbox (macOS).
#
# Everything happens in a temporary directory: fake HOME, test repository,
# fake claude binary that runs a scenario inside the sandbox. No real file of
# the machine is read.
set -u

ROOT=$(cd -P "$(dirname "$0")/.." && pwd)
OS=$(uname)
T=${CS_TEST_DIR:-$(mktemp -d "${TMPDIR:-/tmp}/cs-test.XXXXXX")}
T=$(cd -P "$T" && pwd)
PASS=0; FAILED=0; FAIL_NAMES=
PF=.claudeprotected
PLACEHOLDER="[claude-secure-sandbox] hidden by $PF"

ok()   { PASS=$((PASS + 1)); printf '  ok    %s\n' "$1"; }
fail() { FAILED=$((FAILED + 1)); FAIL_NAMES="$FAIL_NAMES
  - $1"; printf '  FAIL  %s\n' "$1"; }
check() { local name=$1; shift; if "$@" >/dev/null 2>&1; then ok "$name"; else fail "$name"; fi; }
check_not() { local name=$1; shift; if "$@" >/dev/null 2>&1; then fail "$name"; else ok "$name"; fi; }
section() { printf '\n== %s\n' "$1"; }
# Collects the IN-OK / IN-FAIL lines printed by a scenario run inside the sandbox.
collect() {
  sed -n 's/^IN-OK /  ok    /p' "$T/out/last"
  PASS=$((PASS + $(grep -c '^IN-OK ' "$T/out/last")))
  grep '^IN-FAIL ' "$T/out/last" | sed 's/^IN-FAIL //' > "$T/out/infail"
  while IFS= read -r n; do [ -n "$n" ] && fail "$n"; done < "$T/out/infail"
}

# --- isolated environment --------------------------------------------------------

rm -rf "$T/home" "$T/repo" "$T/fakebin" "$T/plain" "$T/out"
mkdir -p "$T/home/.ssh" "$T/home/.claude" "$T/fakebin" "$T/out"
echo "FAKE-SSH-KEY" > "$T/home/.ssh/id_test"
echo '{"theme":"dark"}' > "$T/home/.claude/settings.json"
mkdir -p "$T/home/.claude/skills/demo" "$T/home/.claude/projects/-old-session"
echo "demo skill" > "$T/home/.claude/skills/demo/SKILL.md"
echo "OLD-TRANSCRIPT-WITH-PROTECTED-CODE" > "$T/home/.claude/projects/-old-session/t.jsonl"
cat > "$T/home/.claude.json" <<EOF
{"mcpServers":{"demo":{"command":"true"}},"oauthAccount":{"emailAddress":"dev@example.com"},
 "projects":{"$T/repo":{"mcpServers":{"repo-mcp":{"command":"true"}}},"/other/project":{"mcpServers":{"other-mcp":{"command":"true"}}}}}
EOF
echo '{"token":"FAKE-JSON-SECRET"}' > "$T/secret.json"
printf '[user]\n\tname = Dev\n\temail = dev@example.com\n[init]\n\tdefaultBranch = main\n' > "$T/home/.gitconfig"

export HOME=$T/home
export GIT_CONFIG_GLOBAL=$T/home/.gitconfig
export GIT_CONFIG_NOSYSTEM=1
export CLAUDE_SECURE_SANDBOX_HOME=$T/home/.cache/claude-secure-sandbox
export CLAUDE_SECURE_SANDBOX_CONFIG=$T/config.json
export CS_TEST_SECRET_JSON=$T/secret.json
export CS_TEST_PLACEHOLDER=$PLACEHOLDER
export CS_TEST_EXPECTED=$T/fakebin/expected-head
export PATH=$ROOT/bin:$T/fakebin:$PATH
unset CLAUDE_SECURE_SANDBOX_ACTIVE CLAUDE_CONFIG_DIR ZDOTDIR XDG_CONFIG_HOME
STATE_ROOT=$CLAUDE_SECURE_SANDBOX_HOME

cat > "$T/config.json" <<EOF
{ "claude_bin": "$T/fakebin/claude" }
EOF

# Fake claude: outside the sandbox it says so; inside it runs the scenario
# named by CS_TEST_SCENARIO.
cat > "$T/fakebin/claude" <<'EOF'
#!/bin/bash
if [ -z "${CLAUDE_SECURE_SANDBOX_ACTIVE:-}" ]; then echo "UNSANDBOXED $*"; exit 0; fi
echo "SANDBOXED cwd=$(pwd -P)"
echo "ARGS $*"
[ -n "${CS_TEST_SCENARIO:-}" ] && exec bash "$CS_TEST_SCENARIO"
exit 0
EOF
chmod +x "$T/fakebin/claude"

# Fake keychain: only Claude Code's login, read from a file.
cat > "$T/fakebin/security" <<'EOF'
#!/bin/bash
if [ "$1" = find-generic-password ] && [[ " $* " == *" Claude Code-credentials "* ]] && [ -f "$CS_TEST_KEYCHAIN" ]; then
  cat "$CS_TEST_KEYCHAIN"; exit 0
fi
exit 44
EOF
chmod +x "$T/fakebin/security"
echo ':' > "$T/fakebin/s-noop.sh"
if [ "$OS" = Darwin ]; then export CS_TEST_KEYCHAIN=$T/keychain.json
else export CS_TEST_KEYCHAIN=$T/home/.claude/.credentials.json; fi
echo '{"claudeAiOauth":{"accessToken":"FAKE-OAUTH-1","refreshToken":"r","expiresAt":1000}}' > "$CS_TEST_KEYCHAIN"

# --- developer repository ------------------------------------------------------------

D=$T/repo
git init -q "$D"
(
  cd "$D" || exit 1
  mkdir -p src secret lib/deep/core lib/deep/public
  echo "public v1" > src/a.txt
  echo "SECRET-ONE" > secret/s.txt
  echo "MOVED-SECRET" > src/later.txt
  echo "DEEP-SECRET" > lib/deep/core/x.c
  echo "deep public" > lib/deep/public/y.c
  echo "DUP-SECRET" > secret/dup.txt
  echo "DUP-SECRET" > src/dup.txt
  : > secret/empty.txt
  : > src/empty.txt
  printf '# proprietary code\nsecret/\nlib/deep/core\n' > "$PF"
  git add -A && git commit -qm c1
  git mv src/later.txt secret/later.txt
  echo "SECRET-TWO" > secret/s.txt
  git commit -qam c2
  echo "public v2" > src/a.txt
  git commit -qam c3
  git checkout -q -b side
  echo "SIDE-COPY" > src/side.txt
  echo "SIDE-COPY" > secret/side.txt
  git add -A && git commit -qm side1
  git checkout -q main
  git merge -q -s ours --no-edit side
  git branch -q -D side
  git branch -q other
)
SECRETS='SECRET-ONE SECRET-TWO MOVED-SECRET DEEP-SECRET DUP-SECRET SIDE-COPY FAKE-SSH-KEY'

# The developer signs their commits with an SSH key, which must stay out of
# the sandbox: the developer signs Claude's commits when integrating them.
ssh-keygen -q -t ed25519 -N '' -C dev@example.com -f "$T/home/.ssh/id_signing"
printf 'dev@example.com namespaces="git" %s\n' "$(cat "$T/home/.ssh/id_signing.pub")" > "$T/allowed_signers"
git -C "$D" config gpg.format ssh
git -C "$D" config user.signingkey "$T/home/.ssh/id_signing"
git -C "$D" config commit.gpgsign true
git -C "$D" config gpg.ssh.allowedSignersFile "$T/allowed_signers"

# Files outside the user's home that the sandbox must not read either.
if [ "$OS" = Darwin ]; then USER_TMP=$(getconf DARWIN_USER_TEMP_DIR); else USER_TMP=/var/tmp/; fi
PROBE_TMP=$(mktemp /tmp/cs-probe.XXXXXX); echo "PROBE" > "$PROBE_TMP"
PROBE_USER_TMP=$(mktemp "${USER_TMP}cs-probe.XXXXXX"); echo "PROBE" > "$PROBE_USER_TMP"
trap 'rm -f "$PROBE_TMP" "$PROBE_USER_TMP"' EXIT

COPY=$STATE_ROOT/repo-$(python3 -c 'import hashlib, sys; print(hashlib.sha1(sys.argv[1].encode()).hexdigest()[:10])' "$D")

run_claude() { # scenario, args...
  local scenario=$1; shift
  ( cd "$D" && CS_TEST_SCENARIO=$scenario claude "$@" ) < /dev/null > "$T/out/last" 2>&1
}
# Same, in a terminal that answers the wrapper's question.
run_claude_answer() { # answer, scenario, args...
  local answer=$1 scenario=$2; shift 2
  if [ "$OS" = Darwin ]; then
    ( cd "$D" && { printf '%s\n\n' "$answer"; sleep 3; } |
        CS_TEST_SCENARIO=$scenario script -q /dev/null claude "$@" ) > "$T/out/last" 2>&1
  else
    ( cd "$D" && { printf '%s\n\n' "$answer"; sleep 3; } |
        CS_TEST_SCENARIO=$scenario script -q -e -c "claude $*" /dev/null ) > "$T/out/last" 2>&1
  fi
}

# --- 1. first session --------------------------------------------------------------

section "first session: filtered copy and sandbox"

cat > "$T/fakebin/s1.sh" <<EOF
t()  { if "\$@" >/dev/null 2>&1; then echo "IN-OK \$name"; else echo "IN-FAIL \$name"; fi; }
tn() { if "\$@" >/dev/null 2>&1; then echo "IN-FAIL \$name"; else echo "IN-OK \$name"; fi; }
name="reading the developer's repository is blocked";  tn cat "$D/src/a.txt"
name="reading the developer's .git is blocked";        tn ls "$D/.git"
name="reading the fake ~/.ssh is blocked";             tn cat "$T/home/.ssh/id_test"
name="reading the signing key is blocked";             tn cat "$T/home/.ssh/id_signing"
name="no signing in the sandbox";                      t sh -c '[ "\$(git config --get commit.gpgsign)" = false ]'
name="reading ~/.claude transcripts is blocked";       tn cat "$T/home/.claude/projects/-old-session/t.jsonl"
name="Claude settings passed through";                 t grep -q dark "\$CLAUDE_CONFIG_DIR/settings.json"
name="skills passed through";                          t grep -q "demo skill" "\$CLAUDE_CONFIG_DIR/skills/demo/SKILL.md"
name="~/.claude settings are not writable";            tn sh -c 'echo x >> "\$CLAUDE_CONFIG_DIR/settings.json"'
name="~/.claude skills are not writable";              tn sh -c 'echo x > "\$CLAUDE_CONFIG_DIR/skills/demo/new.md"'
name="reading /tmp is blocked";                        tn cat "$PROBE_TMP"
name="reading the user's temp dir is blocked";         tn cat "$PROBE_USER_TMP"
name="listing /var/log is blocked";                    tn ls /var/log
name="Homebrew data is unreadable";                    tn ls /opt/homebrew/var
name="the xcrun cache is not writable";                tn sh -c ': >> "${USER_TMP}xcrun_db"'
name="system files are readable";                      t cat /etc/hosts
name="git works";                                      t git --version
name="Claude Code's login is readable";                t grep -q FAKE-OAUTH-1 "\$CLAUDE_CONFIG_DIR/.credentials.json"
name="Claude Code's temp root is the session's";       t sh -c '[ "\$CLAUDE_CODE_TMPDIR" = "\${TMPDIR%/}" ]'
name="reading the wrapper state is blocked";           tn ls "$COPY/state"
name="writing .git/config is blocked";                 tn sh -c 'echo x >> .git/config'
name="writing .git/hooks is blocked";                  tn sh -c 'echo x > .git/hooks/post-checkout'
name="writing alternates is blocked";                  tn sh -c 'echo /x > .git/objects/info/alternates'
name="renaming .git is blocked";                       tn mv .git .git-old
name="renaming .git/objects is blocked";               tn mv .git/objects .git/objects-old
name="writing $PF is blocked";                         tn sh -c 'echo x > $PF'
name="creating a file in a protected path is blocked"; tn sh -c 'mkdir -p secret && echo x > secret/new.txt'
name="network to github.com is blocked";               tn curl -sf -m 5 -o /dev/null https://github.com
name="network to example.com is blocked";              tn curl -sf -m 5 -o /dev/null https://example.com
name="writing in the copy is allowed";                 t sh -c 'echo ok > scratch.txt && rm scratch.txt'
name="writing in TMPDIR is allowed";                   t sh -c 'echo ok > "\$TMPDIR/x" && rm "\$TMPDIR/x"'
name="TMPDIR cannot be replaced by a link";            tn sh -c 'rm -rf "$COPY/tmp" && ln -s "$T/home" "$COPY/tmp"'
name="claude-home cannot be replaced by a link";       tn sh -c 'mv "\$CLAUDE_CONFIG_DIR" "\$CLAUDE_CONFIG_DIR.old"'
name="protected files are absent from disk";           tn test -e secret/s.txt
name="hidden public file is absent from disk";         tn test -e src/dup.txt
name="public file is visible";                         t grep -q "public v2" src/a.txt
name="empty public file is visible";                   t test -e src/empty.txt
name="placeholder in git show";                        t sh -c 'git show HEAD:secret/s.txt | grep -qF "$PLACEHOLDER"'
name="placeholder in git log -p";                      t sh -c 'git log -p -- secret | grep -qF "$PLACEHOLDER"'
name="git blame on a protected file";                  t sh -c 'git blame HEAD -- secret/s.txt | grep -qF "$PLACEHOLDER"'
name="the developer's branch is checked out";         t sh -c '[ "\$(git symbolic-ref --short HEAD)" = main ]'
name="the developer's other branch is copied";        t git rev-parse --verify -q other
name="no host/* branches";                            tn sh -c 'git for-each-ref refs/remotes refs/incoming | grep -q .' 
for s in $SECRETS; do
  name="no object contains \$s"
  if GIT_NO_REPLACE_OBJECTS=1 GIT_NO_LAZY_FETCH=1 git cat-file --batch-all-objects --batch-check='%(objectname) %(objecttype)' |
       awk '\$2=="blob"{print \$1}' | while read o; do GIT_NO_LAZY_FETCH=1 git cat-file -p \$o; done | grep -q "\$s"; then
    echo "IN-FAIL \$name"; else echo "IN-OK \$name"; fi
  name="no file contains \$s"
  tn grep -rq --exclude-dir=.git "\$s" .
done

# Claude's work: a commit on the developer's branch, and branches that a
# manipulated Claude could build with git plumbing.
echo "public v3 by claude" > src/a.txt
echo "new file" > src/new.txt
git add -A && git commit -qm "claude: work on main" || echo "IN-FAIL commit on main"

# git commit refuses a missing blob: use plumbing, as a manipulated Claude would.
oid=\$(git ls-tree main secret/s.txt | awk '{print \$3}')
src_tree=\$( { git ls-tree main:src; printf '100644 blob %s\tleak.txt\n' \$oid; } | git mktree --missing)
root=\$(git ls-tree main | awk -F'\t' -v t=\$src_tree '{ if (\$2 == "src") { split(\$1, a, " "); print a[1] " " a[2] " " t "\t" \$2 } else print }' | git mktree --missing)
c=\$(git commit-tree \$root -p main -m "reference protected blob") && git branch bad-ref \$c || echo "IN-FAIL commit bad-ref"

git checkout -q -f -b feature-ok other
echo "feature" > src/feature.txt
git add -A && git commit -qm "claude: feature ok" || echo "IN-FAIL commit feature-ok"
git checkout -q -f main
echo "uncommitted" > src/wip.txt
EOF

run_claude "$T/fakebin/s1.sh"
check "claude runs inside the sandbox" grep -q '^SANDBOXED' "$T/out/last"
check "claude runs in the copy" grep -q "^SANDBOXED cwd=$COPY/work" "$T/out/last"
check "system prompt passed" grep -q -- '--append-system-prompt-file' "$T/out/last"
check "Claude Code's own sandbox disabled" grep -q -- '"sandbox": {"enabled": false}' "$T/out/last"
check "status line marks the session as sandboxed" grep -q -- '"statusLine".*sandboxed · protected repository repo · main' "$T/out/last"
check "no banner when the output is not a terminal" sh -c "! grep -q 'sandboxed session' '$T/out/last'"
collect
check "absent public file reported" sh -c "grep -q 'a protected file has the same content' '$T/out/last' && grep -q '^  src/dup.txt' '$T/out/last'"

section "end of session and git fetch"
check "end of session lists main" grep -q '  main: 1 new commit' "$T/out/last"
check "end of session lists feature-ok" grep -q '  feature-ok: 1 new commit' "$T/out/last"
check "uncommitted changes reported" grep -q 'uncommitted file' "$T/out/last"
check "remote claude points to the copy" test "$(git -C "$D" config --get remote.claude.url)" = "$COPY/work"
check_not "push to claude is impossible" git -C "$D" push -q claude main
git -C "$D" fetch -q claude 2>"$T/out/fetch-err"
check "git fetch claude" test ! -s "$T/out/fetch-err"
check "branch claude/feature-ok" git -C "$D" rev-parse -q --verify claude/feature-ok
check "branch claude/main" git -C "$D" rev-parse -q --verify claude/main
check "a commit referencing hidden content does not break the fetch" git -C "$D" rev-parse -q --verify claude/bad-ref
check "lock released" test ! -d "$COPY/state/lock"
check "sessions separate from ~/.claude" test -f "$STATE_ROOT/claude-home/settings.json"
check "settings passed through, not copied" test -L "$STATE_ROOT/claude-home/settings.json"
check "~/.claude untouched by the session" sh -c "! grep -q x '$T/home/.claude/settings.json' && test ! -e '$T/home/.claude/skills/demo/new.md'"
check "mcpServers synced" sh -c "jq -e '.mcpServers.demo' '$STATE_ROOT/claude-home/.claude.json'"
check "this repository's MCP servers synced" sh -c "jq -e --arg w '$COPY/work' '.projects[\$w].mcpServers[\"repo-mcp\"]' '$STATE_ROOT/claude-home/.claude.json'"
check_not "other projects and account not copied" sh -c "grep -q 'other-mcp\|dev@example.com\|/other/project' '$STATE_ROOT/claude-home/.claude.json'"
check "system prompt lists protected paths" sh -c "grep -qF -- '- \`secret\`' '$COPY/run/system-prompt.md' && grep -qF -- '- \`lib/deep/core\`' '$COPY/run/system-prompt.md'"
check "system prompt explains the placeholder" grep -qF "$PLACEHOLDER" "$COPY/run/system-prompt.md"

section "Claude Code's login"
CREDS=$STATE_ROOT/claude-home/.credentials.json
check "login copied from the keychain" grep -q FAKE-OAUTH-1 "$CREDS"
check "login file private" test "$(python3 -c 'import os, sys; print(oct(os.stat(sys.argv[1]).st_mode & 0o777))' "$CREDS")" = 0o600
echo '{"claudeAiOauth":{"accessToken":"REFRESHED","refreshToken":"r","expiresAt":2000}}' > "$CREDS"
run_claude "$T/fakebin/s-noop.sh" --continue
check "a login refreshed in the sandbox is kept" grep -q REFRESHED "$CREDS"
echo '{"claudeAiOauth":{"accessToken":"FAKE-OAUTH-2","refreshToken":"r","expiresAt":3000}}' > "$CS_TEST_KEYCHAIN"
run_claude "$T/fakebin/s-noop.sh" --continue
check "a newer keychain login replaces it" grep -q FAKE-OAUTH-2 "$CREDS"

section "new session: Claude's branch is ahead"
cat > "$T/fakebin/s1b.sh" <<'EOF'
t()  { if "$@" >/dev/null 2>&1; then echo "IN-OK $name"; else echo "IN-FAIL $name"; fi; }
tn() { if "$@" >/dev/null 2>&1; then echo "IN-FAIL $name"; else echo "IN-OK $name"; fi; }
name="Claude continues on its own commits";  t grep -q "public v3 by claude" src/a.txt
name="uncommitted work stashed";             t sh -c 'git stash list | grep -q claude-secure-sandbox'
name="uncommitted file removed from disk";   tn test -e src/wip.txt
echo "second change" > src/c.txt
git add src/c.txt && git commit -qm "claude: second change" || echo "IN-FAIL commit second change"
EOF
run_claude "$T/fakebin/s1b.sh"
collect
check "continuation announced" grep -q "continuing on Claude's main: 1 commit" "$T/out/last"
check_not "absent public file reported only once" grep -q 'a protected file has the same content' "$T/out/last"
check "git pull claude main" git -C "$D" pull -q --ff-only claude main
check "Claude's content merged" sh -c "grep -q 'public v3 by claude' '$D/src/a.txt' && test -f '$D/src/c.txt'"
check "protected content intact after merge" grep -q "SECRET-TWO" "$D/secret/s.txt"
check_not "Claude's commits are not signed" git -C "$D" verify-commit HEAD
check "the developer signs them with a rebase" git -C "$D" rebase -q -f HEAD~2
check "rebased commits signed" sh -c "git -C '$D' verify-commit HEAD && git -C '$D' verify-commit HEAD~1"

# --- 2. new session after developer commits ----------------------------------------

section "new session: Claude's commits integrated (rebased and signed)"
(
  cd "$D" || exit 1
  echo "SECRET-THREE" > secret/s.txt
  echo "public v4" > src/b.txt
  git add -A && git commit -qm c4
)
mkdir -p "$STATE_ROOT/claude-home/projects/-other-repo"
echo "OTHER-TRANSCRIPT" > "$STATE_ROOT/claude-home/projects/-other-repo/abc.jsonl"
cat > "$T/fakebin/s2.sh" <<'EOF'
t()  { if "$@" >/dev/null 2>&1; then echo "IN-OK $name"; else echo "IN-FAIL $name"; fi; }
tn() { if "$@" >/dev/null 2>&1; then echo "IN-FAIL $name"; else echo "IN-OK $name"; fi; }
name="other repositories' sessions are unreadable";   tn cat "$CLAUDE_CONFIG_DIR/projects/-other-repo/abc.jsonl"
name="sandbox Claude configuration is readable";      t cat "$CLAUDE_CONFIG_DIR/settings.json"
rm -f "$CLAUDE_CONFIG_DIR/.claude.json" && ln -s "$CS_TEST_SECRET_JSON" "$CLAUDE_CONFIG_DIR/.claude.json"
name="copy reset to the developer's branch";          t sh -c '[ "$(git rev-parse HEAD)" = "$(cat "$CS_TEST_EXPECTED")" ]'
name="new public file from the developer is visible"; t grep -q "public v4" src/b.txt
name="new secret is absent";                          tn sh -c 'GIT_NO_REPLACE_OBJECTS=1 GIT_NO_LAZY_FETCH=1 git cat-file --batch-all-objects --batch-check="%(objectname)" | while read o; do GIT_NO_LAZY_FETCH=1 git cat-file -p $o 2>/dev/null; done | grep -q SECRET-THREE'
name="new secret shown as placeholder";               t sh -c 'git show HEAD:secret/s.txt | grep -qF "$CS_TEST_PLACEHOLDER"'
echo "claude diverges" > src/d.txt
git add src/d.txt && git commit -qm "claude: diverging work" || echo "IN-FAIL commit diverging work"
echo "resume marker" > src/resume.txt
EOF
git -C "$D" rev-parse HEAD > "$T/fakebin/expected-head"
run_claude "$T/fakebin/s2.sh"
collect
check_not "no question when Claude's commits are integrated" grep -q "diverged" "$T/out/last"
check "TMPDIR is still a real directory" sh -c "test -d '$COPY/tmp' && test ! -L '$COPY/tmp'"

section "resume: --continue leaves the copy untouched"
cat > "$T/fakebin/s3.sh" <<'EOF'
if [ -f src/resume.txt ]; then echo "IN-OK file from the previous session kept"; else echo "IN-FAIL file from the previous session kept"; fi
EOF
run_claude "$T/fakebin/s3.sh" --continue
check "resume without reset" grep -q '^IN-OK' "$T/out/last"
check "arguments passed to claude" grep -q -- 'ARGS .* --continue' "$T/out/last"
check "planted .claude.json link replaced, not followed" sh -c "test ! -L '$STATE_ROOT/claude-home/.claude.json' && ! grep -q FAKE-JSON-SECRET '$STATE_ROOT/claude-home/.claude.json' && grep -q FAKE-JSON-SECRET '$T/secret.json'"
check "mcpServers synced again" sh -c "jq -e .mcpServers.demo '$STATE_ROOT/claude-home/.claude.json'"

section "new session: diverged branches"
( cd "$D" && echo "dev work" > src/e.txt && git add -A && git commit -qm c5 )
cat > "$T/fakebin/s-claude.sh" <<'EOF'
if [ -f src/d.txt ] && [ ! -f src/e.txt ]; then echo "IN-OK on Claude's branch"; else echo "IN-FAIL on Claude's branch"; fi
EOF
cat > "$T/fakebin/s-dev.sh" <<'EOF'
if [ -f src/e.txt ] && [ ! -f src/d.txt ]; then echo "IN-OK on the developer's branch"; else echo "IN-FAIL on the developer's branch"; fi
if git for-each-ref refs/backup | grep -q '/main$'; then echo "IN-OK Claude's branch backed up"; else echo "IN-FAIL Claude's branch backed up"; fi
EOF
run_claude "$T/fakebin/s-claude.sh"
check "divergence reported" grep -q "have diverged: 1 commit(s) of yours are not in Claude's branch, 1 of Claude's" "$T/out/last"
check "without a terminal: continue on Claude's branch" grep -q "no terminal to ask: continuing on Claude's branch" "$T/out/last"
collect
run_claude_answer 2 "$T/fakebin/s-claude.sh"
check "choice 2: quit to integrate first" grep -q "integrate Claude's commits (git fetch claude, then on main: git cherry-pick -S main..claude/main)" "$T/out/last"
check_not "choice 2: claude not started" grep -q '^SANDBOXED' "$T/out/last"
run_claude_answer 1 "$T/fakebin/s-dev.sh"
check "choice 1: backup announced" grep -q "Claude's main kept: git fetch claude refs/backup/" "$T/out/last"
check "start screen says why" grep -q "This repository is protected: it has a .claudeprotected file." "$T/out/last"
check "start screen lists what is hidden" grep -q "Hidden from Claude:  lib/deep/core, secret" "$T/out/last"
if [ "$OS" = Darwin ]; then
  ( cd "$D" && sleep 3 | CS_TEST_SCENARIO=$T/fakebin/s-dev.sh script -q /dev/null claude ) > "$T/out/cancel" 2>&1
else
  ( cd "$D" && sleep 3 | CS_TEST_SCENARIO=$T/fakebin/s-dev.sh script -q -e -c claude /dev/null ) > "$T/out/cancel" 2>&1
fi
check "no Enter: Claude not started" sh -c "grep -q cancelled '$T/out/cancel' && ! grep -q '^SANDBOXED' '$T/out/cancel'"
collect
ref=$(sed -n "s/.*git fetch claude \(refs\/backup\/[^ ]*\).*/\1/p" "$T/out/last" | tr -d '\r' | head -1)
check "choice 1: Claude's commit retrievable" sh -c "git -C '$D' fetch -q claude '$ref' && git -C '$D' cat-file -e FETCH_HEAD:src/d.txt"

section "other branches follow the developer's"
cat > "$T/fakebin/s-other.sh" <<'EOF'
git checkout -q other && echo "on other" > src/o.txt && git add src/o.txt &&
  git commit -qm "claude: on other" && git checkout -q main || echo "IN-FAIL commit on other"
EOF
run_claude "$T/fakebin/s-other.sh"
collect
run_claude "$T/fakebin/s-noop.sh"
check "branch with Claude's work kept" grep -q "Claude's other has commits you have not integrated: left as is" "$T/out/last"
check "the developer fetches it" git -C "$D" fetch -q claude other:other
run_claude "$T/fakebin/s-noop.sh"
check_not "integrated branch updated without a message" grep -q "Claude's other" "$T/out/last"
git -C "$D" branch -q -D other
cat > "$T/fakebin/s-deleted.sh" <<'EOF'
if git rev-parse --verify -q other >/dev/null; then echo "IN-FAIL branch deleted by the developer removed"; else echo "IN-OK branch deleted by the developer removed"; fi
if git rev-parse --verify -q feature-ok >/dev/null; then echo "IN-OK Claude's own branch kept"; else echo "IN-FAIL Claude's own branch kept"; fi
EOF
run_claude "$T/fakebin/s-deleted.sh"
collect

# --- 3. tampering with the copy's .git ------------------------------------------------

section "tampering: link in the object store"
cat > "$T/fakebin/s4.sh" <<'EOF'
git checkout -q -b tampered HEAD
echo "x" > src/t.txt && git add src/t.txt && git commit -qm tampered
ln -s /etc/hosts .git/objects/pack/pack-evil.pack
EOF
run_claude "$T/fakebin/s4.sh"
check "tampering reported at the end of the session" grep -q 'tampered with during the session' "$T/out/last"
run_claude "$T/fakebin/s3.sh"
check "copy rebuilt at the next launch" grep -q 'tampered with: rebuilding' "$T/out/last"
check "previous copy archived" sh -c "ls '$COPY/archive' | grep -q integrity"
check "archived branch announced" grep -q "Claude's tampered is still there: git fetch .*/work tampered" "$T/out/last"
check "git fetch claude after a rebuild" git -C "$D" fetch -q claude

# --- 4. protection file change ------------------------------------------------------

section "protection file change"
(
  cd "$D" || exit 1
  mkdir -p src/private && echo "PRIVATE-NEW" > src/private/p.txt
  git add -A && git commit -qm "private code"
)
cat > "$T/fakebin/s5.sh" <<'EOF'
if grep -rq PRIVATE-NEW src 2>/dev/null; then echo "IN-OK private visible before protection"; else echo "IN-FAIL private visible before protection"; fi
EOF
run_claude "$T/fakebin/s5.sh"
check "file is public before protection" grep -q '^IN-OK' "$T/out/last"
(
  cd "$D" || exit 1
  printf 'src/private\n' >> "$PF"
  git commit -qam "protect src/private"
)
cat > "$T/fakebin/s6.sh" <<'EOF'
if GIT_NO_REPLACE_OBJECTS=1 GIT_NO_LAZY_FETCH=1 git cat-file --batch-all-objects --batch-check='%(objectname)' |
   while read o; do GIT_NO_LAZY_FETCH=1 git cat-file -p $o 2>/dev/null; done | grep -q PRIVATE-NEW; then
  echo "IN-FAIL newly protected path removed from history"
else
  echo "IN-OK newly protected path removed from history"
fi
EOF
run_claude "$T/fakebin/s6.sh"
check "rebuild announced" grep -q "$PF changed" "$T/out/last"
check "newly protected path removed from history" grep -q '^IN-OK' "$T/out/last"
check "archive created" sh -c "ls '$COPY/archive' | grep -q protect-file"

section "protection removed by one of Claude's commits"
cat > "$T/fakebin/s7.sh" <<EOF
blob=\$(printf 'lib/deep/core\nsrc/private\n' | git hash-object -w --stdin)
git update-index --cacheinfo 100644,\$blob,$PF
git commit -qm "refactor" || echo "IN-FAIL commit dropping secret"
EOF
run_claude "$T/fakebin/s7.sh"
collect
git -C "$D" pull -q --ff-only claude main
check "the developer merged the change" sh -c "! grep -q secret '$D/$PF'"
cat > "$T/fakebin/s-hidden.sh" <<'EOF'
if [ -e secret/s.txt ]; then echo "IN-FAIL secret still hidden"; else echo "IN-OK secret still hidden"; fi
EOF
cat > "$T/fakebin/s-visible.sh" <<'EOF'
if grep -q SECRET-THREE secret/s.txt 2>/dev/null; then echo "IN-OK unprotected path visible"; else echo "IN-FAIL unprotected path visible"; fi
EOF
run_claude "$T/fakebin/s-hidden.sh"
check "removal detected" grep -q "no longer listed in $PF" "$T/out/last"
check "without a terminal: kept protected" grep -q "no terminal to confirm: they stay protected" "$T/out/last"
collect
run_claude_answer n "$T/fakebin/s-hidden.sh"
check "answer no: kept protected" grep -q "they stay protected; restore them" "$T/out/last"
collect
run_claude_answer y "$T/fakebin/s-visible.sh"
check "answer yes: protection removed" grep -q "protection removed" "$T/out/last"
collect
check "confirmation stored in the developer's .git" grep -qx secret "$D/.git/claude-secure-sandbox/unprotected"
run_claude "$T/fakebin/s-visible.sh"
check_not "no question once confirmed" grep -q "no longer listed" "$T/out/last"
( cd "$D" && printf 'secret\n' >> "$PF" && git commit -qam "protect secret again" )
run_claude "$T/fakebin/s-hidden.sh"
collect
check "confirmation dropped when protected again" sh -c "! grep -qx secret '$D/.git/claude-secure-sandbox/unprotected'"

# --- escape attempts ------------------------------------------------------------------

section "escape attempts: getting a program outside the sandbox to run something"
SOCK=/tmp/cs-sock.$$
python3 -c 'import socket, sys, time
s = socket.socket(socket.AF_UNIX); s.bind(sys.argv[1]); s.listen(1); time.sleep(60)' "$SOCK" &
sock_pid=$!
PORT=$(python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')
python3 -m http.server "$PORT" --bind 127.0.0.1 >/dev/null 2>&1 &
http_pid=$!
CRON_BEFORE=$(crontab -l 2>/dev/null)
sleep 1
cat > "$T/fakebin/s-escape.sh" <<EOF
t()  { if "\$@" >/dev/null 2>&1; then echo "IN-OK \$name"; else echo "IN-FAIL \$name"; fi; }
tn() { if "\$@" >/dev/null 2>&1; then echo "IN-FAIL \$name"; else echo "IN-OK \$name"; fi; }
name="crontab is blocked";                  tn sh -c '(crontab -l 2>/dev/null; echo "# cs-test-escape") | crontab -'
name="at is blocked";                       tn sh -c 'echo true | at now + 30 minutes'
if [ "\$(uname)" = Darwin ]; then
name="launchctl submit is blocked";         tn launchctl submit -l cs-test-escape -- /usr/bin/true
name="open is blocked";                     tn open -g -j -a Calculator
name="Apple Events (osascript) are blocked"; tn osascript -e 'with timeout of 5 seconds' -e 'tell application "Finder" to count windows' -e 'end timeout'
name="the clipboard is blocked";            tn sh -c 'echo cs-test-escape | pbcopy'
else
name="systemd-run is blocked";              tn systemd-run --user --quiet /bin/true
name="xdg-open is blocked";                 tn xdg-open https://example.com
name="the D-Bus session bus is blocked";    tn sh -c 'test -n "\$DBUS_SESSION_BUS_ADDRESS" && dbus-send --session --print-reply --dest=org.freedesktop.DBus /org/freedesktop/DBus org.freedesktop.DBus.ListNames'
fi
name="Unix sockets are blocked";            tn python3 -c 'import socket, sys; s = socket.socket(socket.AF_UNIX); s.connect(sys.argv[1])' "$SOCK"
name="localhost TCP is blocked";            tn curl -s -m 3 -o /dev/null "http://127.0.0.1:$PORT/"
# On Linux a read-denied directory is an empty tmpfs: writes there succeed
# but vanish. What matters is the real file, checked after the session.
sh -c 'echo x >> "\$HOME/.zshrc"' 2>/dev/null
sh -c 'mkdir -p "\$HOME/Library/LaunchAgents" && echo x > "\$HOME/Library/LaunchAgents/cs-test.plist"' 2>/dev/null
sh -c 'mkdir -p "\$HOME/.config/autostart" && echo x > "\$HOME/.config/autostart/cs-test.desktop"' 2>/dev/null
# A detached job outlives the session: it must stay confined.
nohup sh -c 'sleep 4; { cat "$T/home/.ssh/id_test" >/dev/null 2>&1 && echo read-home; curl -s -m 3 -o /dev/null https://api.anthropic.com && echo network; echo done; } > "\$TMPDIR/survivor"' >/dev/null 2>&1 &
EOF
run_claude "$T/fakebin/s-escape.sh"
collect
kill $sock_pid $http_pid 2>/dev/null; wait $sock_pid $http_pid 2>/dev/null
if [ "$OS" = Darwin ]; then
  check_not "no launchd job created" sh -c "launchctl list | grep -q cs-test-escape"
  launchctl remove cs-test-escape 2>/dev/null
  check_not "Calculator not opened" pgrep -x Calculator && osascript -e 'quit app "Calculator"' 2>/dev/null
fi
check_not "~/.zshrc not written" test -e "$T/home/.zshrc"
check_not "no LaunchAgent written" test -e "$T/home/Library/LaunchAgents/cs-test.plist"
check_not "no autostart entry written" test -e "$T/home/.config/autostart/cs-test.desktop"
if [ "$(crontab -l 2>/dev/null)" != "$CRON_BEFORE" ]; then
  fail "crontab unchanged"; printf '%s\n' "$CRON_BEFORE" | crontab -
else ok "crontab unchanged"; fi
sleep 6
# macOS: the job outlives the session, confined. Linux: bubblewrap ends it with the session.
if [ "$OS" = Darwin ]; then check "the detached job outlived the session" grep -q done "$COPY/tmp/survivor"; fi
check_not "after the session it still cannot read the home" grep -q read-home "$COPY/tmp/survivor"
check_not "after the session it has no network" grep -q network "$COPY/tmp/survivor"
rm -f "$COPY/tmp/survivor" "$SOCK"

# --- 5. launch cases ---------------------------------------------------------------

section "launch cases"
mkdir -p "$T/plain/sub/protected"
git init -q "$T/plain/sub/protected"
echo "x" > "$T/plain/sub/protected/$PF"
out=$(cd "$T/plain" && claude 2>&1)
check "refused when a protection file is further down" sh -c "printf '%s' '$out' | grep -q 'protected code in $T/plain/sub/protected: run claude from that directory'"

section "directory mode: no .claudeprotected, only the directory is readable"
F=$T/plain/free
mkdir -p "$F/sub" && git init -q "$F" && echo "free readme" > "$F/README" && echo "sub file" > "$F/sub/s.txt"
git -C "$F" add -A && git -C "$F" commit -qm init
mkdir -p "$T/plain/nogit" && echo "loose" > "$T/plain/nogit/loose.txt"
cat > "$T/fakebin/s-dir.sh" <<EOF
t()  { if "\$@" >/dev/null 2>&1; then echo "IN-OK \$name"; else echo "IN-FAIL \$name"; fi; }
tn() { if "\$@" >/dev/null 2>&1; then echo "IN-FAIL \$name"; else echo "IN-OK \$name"; fi; }
name="directory mode: the repository is readable";        t cat "$F/README"
name="directory mode: writing in the repository";          t sh -c 'echo new > "$F/new.txt"'
name="directory mode: git commit";                         t sh -c 'cd "$F" && git add new.txt && git commit -qm "by claude"'
name="directory mode: the protected repository is unreadable"; tn cat "$D/src/a.txt"
name="directory mode: ~/.ssh is unreadable";               tn cat "$T/home/.ssh/id_test"
name="directory mode: .git/hooks is not writable";         tn sh -c 'echo x > "$F/.git/hooks/pre-commit"'
name="directory mode: .git/config is not writable";        tn sh -c 'echo x >> "$F/.git/config"'
EOF
out=$(cd "$F/sub" && CS_TEST_SCENARIO=$T/fakebin/s-dir.sh claude --foo 2>&1 < /dev/null)
printf '%s\n' "$out" > "$T/out/last"
check "repository without protection file: sandboxed" grep -q "^SANDBOXED cwd=$F/sub" "$T/out/last"
check "arguments passed" grep -q -- 'ARGS .*--foo' "$T/out/last"
collect
check "Claude's commit is in the repository" sh -c "git -C '$F' log -1 --format=%s | grep -q 'by claude'"
cat > "$T/fakebin/s-nogit.sh" <<'EOF'
if cat loose.txt >/dev/null 2>&1; then echo "IN-OK outside a repository: the directory is readable"; else echo "IN-FAIL outside a repository: the directory is readable"; fi
EOF
( cd "$T/plain/nogit" && CS_TEST_SCENARIO=$T/fakebin/s-nogit.sh claude ) < /dev/null > "$T/out/last" 2>&1
check "directory without a repository: sandboxed" grep -q "^SANDBOXED cwd=$T/plain/nogit" "$T/out/last"
collect

section "home directory without protected code: readable, not writable"
HOME_RUN=$STATE_ROOT/home-$(python3 -c 'import hashlib, sys; print(hashlib.sha1(sys.argv[1].encode()).hexdigest()[:10])' "$T/home")/run
cat > "$T/fakebin/s-home.sh" <<EOF
t()  { if "\$@" >/dev/null 2>&1; then echo "IN-OK \$name"; else echo "IN-FAIL \$name"; fi; }
tn() { if "\$@" >/dev/null 2>&1; then echo "IN-FAIL \$name"; else echo "IN-OK \$name"; fi; }
name="home: the whole home directory is readable";        t cat "$T/home/.ssh/id_test"
name="home: old Claude transcripts stay unreadable";       tn cat "$T/home/.claude/projects/-old-session/t.jsonl"
name="home: other repositories' copies stay unreadable";   tn ls "$COPY/state"
name="home: Claude's configuration is readable";           t ls "\$CLAUDE_CONFIG_DIR"
name="home: the home directory can be listed";             t sh -c 'ls -a "$T/home" | grep -q "^.ssh\$"'
name="home: the system prompt is readable";                t cat "$HOME_RUN/system-prompt.md"
name="home: the session temp directory is readable";       t ls "\$TMPDIR"
name="home: a protected repository elsewhere is unreadable"; tn cat "$D/src/a.txt"
name="home: writing in the temp directory works";          t sh -c 'echo x > "\$TMPDIR/home-test"'
sh -c 'echo x > "$T/home/written-by-claude"' 2>/dev/null
EOF
( cd "$T/home" && CS_TEST_SCENARIO=$T/fakebin/s-home.sh claude ) < /dev/null > "$T/out/last" 2>&1
check "home: sandboxed in the home directory" grep -q "^SANDBOXED cwd=$T/home" "$T/out/last"
collect
check "home: nothing written in the home directory" test ! -e "$T/home/written-by-claude"
check "home: status line says read only" grep -q -- '(read only)' "$T/out/last"
( cd / && claude ) < /dev/null > "$T/out/root" 2>&1
check "from /, a protected repository below is found and refused" grep -q "protected code in .*: run claude from that directory" "$T/out/root"
out=$(cd "$D" && claude --version 2>&1)
check "--version without sandbox" sh -c "printf '%s' '$out' | grep -q '^UNSANDBOXED --version'"
out=$(cd "$D/secret" && claude 2>&1)
check "refused from a protected directory" sh -c "printf '%s' '$out' | grep -q 'is protected by'"
mkdir -p "$T/plain/badrepo" && git init -q "$T/plain/badrepo"
printf '/etc/passwd\n' > "$T/plain/badrepo/$PF"
out=$(cd "$T/plain/badrepo" && claude 2>&1)
check "absolute path refused in the protection file" sh -c "printf '%s' '$out' | grep -q 'absolute path not allowed'"
printf '../outside\n' > "$T/plain/badrepo/$PF"
out=$(cd "$T/plain/badrepo" && claude 2>&1)
check ".. refused in the protection file" sh -c "printf '%s' '$out' | grep -q 'outside the repository root'"
mkdir -p "$COPY/state/lock"
sleep 30 & sleeper=$!
echo $sleeper > "$COPY/state/lock/pid"
out=$(cd "$D" && claude 2>&1)
check "one session per repository" sh -c "printf '%s' '$out' | grep -q 'already running'"
kill $sleeper 2>/dev/null; wait $sleeper 2>/dev/null
rm -rf "$COPY/state/lock"
cat > "$T/config.json" <<EOF
{ "claude_bin": "$T/fakebin/claude", "network": { "allowed_domains": ["github.com"] } }
EOF
out=$(cd "$D" && claude 2>&1)
check "git host refused in the configuration" sh -c "printf '%s' '$out' | grep -q 'git host not allowed'"
check_not "claude not started when the configuration is refused" sh -c "printf '%s' '$out' | grep -q '^SANDBOXED'"
cat > "$T/config.json" <<EOF
{ "claude_bin": "$T/fakebin/claude", "filesystem": { "allow_read": ["~/.ssh/known_hosts"] } }
EOF
out=$(cd "$D" && claude 2>&1)
check "allow_read inside ~/.ssh refused" sh -c "printf '%s' '$out' | grep -q 'conflicts with $T/home/.ssh'"
check_not "claude not started (allow_read refused)" sh -c "printf '%s' '$out' | grep -q '^SANDBOXED'"

# --- uninstall ----------------------------------------------------------------------

section "uninstall"
echo "{ \"claude_bin\": \"$T/fakebin/claude\" }" > "$T/config.json"
cat > "$T/fakebin/s-unfetched.sh" <<'EOF'
git checkout -q -b never-fetched && echo z > src/z.txt && git add src/z.txt && git commit -qm "never fetched"
EOF
run_claude "$T/fakebin/s-unfetched.sh"
printf 'alias ll="ls -l"\nexport PATH="%s/bin:$PATH"\n' "$ROOT" > "$T/home/.zshrc"
cp "$T/home/.zshrc" "$T/zshrc-before"
"$ROOT/uninstall.sh" > "$T/out/last" 2>&1
check "startup file not changed" cmp -s "$T/home/.zshrc" "$T/zshrc-before"
check "PATH line shown" grep -q "$T/home/.zshrc, line 2: export PATH" "$T/out/last"
check "unfetched branch reported" grep -q "never-fetched was never fetched" "$T/out/last"
check "remote kept, removal command shown" sh -c "git -C '$D' config --get remote.claude.url && grep -q \"remote remove claude\" '$T/out/last'"
check "state kept, removal command shown" sh -c "test -d '$STATE_ROOT' && grep -q \"rm -rf '$STATE_ROOT'\" '$T/out/last'"
check "configuration kept" test -f "$CLAUDE_SECURE_SANDBOX_CONFIG"

# --- summary -----------------------------------------------------------------------

printf '\n%d passed, %d failed\n' "$PASS" "$FAILED"
if [ "$FAILED" -gt 0 ]; then
  printf 'Failures:%s\n' "$FAIL_NAMES"
  printf 'Test directory kept: %s\n' "$T"
  exit 1
fi
[ -n "${CS_TEST_DIR:-}" ] || rm -rf "$T"
