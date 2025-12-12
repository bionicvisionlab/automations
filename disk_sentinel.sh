#!/usr/bin/env bash
# DiskSentinel: monitors /home (or any mount), reports visible vs hidden usage
# Installed under /etc/bvl-automations, runs as root via root's crontab

export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
CONFIG="/etc/bvl-automations/.disk_sentinel.conf"
[ -f "$CONFIG" ] && source "$CONFIG"

# DEFAULTS
# If RECOVERY_OFFSET isn't in .conf, default to 5 (Alert at 90%, Reset at 85%)
MOUNTS="${MOUNT_POINTS:-/home /hdd}"
OFFSET="${RECOVERY_OFFSET:-5}" 

for MP in $MOUNTS; do
  SAN="${MP#/}"
  FLAG="/etc/bvl-automations/.disk_sentinel_${SAN}.alerted"

  # — df: exact bytes —
  read TOTAL_BYTES USED_BYTES <<< $(
    df --output=size,used -B1 "$MP" | tail -n1
  )

  # — du: visible bytes —
  VISIBLE_BYTES=$(du -sb "$MP" 2>/dev/null | awk '{print $1}')

  # — hidden bytes —
  HIDDEN_BYTES=$(( USED_BYTES - VISIBLE_BYTES ))
  (( HIDDEN_BYTES < 0 )) && HIDDEN_BYTES=0

  # — convert to GiB —
  TOTAL_GIB=$(( TOTAL_BYTES   /1024/1024/1024 ))
  USED_GIB=$(( USED_BYTES    /1024/1024/1024 ))
  VISIBLE_GIB=$(( VISIBLE_BYTES /1024/1024/1024 ))
  HIDDEN_GIB=$(( HIDDEN_BYTES  /1024/1024/1024 ))

  # — recompute percent —
  PERC=$(( USED_BYTES * 100 / TOTAL_BYTES ))

  # Calculate the "All Clear" point based on the offset
  RECOVERY_THRESHOLD=$(( THRESHOLD - OFFSET ))

  # 1. TRIGGER CONDITION (High Watermark)
  if (( PERC >= THRESHOLD )); then
    if [ ! -f "$FLAG" ]; then
      HOST=$(hostname -s)

      # top-5 breakdown
      BREAKDOWN=$(du -sh "${MP}"/* 2>/dev/null \
                  | sort -rh \
                  | awk '{print $2 ": " $1}')
      CODE="\`\`\`\n${BREAKDOWN}\n\`\`\`"

      TEXT=":satellite: *DiskSentinel*: \`${HOST}\` \`${MP}\` is at *${USED_GIB}G/${TOTAL_GIB}G* (${PERC}% used). :naughty_naughty: 
• Visible: ${VISIBLE_GIB}G  
• Hidden:  ${HIDDEN_GIB}G (metadata, reserves, deleted-open, etc.)"

      SUGGEST=":brain2: *Suggestion:*"
      if [[ "$MP" == "/home" ]]; then
        SUGGEST+=" Move files to \`/hdd/\$USER\` to free home space."
      else
        SUGGEST+=" Archive old data or request more storage."
      fi
      
      # Added: Note about when the alert will clear
      SUGGEST+="\n_:loading: Alert will auto-resolve when usage drops below ${RECOVERY_THRESHOLD}%._"

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

  # 2. RESET CONDITION (Low Watermark)
  # Only clear the flag if we drop below the recovery threshold
  elif (( PERC < RECOVERY_THRESHOLD )); then
    if [ -f "$FLAG" ]; then
      
      RESOLVED_TEXT=":satellite: *DiskSentinel*: Normality restored on \`${HOST}\` \`${MP}\`*:  
Usage has dropped to ${USED_GIB}G (*${PERC}%). Threshold was: ${RECOVERY_THRESHOLD}%)"

      curl -sS -X POST https://slack.com/api/chat.postMessage \
        -H "Authorization: Bearer $SLACK_BOT_TOKEN" \
        -H "Content-Type: application/json; charset=utf-8" \
        --data '{
          "channel":"'"$SLACK_CHANNEL_ID"'",
          "username":"DiskSentinel",
          "icon_emoji":":satellite:",
          "text":"'"$RESOLVED_TEXT"'"
        }'

      rm -f "$FLAG"
    fi
  fi
  
  # Implicit 3. MIDDLE GROUND
  # If PERC is between 85% and 90%, do nothing (maintain current state).
done
