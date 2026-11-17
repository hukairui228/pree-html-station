#!/usr/bin/env bash
# Creates .venv next to this file and installs the toolkit's dependencies.
#   ./install.sh          macOS (MPS) / Linux+NVIDIA (CUDA) / Windows-WSL
#   ./install.sh --cpu    Linux without a GPU: small CPU-only torch build
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
py="${PYTHON:-python3}"
"$py" -c 'import sys; assert sys.version_info >= (3, 10), "Python 3.10+ required"'
"$py" -m venv "$here/.venv"
pip="$here/.venv/bin/pip"
"$pip" install -q --upgrade pip
if [ "${1:-}" = "--cpu" ]; then
  "$pip" install -q --index-url https://download.pytorch.org/whl/cpu torch torchvision
fi
"$pip" install -q -r "$here/requirements.txt"
"$here/scripts/remaster" doctor
echo
echo "Optional: pre-download the default models (~85 MB):"
echo "  $here/scripts/remaster models --download anime6b x4plus animevideo"
