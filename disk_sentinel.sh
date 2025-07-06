#!/usr/bin/env bash
# DiskSentinel: monitors /home and /hdd usage, alerts Slack once per breach each

# ————— CONFIGURATION —————
CONFIG_FILE="/home/mbeyeler/.disk_sentinel.conf"
[ -f "$CONFIG_FILE" ] && source "$CONFIG_FILE"
# ~/.disk_sentinel.conf must export:
#   SLACK_BOT_TOKEN="xoxb-…"
#   SLACK_CHANNEL_ID="C01234567"
#   THRESHOLD=85
# Optionally override which mounts to watch:
#   MOUNT_POINTS="/home /hdd"

# default mounts if none set in config
MOUNT_POINTS="${MOUNT_POINTS:-/home /hdd}"

# ————— LOOP OVER MOUNTS —————
for MP in $MOUNT_POINTS; do
  # sanitize mount name ("/home"→"home", "/hdd"→"hdd")
  SAN="${MP#/}"
  STATE_FILE="/home/mbeyeler/.disk_sentinel_${SAN}.alerted"

  # get numeric usage %
  USAGE=$(df -P "$MP" | awk 'NR==2 {gsub(/%/,""); print $5}')

  if (( USAGE >= THRESHOLD )); then
    # only send once until it dips back below
    if [ ! -f "$STATE_FILE" ]; then
      HOST=$(hostname -s)

      # breakdown of top-5 largest subdirs
      BREAKDOWN=$(du -sh "${MP}"/* 2>/dev/null \
                  | sort -rh \
                  | awk '{print $2 ": " $1}')
      CODE_BLOCK="\`\`\`\n${BREAKDOWN}\n\`\`\`"

      # summary text
      TEXT=":satellite: *DiskSentinel* on ${HOST} reports \`${MP}\` is *${USAGE}%* full (≥${THRESHOLD}%):"

      # mount-specific suggestion
      if [[ "$MP" == "/home" ]]; then
        SUGGEST=":sparkles: *Suggestion:* Move some files to \`/hdd/\$USER\` to free up home space."
      else
        SUGGEST=":sparkles: *Suggestion:* Archive older data or request more storage before this fills up."
      fi

      # send the Slack message
      curl -sS -X POST https://slack.com/api/chat.postMessage \
        -H "Authorization: Bearer $SLACK_BOT_TOKEN" \
        -H "Content-Type: application/json; charset=utf-8" \
        --data '{
          "channel": "'"$SLACK_CHANNEL_ID"'",
          "username": "DiskSentinel",
          "icon_emoji": ":satellite:",
          "text": "'"$TEXT"'\n'"$CODE_BLOCK"'\n'"$SUGGEST"'"
        }'

      # mark alert sent
      touch "$STATE_FILE"
    fi

  else
    # clear the flag so future breaches will alert again
    [ -f "$STATE_FILE" ] && rm -f "$STATE_FILE"
  fi
done
