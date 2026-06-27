#!/usr/bin/env bash
# Nightly entrypoint for sg-legal-help: build -> sanity gate -> (opt-in) deploy.
#
# The user's personal agent owns scheduling and sets its own timer (see
# CLAUDE.md for cadence tiers); this script is the authoritative pipeline.
# Deploy is OPT-IN behind --deploy so local/CI runs never ship by accident.
# Any non-zero exit is the monitoring signal.
#
# Usage:
#   scripts/nightly.sh             # build + sanity checks, NO deploy
#   scripts/nightly.sh --deploy    # build + sanity checks + deploy to S3
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO" || exit 1

DB="sg-legal-help.db"

# Snapshot the previous DB so the sanity gate can enforce the append-mostly
# row-count invariant (resources never shrinks).
if [ -f "$DB" ]; then
  cp "$DB" "sg-legal-help.previous.db"
fi

# Incremental build: sync existing DB from S3, rebuild changed rows, set up FTS.
uv run zeeker build --sync-from-s3 --setup-fts
BUILD_RC=$?
if [ $BUILD_RC -ne 0 ]; then
  echo "BUILD FAILED (rc=$BUILD_RC) -- not deploying" >&2
  exit $BUILD_RC
fi

# Integrity gate -- blocks deploy on any violation.
if ! uv run python scripts/sanity_checks.py --db "$DB" --previous "sg-legal-help.previous.db"; then
  echo "SANITY CHECKS FAILED -- not deploying" >&2
  exit 1
fi

if [ "${1:-}" = "--deploy" ]; then
  uv run zeeker deploy
else
  echo "=== deploy skipped (pass --deploy to ship to S3) ==="
fi
