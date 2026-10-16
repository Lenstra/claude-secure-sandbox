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

Optional configuration: ~/.config/$NAME/config.json
Example: $ROOT/share/config.example.json
MSG
if [ -z "$real" ]; then
  echo
  echo "Warning: Claude Code was not found on your PATH. Install it before using $NAME."
fi
