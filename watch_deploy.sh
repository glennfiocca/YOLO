#!/bin/bash
# Polls DO for the latest deployment of launchpad-jobs and emits a line
# whenever its phase changes. Exits when the latest deploy is ACTIVE
# (== fully rolled out, old containers terminated).
#
# Uses doctl, which must be on PATH and authenticated.

APP_ID="b9033a4b-626c-4256-9900-a459dd6cf061"
last_phase=""
last_id=""

while true; do
  line=$(doctl apps list-deployments "$APP_ID" --format ID,Phase,Progress --no-header 2>/dev/null | head -1)
  if [ -z "$line" ]; then
    echo "[$(date -u +%H:%M:%S)] doctl returned nothing"
    sleep 30
    continue
  fi
  id=$(echo "$line" | awk '{print $1}')
  phase=$(echo "$line" | awk '{print $2}')
  progress=$(echo "$line" | awk '{print $3}')

  if [ "$id" != "$last_id" ] || [ "$phase" != "$last_phase" ]; then
    echo "[$(date -u +%H:%M:%S)] deploy=$id phase=$phase progress=$progress"
    last_phase="$phase"
    last_id="$id"
  fi

  if [ "$phase" = "ACTIVE" ]; then
    echo "[$(date -u +%H:%M:%S)] DEPLOY_LIVE id=$id"
    exit 0
  fi
  if [ "$phase" = "ERROR" ] || [ "$phase" = "FAILED" ]; then
    echo "[$(date -u +%H:%M:%S)] DEPLOY_FAILED id=$id phase=$phase"
    exit 1
  fi
  sleep 30
done
