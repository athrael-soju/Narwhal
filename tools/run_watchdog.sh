#!/usr/bin/env bash
# Node-local watchdog for a long run: one verdict line every 15 s.
#
#   nohup tools/run_watchdog.sh /abs/path/to/run-dir & 
#
# Runs beside the router, never over SSH - remote watch loops fail silently.
# Checks: bench and router process liveness, the ejected list and
# failed/served counters off /arrow/state, and every engine's /health
# from the run's own config.json. Exits when the run's status file says
# done. The collection tick reads this log; ENGINE_DOWN lines beyond a
# few minutes are the abort signal.
D=$1
LOG=$D/watchdog.log
CFG=$D/config.json
echo "$(date -u +%H:%M:%S) watchdog up" >> $LOG
while true; do
  line="$(date -u +%H:%M:%S)"
  pgrep -f narwhal-bench > /dev/null && line="$line bench=up" || line="$line bench=DOWN"
  curl -sf -m 4 localhost:8011/health > /dev/null && line="$line router=up" || line="$line router=DOWN"
  st=$(curl -sf -m 4 localhost:8011/arrow/state 2>/dev/null)
  if [ -n "$st" ]; then
    ej=$(echo "$st" | python3 -c "import json,sys; s=json.load(sys.stdin); print(','.join(s['ejected']) or '-', s['failed'], s['served'])" 2>/dev/null)
    line="$line ejected/failed/served=$ej"
  fi
  down=""
  for u in $(python3 -c "import json; print(' '.join(e['url'] for e in json.load(open('$CFG'))['engines']))" 2>/dev/null); do
    curl -sf -m 4 "$u/health" > /dev/null 2>&1 || down="$down $u"
  done
  [ -n "$down" ] && line="$line ENGINE_DOWN:$down"
  echo "$line" >> $LOG
  grep -q "COOLDOWN WALK DONE" $D/status 2>/dev/null && { echo "$(date -u +%H:%M:%S) watchdog: run done, exiting" >> $LOG; exit 0; }
  sleep 15
done
