#!/usr/bin/env bash
# DiskSentinel: monitors /home usage, alerts Slack once per threshold breach

# ————— STATE FILE —————
# prevents repeated alerts until usage drops below threshold
STATE_FILE="/home/mbeyeler/.disk_sentinel.alerted"

# ————— CONFIGURATION —————
CONFIG_FILE="/home/mbeyeler/.disk_sentinel.conf"
[ -f "$CONFIG_FILE" ] && source "$CONFIG_FILE"
# ~/.disk_sentinel.conf must export:
#   SLACK_BOT_TOKEN="xoxb-…"
#   SLACK_CHANNEL_ID="C01234567"
#   THRESHOLD=85

MOUNT_POINT="/home"    # filesystem to watch

# ————— CHECK USAGE —————
USAGE=$(df -P "$MOUNT_POINT" \
       | awk 'NR==2 {gsub(/%/,""); print $5}')

if (( USAGE >= THRESHOLD )); then
  # only alert once per crossing
  if [ ! -f "$STATE_FILE" ]; then
    HOST=$(hostname -s)

    # ————— per-user breakdown (top 5, biggest first) —————
    HOME_BKDN=$(du -sh /home/* 2>/dev/null \
      | sort -rh \
      | awk '{print $2 ": " $1}')
    CODE_BLOCK="\`\`\`\n${HOME_BKDN}\n\`\`\`"

    TEXT=":satellite: *DiskSentinel*: ${HOST} is *${USAGE}%* full (≥${THRESHOLD}%).  
${CODE_BLOCK}
:sparkles: *Suggestion:* Consider moving some files to \`/hdd/\$USER\` to free up space."

    # ————— post to Slack via chat.postMessage —————
    curl -sS -X POST https://slack.com/api/chat.postMessage \
      -H "Authorization: Bearer $SLACK_BOT_TOKEN" \
      -H "Content-Type: application/json; charset=utf-8" \
      --data '{
        "channel": "'"$SLACK_CHANNEL_ID"'",
        "username": "DiskSentinel",
        "icon_emoji": ":satellite:",
        "text": "'"$TEXT"'"
      }'

    # mark that we've alerted
    touch "$STATE_FILE"
  fi

else
  # clear the flag once back under threshold
  [ -f "$STATE_FILE" ] && rm -f "$STATE_FILE"
fi
