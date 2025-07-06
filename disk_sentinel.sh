#!/usr/bin/env bash
# DiskSentinel: monitors disk usage and alerts Slack with a per-user /home breakdown

# ----- CONFIGURATION -----
CONFIG_FILE="/home/mbeyeler/.disk_sentinel.conf"
[ -f "$CONFIG_FILE" ] && source "$CONFIG_FILE"
# ~/.disk_sentinel.conf must now contain:
#   SLACK_BOT_TOKEN="xoxb-…"
#   SLACK_CHANNEL_ID="C01234567"
#   THRESHOLD=85

MOUNT_POINT="/home"       # filesystem to watch

# ----- CHECK USAGE -----
USAGE=$(df -P "$MOUNT_POINT" \
       | awk 'NR==2 {gsub(/%/,""); print $5}')

if (( USAGE >= THRESHOLD )); then
  HOST=$(hostname -s)

  # ----- BUILD /home BREAKDOWN -----
  HOME_BKDN=$(du -sh /home/* 2>/dev/null | sort -h \
             | awk '{print $2 ": " $1}')
  CODE_BLOCK="\`\`\`\n${HOME_BKDN}\n\`\`\`"

  TEXT=":satellite: *DiskSentinel on* \`${HOST}\` is *${USAGE}%* full (≥${THRESHOLD}%).  
Here's /home by user:"

  # ----- POST to Slack as “DiskSentinel” bot -----
  curl -sS -X POST https://slack.com/api/chat.postMessage \
    -H "Authorization: Bearer $SLACK_BOT_TOKEN" \
    -H "Content-Type: application/json; charset=utf-8" \
    --data '{
      "channel": "'"$SLACK_CHANNEL_ID"'",
      "username": "DiskSentinel",
      "icon_emoji": ":satellite:",
      "text": "'"$TEXT"'\n'"$CODE_BLOCK"'"
    }'
fi
