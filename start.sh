#!/usr/bin/env bash
set -e

# Load .env
if [ -f .env ]; then
  set -a
  source .env
  set +a
fi

if [ -z "$ANTHROPIC_API_KEY" ]; then
  echo "ERROR: ANTHROPIC_API_KEY not set. Copy .env.sample to .env and add your key."
  exit 1
fi

mkdir -p logs/processes

echo ""
echo "⚡ Starting Energy Simulation"
echo "─────────────────────────────"

# Kill any existing processes on our ports
for port in 8000 8001 8002 8003 8080; do
  pid=$(lsof -ti tcp:$port 2>/dev/null) && kill $pid 2>/dev/null && sleep 0.3 || true
done

# Start registry
python -m registry.server > logs/processes/registry.log 2>&1 &
echo $! > logs/processes/registry.pid
printf "  Registry    :8000  "
until curl -s http://localhost:8000/health > /dev/null 2>&1; do sleep 0.3; done
echo "✓ ready"

# Start market
python -m market.agent > logs/processes/market.log 2>&1 &
echo $! > logs/processes/market.pid
printf "  Market      :8001  "
until curl -s http://localhost:8001/stats > /dev/null 2>&1; do sleep 0.3; done
echo "✓ ready"

# Start energy agent (before trader)
python -m energy.agent > logs/processes/energy.log 2>&1 &
echo $! > logs/processes/energy.pid
printf "  Energy Agent :8003  "
until curl -s http://localhost:8003/stats > /dev/null 2>&1; do sleep 0.3; done
echo "✓ ready"

# Start trader
python -m trader.agent > logs/processes/trader.log 2>&1 &
echo $! > logs/processes/trader.pid
printf "  Trader      :8002  "
until curl -s http://localhost:8002/stats > /dev/null 2>&1; do sleep 0.3; done
echo "✓ ready"

# Start observer
python -m observer.agent > logs/processes/observer.log 2>&1 &
echo $! > logs/processes/observer.pid
printf "  Observer    :8080  "
until curl -s http://localhost:8080/ > /dev/null 2>&1; do sleep 0.3; done
echo "✓ ready"

echo "─────────────────────────────"
echo ""
echo "  Dashboard → http://localhost:8080"
echo "  Logs      → logs/processes/*.log"
echo "  Stop      → ./stop.sh"
echo ""
echo "  First bids appear in ~2 minutes once the market"
echo "  opens intervals and the energy agent recommends."
echo ""
