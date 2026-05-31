#!/usr/bin/env bash
# run_daily_analysis.sh — run AI analysis for all tickers, then commit & push.
# GitHub Actions auto-update-alerts.yml handles Discord notifications after push.
#
# Usage:
#   ./scripts/run_daily_analysis.sh               # analyze NVDA + SMCI
#   ./scripts/run_daily_analysis.sh NVDA          # NVDA only
#   ./scripts/run_daily_analysis.sh SMCI          # SMCI only
#   ./scripts/run_daily_analysis.sh NVDA SMCI     # explicit list
#
# Required env vars (per ticker):
#   FINNHUB_NVDA_API_KEY   (falls back to FINNHUB_API_KEY if unset)
#   FINNHUB_SMCI_API_KEY   (falls back to FINNHUB_API_KEY if unset)
#   GITHUB_TOKEN           (auto-set by `gh auth token` if gh CLI is installed)

set -euo pipefail
cd "$(dirname "$0")/.."

# ── Resolve GITHUB_TOKEN from gh CLI if not set ──────────────────────────────
if [[ -z "${GITHUB_TOKEN:-}" ]]; then
  if command -v gh &>/dev/null; then
    GITHUB_TOKEN="$(gh auth token 2>/dev/null || true)"
  fi
fi
if [[ -z "${GITHUB_TOKEN:-}" ]]; then
  echo "❌  GITHUB_TOKEN is required (needed for Claude via GitHub Models)" >&2
  exit 1
fi

# ── Allow legacy FINNHUB_API_KEY as fallback for per-ticker keys (local only) ─
: "${FINNHUB_NVDA_API_KEY:=${FINNHUB_API_KEY:-}}"
: "${FINNHUB_SMCI_API_KEY:=${FINNHUB_API_KEY:-}}"

# ── Determine which tickers to run ───────────────────────────────────────────
if [[ $# -gt 0 ]]; then
  TICKERS=("$@")
else
  TICKERS=(NVDA SMCI)
fi

CHANGED_CONFIGS=()

# ── Run analysis per ticker ───────────────────────────────────────────────────
for TICKER in "${TICKERS[@]}"; do
  KEY_VAR="FINNHUB_${TICKER}_API_KEY"
  FINNHUB_KEY="${!KEY_VAR:-}"

  if [[ -z "$FINNHUB_KEY" ]]; then
    echo "❌  ${KEY_VAR} is not set — skipping ${TICKER}" >&2
    continue
  fi

  echo ""
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "  📡  Running AI analysis for ${TICKER}"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

  python3 scripts/auto_analyze_and_update.py \
    --ticker "${TICKER}" \
    --finnhub-key "${FINNHUB_KEY}" \
    --github-token "${GITHUB_TOKEN}"

  CONFIG="config/${TICKER,,}_alert_levels.json"
  if git diff --quiet "${CONFIG}" 2>/dev/null; then
    echo "ℹ️   ${CONFIG} unchanged — nothing to commit for ${TICKER}"
  else
    CHANGED_CONFIGS+=("${CONFIG}")
    echo "✅  ${CONFIG} updated"
  fi
done

# ── Commit & push if anything changed ────────────────────────────────────────
if [[ ${#CHANGED_CONFIGS[@]} -eq 0 ]]; then
  echo ""
  echo "ℹ️   No config changes — nothing to commit. Done."
  exit 0
fi

LABEL=$(IFS=/ ; echo "${TICKERS[*]}")
DATE=$(date +%Y-%m-%d)

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  📦  Committing and pushing updated configs"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

git add "${CHANGED_CONFIGS[@]}"
git commit -m "manual: update ${LABEL} alert levels ${DATE}"
git push origin main

echo ""
echo "🚀  Pushed! GitHub Actions will now send Discord alerts automatically."
