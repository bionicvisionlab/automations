#!/usr/bin/env bash
# DiskSentinel: monitors /home (or any mount), reports visible vs hidden usage

CONFIG="$HOME/.disk_sentinel.conf"
[ -f "$CONFIG" ] && source "$CONFIG"
# Needs in ~/.disk_sentinel.conf:
#   SLACK_BOT_TOKEN
#   SLACK_CHANNEL_ID
#   THRESHOLD
# Optional override:
#   MOUNT_POINTS="/home /hdd"

MOUNTS="${MOUNT_POINTS:-/home}"

for MP in $MOUNTS; do
  SAN="${MP#/}"
  FLAG="$HOME/.disk_sentinel_${SAN}.alerted"

  # — df: exact bytes —
  read TOTAL_BYTES USED_BYTES <<< $(
    sudo df --output=size,used -B1 "$MP" | tail -n1
  )

  # — du: visible bytes —
  VISIBLE_BYTES=$(sudo du -sb "$MP" 2>/dev/null | awk '{print $1}')

  # — hidden bytes (may include FS metadata, deleted-open files, reserved blocks…) —
  HIDDEN_BYTES=$(( USED_BYTES - VISIBLE_BYTES ))
  (( HIDDEN_BYTES < 0 )) && HIDDEN_BYTES=0

  # — convert to GiB, integer —
  TOTAL_GIB=$(( TOTAL_BYTES  /1024/1024/1024 ))
  USED_GIB=$(( USED_BYTES   /1024/1024/1024 ))
  VISIBLE_GIB=$(( VISIBLE_BYTES/1024/1024/1024 ))
  HIDDEN_GIB=$(( HIDDEN_BYTES /1024/1024/1024 ))

  # — recompute percent based on df —
  PERC=$(( USED_BYTES *100 / TOTAL_BYTES ))

  if (( PERC >= THRESHOLD )); then
    if [ ! -f "$FLAG" ]; then
      HOST=$(hostname -s)

      # top-5 breakdown of actual dirs
      BREAKDOWN=$(sudo du -sh "${MP}"/* 2>/dev/null \
                  | sort -rh \
                  | awk '{print $2 ": " $1}')
      CODE="\`\`\`\n${BREAKDOWN}\n\`\`\`"

      TEXT=":satellite: *DiskSentinel on* ${HOST} reports \`${MP}\` at *${USED_GIB}G/${TOTAL_GIB}G* (${PERC}% used).  
• Visible: ${VISIBLE_GIB}G  
• Hidden:  ${HIDDEN_GIB}G (metadata, reserves, deleted-open, etc.)"
      SUGGEST=":sparkles: *Suggestion:*"
      if [[ "$MP" == "/home" ]]; then
        SUGGEST+=" Move files to \`/hdd/\$USER\` to free home space."
      else
        SUGGEST+=" Archive old data or request more storage."
      fi

      curl -sS -X POST https://slack.com/api/chat.postMessage \
        -H "Authorization: Bearer $SLACK_BOT_TOKEN" \
        -H "Content-Type: application/json; charset=utf-8" \
        --data '{
          "channel":"'"$SLACK_CHANNEL_ID"'",
          "username":"DiskSentinel",
          "icon_emoji":":satellite:",
          "text":"'"$TEXT"'\n'"$CODE"'\n'"$SUGGEST"'"
        }'

      touch "$FLAG"
    fi
  else
    [ -f "$FLAG" ] && rm -f "$FLAG"
  fi
done
