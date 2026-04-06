#!/bin/zsh
# Auto-sync YOLO project to GitHub
# Run by cron hourly

REPO_DIR="/Users/glennfiocca/YOLO"
LOG_FILE="$REPO_DIR/sync.log"
MAX_LOG_LINES=500

cd "$REPO_DIR" || exit 1

# Stage all changes
git add -A

# Only commit if there are staged changes
if ! git diff --cached --quiet; then
  git commit -m "auto: sync $(date '+%Y-%m-%d %H:%M:%S')"
  git push origin main >> "$LOG_FILE" 2>&1
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Pushed changes" >> "$LOG_FILE"
else
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] No changes" >> "$LOG_FILE"
fi

# Trim log to last MAX_LOG_LINES lines
if [ -f "$LOG_FILE" ]; then
  tail -n "$MAX_LOG_LINES" "$LOG_FILE" > "$LOG_FILE.tmp" && mv "$LOG_FILE.tmp" "$LOG_FILE"
fi
