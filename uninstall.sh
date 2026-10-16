#!/bin/bash
# Removes what claude-secure-sandbox created outside its own directory: the
# "claude" remote of each repository, the wrapper's state and configuration.
# Asks before deleting anything; -y answers yes to every question.
set -eu
ROOT=$(cd -P "$(dirname "$0")" && pwd)
NAME=claude-secure-sandbox
YES=0
[ "${1:-}" = "-y" ] && YES=1

ask() {
  [ "$YES" = 1 ] && return 0
  printf '%s [y/N] ' "$1"
  read -r answer || return 1
  [ "$answer" = y ] || [ "$answer" = yes ]
}

CONFIG=${CLAUDE_SECURE_SANDBOX_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/$NAME/config.json}
STATE=${CLAUDE_SECURE_SANDBOX_HOME:-}
if [ -z "$STATE" ] && [ -f "$CONFIG" ]; then
  STATE=$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1])).get("state_dir") or "")' "$CONFIG" 2>/dev/null || true)
fi
STATE=${STATE:-$HOME/.cache/$NAME}
STATE=${STATE/#\~/$HOME}

# Git on Claude's copies: never their configuration or hooks.
copy_git() { GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_NO_LAZY_FETCH=1 git --git-dir="$1" "${@:2}"; }

# --- 1. repositories ---------------------------------------------------------------

unfetched=0
for marker in "$STATE"/*/state/repository; do
  [ -f "$marker" ] || continue
  repo=$(cat "$marker")
  copy=$(dirname "$(dirname "$marker")")/work/.git
  [ -d "$repo/.git" ] || [ -f "$repo/.git" ] || continue
  if [ -d "$copy" ]; then
    while read -r tip branch; do
      [ -n "$tip" ] || continue
      if ! git -C "$repo" cat-file -e "$tip^{commit}" 2>/dev/null; then
        echo "$NAME: $repo: Claude's branch $branch was never fetched (git fetch claude)"
        unfetched=1
      fi
    done < <(copy_git "$copy" for-each-ref --format='%(objectname) %(refname:short)' refs/heads 2>/dev/null)
  fi
done
if [ "$unfetched" = 1 ] && ! ask "Claude's unfetched work will be lost. Continue?"; then
  echo "$NAME: nothing removed"; exit 1
fi

for marker in "$STATE"/*/state/repository; do
  [ -f "$marker" ] || continue
  repo=$(cat "$marker")
  git -C "$repo" rev-parse --git-dir >/dev/null 2>&1 || continue
  url=$(git -C "$repo" config --get remote.claude.url 2>/dev/null || true)
  case "$url" in
    "$STATE"/*) git -C "$repo" remote remove claude && echo "$NAME: $repo: remote \"claude\" removed" ;;
  esac
  common=$(git -C "$repo" rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)
  if [ -n "$common" ] && [ -d "$common/$NAME" ]; then
    rm -rf "$common/$NAME" && echo "$NAME: $repo: confirmed removals of protection deleted"
  fi
done

# --- 2. state and configuration ----------------------------------------------------------

if [ -d "$STATE" ]; then
  echo "$NAME: $STATE holds Claude's copies, archives, sessions and its copy of your Claude Code login"
  if ask "Delete $STATE?"; then rm -rf "$STATE" && echo "$NAME: $STATE deleted"; fi
fi
if [ -f "$CONFIG" ] && ask "Delete the configuration $CONFIG?"; then
  rm -f "$CONFIG"
  rmdir "$(dirname "$CONFIG")" 2>/dev/null || true
  echo "$NAME: $CONFIG deleted"
fi

# --- 3. what is left to the developer ---------------------------------------------------

for rc in "$HOME/.zshrc" "$HOME/.bashrc" "$HOME/.bash_profile" "$HOME/.profile"; do
  [ -f "$rc" ] || continue
  grep -n -F "$ROOT/bin" "$rc" 2>/dev/null | while IFS=: read -r line _; do
    echo "$NAME: remove line $line of $rc, which puts the wrapper on the PATH"
  done
done
cat <<MSG
$NAME: uninstalled. Claude Code itself is untouched; once the PATH line is
removed, claude runs it directly. Delete $ROOT to remove the tool.
MSG
