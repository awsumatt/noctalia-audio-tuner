#!/usr/bin/env bash
# Refresh plugin/noctalia.d.luau from the pinned official-plugins checkout.
# The file is type-only (luau-lsp definitions); Noctalia does not read it.
set -euo pipefail
cd "$(dirname "$0")/.."
cp references/noctalia.d.luau plugin/noctalia.d.luau
echo "wrote plugin/noctalia.d.luau (gitignored)"
