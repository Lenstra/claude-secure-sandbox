#!/bin/bash
# Installs the dependencies of claude-secure-sandbox and prints the PATH line to add.
set -eu
ROOT=$(cd -P "$(dirname "$0")" && pwd)
NAME=claude-secure-sandbox

case "$(uname -s)" in
  Darwin) tools="git python3 rg node npm"; hint="brew install ripgrep node" ;;
  Linux)  tools="git python3 rg node npm bwrap socat"
          hint="apt-get install git python3 ripgrep nodejs npm bubblewrap socat, or the equivalent for your distribution" ;;
  *) echo "$NAME: only macOS and Linux are supported" >&2; exit 1 ;;
esac
missing=
for tool in $tools; do
  command -v "$tool" >/dev/null 2>&1 || missing="$missing $tool"
done
[ -z "$missing" ] || { echo "$NAME: missing tools:$missing ($hint)" >&2; exit 1; }
python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' \
  || { echo "$NAME: Python 3.9 or later is required" >&2; exit 1; }
command -v fd >/dev/null 2>&1 || command -v fdfind >/dev/null 2>&1 \
  || echo "$NAME: fd not found, protection file detection will be slower" >&2

# The directory is the tool: the PATH line points to it. A temporary or synced
# place could lose it, and claude would then run Claude Code without the sandbox.
risky() {
  case "$1" in
    "$HOME/Downloads"*|"$HOME/Desktop"*|"$HOME/Documents"*|"$HOME/Library/Mobile Documents"*|\
    /tmp/*|/private/tmp/*|/var/folders/*|/private/var/folders/*) return 0 ;;
  esac
  return 1
}
moved=0
if risky "$ROOT"; then
  moved=1
  if [ -t 0 ] && [ -z "${CLAUDE_SECURE_SANDBOX_MOVED:-}" ]; then
    default="$HOME/.local/share/$NAME"
    cat <<MSG
The tool is in a place that may be temporary, synced or cleaned up:

    $ROOT

This directory is the tool itself: if it is moved or deleted later, claude
silently runs Claude Code without the sandbox. Move it now?

  1) $default (recommended)
  2) another directory
  3) keep it here

MSG
    printf 'Choice [1/2/3]: '
    read -r choice || choice=3
    dest=
    case "$choice" in
      1) dest=$default ;;
      2) printf 'Directory: '; read -r dest || dest=; dest=${dest/#\~/$HOME} ;;
    esac
    if [ -n "$dest" ]; then
      case "$dest" in /*) ;; *) dest="$PWD/$dest" ;; esac
      if [ -e "$dest" ]; then
        echo "$NAME: $dest already exists; nothing moved" >&2; exit 1
      fi
      mkdir -p "$(dirname "$dest")"
      mv "$ROOT" "$dest"
      echo "$NAME: moved to $dest (cd $dest in this terminal)"
      echo
      exec env CLAUDE_SECURE_SANDBOX_MOVED=1 "$dest/install.sh"
    fi
  fi
fi

(cd "$ROOT" && npm ci --silent)
chmod +x "$ROOT/bin/claude" "$ROOT/bin/claude-unsandboxed" "$ROOT/libexec/git-remote-csnull"

real=$(command -v claude 2>/dev/null || true)

case "$(basename "${SHELL:-}")" in
  zsh) rc="~/.zshrc" ;;
  bash) [ "$(uname -s)" = Darwin ] && rc="~/.bash_profile" || rc="~/.bashrc" ;;
  *) rc="your shell startup file" ;;
esac

cat <<MSG

$NAME is installed in $ROOT

Next steps:

  1. Put the wrapper first on your PATH.
     $ROOT/bin holds its two commands, claude and claude-unsandboxed.
     Add this line at the end of $rc:

         export PATH="$ROOT/bin:\$PATH"

  2. Open a new shell and check that both commands come from the wrapper:

         command -v claude               # $ROOT/bin/claude
         command -v claude-unsandboxed   # $ROOT/bin/claude-unsandboxed

  3. Log in to Claude Code if you have not yet: claude-unsandboxed, then /login.

Optional: to allow more network domains or readable directories for your
tools, create the wrapper's configuration from the example:

    mkdir -p ~/.config/$NAME
    cp $ROOT/share/config.example.json ~/.config/$NAME/config.json
MSG
if [ "$moved" = 1 ]; then
  echo
  echo "Warning: the tool is in a place that may be temporary, synced or cleaned up:"
  echo
  echo "    $ROOT"
  echo
  echo "This directory is the tool itself. Move it to a permanent place, such as"
  echo "~/.local/share/$NAME, then run install.sh again from there: if it is"
  echo "moved or deleted later, claude silently runs Claude Code without the sandbox."
fi
if [ -z "$real" ]; then
  echo
  echo "Warning: Claude Code was not found on your PATH. Install it before using $NAME."
fi
