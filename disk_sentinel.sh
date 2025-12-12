#!/usr/bin/env bash
# DiskSentinel: Smart disk monitoring with Hysteresis, Privacy, and Ghost detection.
#
# Recommended Crontab (Run every 10m, use flock to prevent stampedes):
# */10 * * * * /usr/bin/flock -n /var/lock/disk_sentinel.lock /etc/bvl-automations/disk_sentinel.sh >> /var/log/disk_sentinel.log 2>&1

# --- CONFIGURATION ---
CONFIG="/etc/bvl-automations/.disk_sentinel.conf"
[ -f "$CONFIG" ] && source "$CONFIG"

# DEFAULTS (Overridable via config file)
MOUNTS="${MOUNT_POINTS:-/home /hdd}"
OFFSET="${RECOVERY_OFFSET:-5}"          # Drop 5% below threshold to clear alert
NAG_INTERVAL="${RENOTIFICATION_MINUTES:-480}" # Remind every 8 hours if still high

# HARDENING: Ensure we have access to system tools (lsof, awk, curl)
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

HOST=$(hostname -s)

# --- MAIN LOOP ---
for MP in $MOUNTS; do
  # Sanitize mount path for filename (e.g., /home -> home)
  SAN="${MP#/}"
  
  # FAILSAFE FLAG PATH
  # Try /etc; if disk is full/read-only, fallback to RAM (/dev/shm)
  FLAG_NAME=".disk_sentinel_${SAN}.alerted"
  FLAG_FILE="/etc/bvl-automations/${FLAG_NAME}"
  
  if [ ! -w "/etc/bvl-automations/" ] && [ ! -f "$FLAG_FILE" ]; then
     FLAG_FILE="/dev/shm/${FLAG_NAME}"
  fi
  # If the file already exists in SHM from a previous run, respect it
  [ -f "/dev/shm/${FLAG_NAME}" ] && FLAG_FILE="/dev/shm/${FLAG_NAME}"

  # 1. METRICS COLLECTION
  # Get raw bytes for precision calculation
  read TOTAL_BYTES USED_BYTES <<< $(df --output=size,used -B1 "$MP" | tail -n1)
  
  # Get visible usage (Privacy: max-depth 1 to avoid listing user subfiles)
  VISIBLE_BYTES=$(du -sb --max-depth=1 "$MP" 2>/dev/null | head -n 1 | awk '{print $1}')
  
  # Calculate Ghost (deleted-open) space
  HIDDEN_BYTES=$(( USED_BYTES - VISIBLE_BYTES ))
  (( HIDDEN_BYTES < 0 )) && HIDDEN_BYTES=0

  # 2. CALCULATIONS (AWK for float precision)
  # Returns: Total(GB) Used(GB) Visible(GB) Hidden(GB) Percent(%) RecoveryLimit(%)
  read TOTAL_GIB USED_GIB VISIBLE_GIB HIDDEN_GIB PERC RECOVERY_THRES <<< $(awk -v t="$TOTAL_BYTES" -v u="$USED_BYTES" -v v="$VISIBLE_BYTES" -v h="$HIDDEN_BYTES" -v th="$THRESHOLD" -v off="$OFFSET" 'BEGIN {
    printf "%.2f %.2f %.2f %.2f %.2f %.2f", t/1073741824, u/1073741824, v/1073741824, h/1073741824, (u/t)*100, th-off
  }')

  # Logic Checks (0=False, 1=True)
  IS_HIGH=$(awk -v p="$PERC" -v t="$THRESHOLD" 'BEGIN {print (p >= t) ? 1 : 0}')
  IS_LOW=$(awk -v p="$PERC" -v r="$RECOVERY_THRES" 'BEGIN {print (p < r) ? 1 : 0}')

  # --- LOGIC BRANCHES ---

  # CASE A: USAGE IS HIGH (Alert or Remind)
  if [ "$IS_HIGH" -eq 1 ]; then
    
    SHOULD_ALERT=0
    ALERT_TYPE="New"

    if [ ! -f "$FLAG_FILE" ]; then
      # No flag = Fresh Alert
      SHOULD_ALERT=1
    else
      # Flag exists = Check if stale (Nagging)
      # Find file only if modified more than X minutes ago
      IS_STALE=$(find "$FLAG_FILE" -mmin +$NAG_INTERVAL 2>/dev/null)
      if [ -n "$IS_STALE" ]; then
        SHOULD_ALERT=1
        ALERT_TYPE="Reminder"
      fi
    fi

    if [ "$SHOULD_ALERT" -eq 1 ]; then
      # -- 1. Breakdown (Top 5 Folders) --
      BREAKDOWN=$(du -sh "${MP}"/* 2>/dev/null | sort -rh | head -n 5 | awk '{print $2 ": " $1}')
      
      # -- 2. Ghost Stats (Privacy Sanitized) --
      # Sums usage by User+Process. Hides specific filenames.
      GHOST_STATS=$(lsof +L1 2>/dev/null | grep "$MP" | awk '{print $3 " (" $1 "): " $7}' | \
        awk '{a[$1]+=$2} END {for (i in a) {printf "%s %.2fG\n", i, a[i]/1073741824}}' | sort -rn -k2)

      [ -n "$GHOST_STATS" ] && GHOST_SECTION="\n:ghost: *Ghost Usage (Deleted-Open):*\n\`\`\`\n${GHOST_STATS}\n\`\`\`" || GHOST_SECTION=""

      # -- 3. Build Message --
      if [ "$ALERT_TYPE" == "New" ]; then
         HEADER=":satellite: *DiskSentinel Alert*"
      else
         HEADER=":alarm_clock: *DiskSentinel Reminder*"
      fi

      TEXT="$HEADER: \`${HOST}\` \`${MP}\` is at *${USED_GIB}G / ${TOTAL_GIB}G* (${PERC}%) :naughty_naughty:.
• Visible: ${VISIBLE_GIB}G
• Hidden:  ${HIDDEN_GIB}G"
      
      CODE="\`\`\`\n${BREAKDOWN}\n\`\`\`"
      SUGGEST=":sparkles: *Action:* Check top users below."
      SUGGEST+="\n_Alert auto-resolves when usage drops below ${RECOVERY_THRES}%._"

      # -- 4. Send Slack --
      curl -sS -X POST https://slack.com/api/chat.postMessage \
        -H "Authorization: Bearer $SLACK_BOT_TOKEN" \
        -H "Content-Type: application/json; charset=utf-8" \
        --data '{
          "channel":"'"$SLACK_CHANNEL_ID"'",
          "username":"DiskSentinel",
          "icon_emoji":":satellite:",
          "text":"'"$TEXT"'\n'"$CODE"''"$GHOST_SECTION"'\n'"$SUGGEST"'"
        }'

      # -- 5. Set Flag (Update Timestamp) --
      # Try touching. If disk is hard-locked full, force to RAM.
      if ! touch "$FLAG_FILE" 2>/dev/null; then
           FLAG_FILE="/dev/shm/${FLAG_NAME}"
           touch "$FLAG_FILE"
      fi
    fi

  # CASE B: USAGE IS LOW (Resolve)
  elif [ "$IS_LOW" -eq 1 ]; then
    # Check if a flag exists (in /etc OR /dev/shm) to know if we need to send "Resolved"
    FOUND_FLAG=""
    [ -f "/etc/bvl-automations/${FLAG_NAME}" ] && FOUND_FLAG="/etc/bvl-automations/${FLAG_NAME}"
    [ -f "/dev/shm/${FLAG_NAME}" ] && FOUND_FLAG="/dev/shm/${FLAG_NAME}"

    if [ -n "$FOUND_FLAG" ]; then
      RESOLVED_TEXT=":white_check_mark: *Normality Restored*: \`${HOST}\` \`${MP}\` dropped to *${PERC}%* :sarcastic:."
      
      curl -sS -X POST https://slack.com/api/chat.postMessage \
        -H "Authorization: Bearer $SLACK_BOT_TOKEN" \
        -H "Content-Type: application/json; charset=utf-8" \
        --data '{
          "channel":"'"$SLACK_CHANNEL_ID"'",
          "username":"DiskSentinel",
          "text":"'"$RESOLVED_TEXT"'"
        }'
      
      # Clean up all possible flags
      rm -f "/etc/bvl-automations/${FLAG_NAME}" 2>/dev/null
      rm -f "/dev/shm/${FLAG_NAME}" 2>/dev/null
    fi
  fi
done
