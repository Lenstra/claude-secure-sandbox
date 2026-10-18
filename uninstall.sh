#!/bin/bash
# Lists what to remove to uninstall claude-secure-sandbox, with the commands.
# It changes nothing itself.
set -u
ROOT=$(cd -P "$(dirname "$0")" && pwd)
NAME=claude-secure-sandbox

STATE=${CLAUDE_SECURE_SANDBOX_HOME:-$HOME/.cache/$NAME}
STATE=${STATE/#\~/$HOME}

echo "$NAME: nothing has been removed. To uninstall, remove these yourself:"
step=0
title() { step=$((step + 1)); echo; echo "$step. $1"; }

# The PATH line, in any of its usual spellings.
title "The line that puts the wrapper on your PATH, then open a new shell: claude runs Claude Code directly."
spellings=("$ROOT/bin")
case "$ROOT" in "$HOME"/*) rest=${ROOT#"$HOME"}; spellings+=("\$HOME$rest/bin" "\${HOME}$rest/bin" "~$rest/bin") ;; esac
found=0
zdot=${ZDOTDIR:-$HOME}
seen=
for rc in "$zdot/.zshrc" "$zdot/.zprofile" "$zdot/.zshenv" "$HOME/.config/zsh/.zshrc" "$HOME/.config/zsh/.zprofile" \
          "$HOME/.bashrc" "$HOME/.bash_profile" "$HOME/.profile"; do
  [ -f "$rc" ] || continue
  case " $seen " in *" $rc "*) continue ;; esac
  seen="$seen $rc"
  for s in "${spellings[@]}"; do
    while IFS=: read -r n text; do
      echo "   $rc, line $n: $text"; found=1
    done < <(grep -n -F -- "$s" "$rc" 2>/dev/null)
  done
done
[ "$found" = 1 ] || echo "   Not found in your shell startup files: remove $ROOT/bin from your PATH where you added it."

# Each repository the wrapper was used in.
repos=""
for marker in "$STATE"/*/state/repository; do
  [ -f "$marker" ] || continue
  repo=$(cat "$marker")
  git -C "$repo" rev-parse --git-dir >/dev/null 2>&1 || continue
  lines=""
  copy=$(dirname "$(dirname "$marker")")/work/.git
  if [ -d "$copy" ]; then
    # Never the copy's configuration or hooks.
    while read -r tip branch; do
      [ -n "$tip" ] || continue
      git -C "$repo" cat-file -e "$tip^{commit}" 2>/dev/null ||
        lines+="   Claude's branch $branch was never fetched; to keep it: git -C '$repo' fetch claude"$'\n'
    done < <(GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_NO_LAZY_FETCH=1 \
               git --git-dir="$copy" for-each-ref --format='%(objectname) %(refname:short)' refs/heads 2>/dev/null)
  fi
  git -C "$repo" config --get remote.claude.url >/dev/null 2>&1 &&
    lines+="   git -C '$repo' remote remove claude"$'\n'
  common=$(git -C "$repo" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)
  [ -n "$common" ] && [ -d "$common/$NAME" ] &&
    lines+="   rm -rf '$common/$NAME'      # your confirmed removals of protection"$'\n'
  repos+="$lines"
done
if [ -n "$repos" ]; then
  title "In each repository you used it in: the \"claude\" remote, and your confirmed removals of protection."
  printf '%s' "$repos"
fi

# State, the tool itself.
if [ -d "$STATE" ]; then
  title "The wrapper's state: Claude's copies, archives and sessions, the access you confirmed, and its copy of your Claude Code login."
  echo "   rm -rf '$STATE'"
fi
title "The tool itself."
echo "   rm -rf '$ROOT'"
echo
echo "Claude Code itself is not affected."
