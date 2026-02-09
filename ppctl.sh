#!/usr/bin/env bash
# ppctl.sh - PPBase server control script
# Usage: ./ppctl.sh {start|stop|restart|status|db-start|db-stop|db-restart|db-status}

set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
PIDFILE="$DIR/.ppbase.pid"
LOGFILE="$DIR/.ppbase.log"
VENV="$DIR/.venv/bin/activate"

export PPBASE_DATABASE_URL="${PPBASE_DATABASE_URL:-postgresql+asyncpg://ppbase:ppbase@localhost:5433/ppbase}"

PG_CONTAINER="ppbase-pg"
PG_IMAGE="postgres:17"
PG_PORT=5433
PG_USER="ppbase"
PG_PASSWORD="ppbase"
PG_DB="ppbase"

# ── PPBase server ─────────────────────────────────────────────

_find_pid() {
  if [[ -f "$PIDFILE" ]]; then
    local pid
    pid=$(<"$PIDFILE")
    if kill -0 "$pid" 2>/dev/null; then
      echo "$pid"
      return
    fi
    rm -f "$PIDFILE"
  fi
  pgrep -f "python.*-m ppbase serve" 2>/dev/null | head -1 || true
}

do_status() {
  local pid
  pid=$(_find_pid)
  if [[ -n "$pid" ]]; then
    echo "PPBase is running (PID $pid)"
  else
    echo "PPBase is not running"
  fi
}

do_stop() {
  local pid
  pid=$(_find_pid)
  if [[ -z "$pid" ]]; then
    echo "PPBase is not running"
    return 0
  fi
  echo "Stopping PPBase (PID $pid)..."
  kill "$pid" 2>/dev/null || true
  for i in {1..10}; do
    if ! kill -0 "$pid" 2>/dev/null; then
      rm -f "$PIDFILE"
      echo "PPBase stopped"
      return 0
    fi
    sleep 0.5
  done
  kill -9 "$pid" 2>/dev/null || true
  rm -f "$PIDFILE"
  echo "PPBase killed"
}

do_start() {
  local pid
  pid=$(_find_pid)
  if [[ -n "$pid" ]]; then
    echo "PPBase is already running (PID $pid)"
    return 1
  fi

  if [[ -f "$VENV" ]]; then
    source "$VENV"
  fi

  echo "Starting PPBase..."
  nohup python -m ppbase serve --port 8090 > "$LOGFILE" 2>&1 &
  local new_pid=$!
  echo "$new_pid" > "$PIDFILE"
  sleep 1

  if kill -0 "$new_pid" 2>/dev/null; then
    echo "PPBase started (PID $new_pid)"
    echo "  URL:  http://127.0.0.1:8090"
    echo "  Logs: $LOGFILE"
  else
    echo "Failed to start PPBase. Check $LOGFILE"
    rm -f "$PIDFILE"
    return 1
  fi
}

do_restart() {
  do_stop
  sleep 1
  do_start
}

# ── PostgreSQL Docker ─────────────────────────────────────────

_pg_status() {
  docker inspect -f '{{.State.Status}}' "$PG_CONTAINER" 2>/dev/null || true
}

do_db_status() {
  local status
  status=$(_pg_status)
  if [[ "$status" == "running" ]]; then
    echo "PostgreSQL is running (container: $PG_CONTAINER, port: $PG_PORT)"
  elif [[ -n "$status" ]]; then
    echo "PostgreSQL container exists but is $status"
  else
    echo "PostgreSQL container '$PG_CONTAINER' does not exist"
  fi
}

do_db_stop() {
  local status
  status=$(_pg_status)
  if [[ "$status" != "running" ]]; then
    echo "PostgreSQL is not running"
    return 0
  fi
  echo "Stopping $PG_CONTAINER..."
  docker stop "$PG_CONTAINER" > /dev/null
  echo "PostgreSQL stopped"
}

do_db_start() {
  local status
  status=$(_pg_status)

  if [[ "$status" == "running" ]]; then
    echo "PostgreSQL is already running (container: $PG_CONTAINER)"
    return 0
  fi

  if [[ -n "$status" ]]; then
    echo "Starting existing container $PG_CONTAINER..."
    docker start "$PG_CONTAINER" > /dev/null
  else
    echo "Creating and starting $PG_CONTAINER (PostgreSQL 17, port $PG_PORT)..."
    docker run -d \
      --name "$PG_CONTAINER" \
      -e "POSTGRES_DB=$PG_DB" \
      -e "POSTGRES_USER=$PG_USER" \
      -e "POSTGRES_PASSWORD=$PG_PASSWORD" \
      -p "$PG_PORT:5433" \
      "$PG_IMAGE" > /dev/null
  fi

  printf "Waiting for PostgreSQL to be ready..."
  for i in {1..30}; do
    if docker exec "$PG_CONTAINER" pg_isready -U "$PG_USER" > /dev/null 2>&1; then
      echo " ready."
      echo "  Container: $PG_CONTAINER"
      echo "  Port:      $PG_PORT"
      return 0
    fi
    printf "."
    sleep 1
  done
  echo ""
  echo "Warning: PostgreSQL did not become ready in 30s"
}

do_db_restart() {
  do_db_stop
  sleep 1
  do_db_start
}

# ── Dispatch ──────────────────────────────────────────────────

case "${1:-}" in
  start)      do_start      ;;
  stop)       do_stop       ;;
  restart)    do_restart    ;;
  status)     do_status     ;;
  db-start)   do_db_start   ;;
  db-stop)    do_db_stop    ;;
  db-restart) do_db_restart ;;
  db-status)  do_db_status  ;;
  *)
    echo "Usage: $0 {start|stop|restart|status|db-start|db-stop|db-restart|db-status}"
    exit 1
    ;;
esac
