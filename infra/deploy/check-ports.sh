#!/usr/bin/env bash
# Refuse to start if a v1 service port is already bound. Run on the VPS before
# launching uvicorn / next. Exits non-zero (and names the port) on a conflict so
# deploy scripts fail loudly instead of double-binding. See PORTS.md.
set -euo pipefail

# Allow either `check-ports.sh` (defaults) or `check-ports.sh 8010 3010 ...`.
# 5555 = Flower (S5.0, loopback) — checked by default since Phase B.
PORTS=("$@")
[ "$#" -eq 0 ] && PORTS=(8010 3010 5555)

fail=0
for port in "${PORTS[@]}"; do
  if ss -ltnH "( sport = :$port )" | grep -q ":$port"; then
    echo "CONFLICT: port $port is already in use" >&2
    fail=1
  else
    echo "OK: port $port is free"
  fi
done

exit "$fail"
