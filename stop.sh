#!/usr/bin/env bash
echo "Stopping Energy Simulation..."
for pid_file in logs/processes/*.pid; do
  [ -f "$pid_file" ] || continue
  name=$(basename "$pid_file" .pid)
  pid=$(cat "$pid_file")
  if kill "$pid" 2>/dev/null; then
    echo "  stopped $name (pid $pid)"
  fi
  rm "$pid_file"
done
echo "Done."
