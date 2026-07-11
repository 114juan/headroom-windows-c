#!/usr/bin/env bash
# headroom installer: symlink bin/headroom onto the PATH. No pip, no sudo
# needed when ~/.local/bin exists (it's on PATH by default on modern systems).
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v python3 >/dev/null 2>&1; then
  echo "headroom needs python3 (3.9+) on PATH" >&2
  exit 1
fi
python3 - <<'EOF'
import sys
if sys.version_info < (3, 9):
    raise SystemExit(f"headroom needs Python 3.9+, found {sys.version.split()[0]}")
EOF

TARGET_DIR="${HEADROOM_BIN_DIR:-$HOME/.local/bin}"
mkdir -p "$TARGET_DIR"
ln -sf "$REPO/bin/headroom" "$TARGET_DIR/headroom"
chmod +x "$REPO/bin/headroom"

echo "installed: $TARGET_DIR/headroom -> $REPO/bin/headroom"
case ":$PATH:" in
  *":$TARGET_DIR:"*) ;;
  *) echo "NOTE: $TARGET_DIR is not on your PATH — add this to your shell rc:"
     echo "  export PATH=\"$TARGET_DIR:\$PATH\"" ;;
esac
echo
echo "next: headroom setup"
