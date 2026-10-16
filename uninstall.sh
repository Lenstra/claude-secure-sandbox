#!/bin/bash
# Takes the claude-secure-sandbox wrapper off the PATH: removes the line that
# puts its bin directory there from the shell startup files (a backup of each
# changed file is kept). Repositories, the wrapper's state and its
# configuration are left as they are; the script prints the commands to
# remove them.
set -eu
ROOT=$(cd -P "$(dirname "$0")" && pwd)
NAME=claude-secure-sandbox

# --- the wrapper's commands: the PATH line --------------------------------------------

python3 - "$ROOT" "$NAME" <<'PY'
import os, sys, time
root, name = sys.argv[1], sys.argv[2]
home = os.environ["HOME"]
spellings = [root + "/bin"]
if root.startswith(home + "/"):
    rest = root[len(home):]
    spellings += ["$HOME" + rest + "/bin", "${HOME}" + rest + "/bin", "~" + rest + "/bin"]
found = False
for rc in (".zshrc", ".bashrc", ".bash_profile", ".profile", ".zprofile"):
    path = os.path.join(home, rc)
    if not os.path.isfile(path) or os.path.islink(path):
        continue
    with open(path, encoding="utf-8", errors="surrogateescape") as f:
        lines = f.readlines()
    kept = [l for l in lines if not any(s in l for s in spellings)]
    if len(kept) == len(lines):
        continue
    backup = f"{path}.{name}-{time.strftime('%Y%m%d%H%M%S')}.bak"
    with open(backup, "w", encoding="utf-8", errors="surrogateescape") as f:
        f.writelines(lines)
    with open(path, "w", encoding="utf-8", errors="surrogateescape") as f:
        f.writelines(kept)
    for l in lines:
        if l not in kept:
            print(f"{name}: removed from {path}: {l.rstrip()}")
    print(f"{name}: backup in {backup}")
    found = True
if not found:
    print(f"{name}: no line puts {root}/bin on the PATH in your shell startup files; remove it where you added it")
PY

# --- what is left to the developer -------------------------------------------------------

CONFIG=${CLAUDE_SECURE_SANDBOX_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/$NAME/config.json}
STATE=${CLAUDE_SECURE_SANDBOX_HOME:-}
if [ -z "$STATE" ] && [ -f "$CONFIG" ]; then
  STATE=$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1])).get("state_dir") or "")' "$CONFIG" 2>/dev/null || true)
fi
STATE=${STATE:-$HOME/.cache/$NAME}
STATE=${STATE/#\~/$HOME}

echo
echo "$NAME: open a new shell: claude now runs Claude Code directly."
echo "$NAME: nothing else was removed. To clean up yourself:"
for marker in "$STATE"/*/state/repository; do
  [ -f "$marker" ] || continue
  repo=$(cat "$marker")
  git -C "$repo" rev-parse --git-dir >/dev/null 2>&1 || continue
  copy=$(dirname "$(dirname "$marker")")/work/.git
  if [ -d "$copy" ]; then
    # Never the copy's configuration or hooks.
    GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_NO_LAZY_FETCH=1 \
      git --git-dir="$copy" for-each-ref --format='%(objectname) %(refname:short)' refs/heads 2>/dev/null |
    while read -r tip branch; do
      git -C "$repo" cat-file -e "$tip^{commit}" 2>/dev/null ||
        echo "  $repo: Claude's branch $branch was never fetched; fetch it first (git fetch claude) if you want to keep it"
    done
  fi
  if git -C "$repo" config --get remote.claude.url >/dev/null 2>&1; then
    echo "  git -C '$repo' remote remove claude"
  fi
  common=$(git -C "$repo" rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)
  [ -n "$common" ] && [ -d "$common/$NAME" ] && echo "  rm -rf '$common/$NAME'"
done
[ -d "$STATE" ] && echo "  rm -rf '$STATE'      # Claude's copies, archives, sessions, its copy of your Claude Code login"
[ -f "$CONFIG" ] && echo "  rm -f '$CONFIG'"
echo "  rm -rf '$ROOT'"
