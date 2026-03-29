#!/bin/bash
# AIMOS Nuclear Kill — terminates ALL AIMOS processes and flushes VRAM.
# Usage: bash scripts/kill_all.sh

set -e
echo "=== AIMOS Nuclear Kill ==="

# 1. Kill all AIMOS Python processes (except this script's parent)
SELF_PID=$$
PIDS=$(pgrep -f "(main\.py|shared_listener|orchestrator|core\.dashboard)" 2>/dev/null || true)

if [ -n "$PIDS" ]; then
    echo "Killing PIDs: $PIDS"
    kill -9 $PIDS 2>/dev/null || true
    sleep 1
else
    echo "No AIMOS processes found."
fi

# 2. Flush VRAM (unload all Ollama models)
echo "Flushing VRAM..."
MODELS=$(curl -s http://127.0.0.1:11434/api/tags 2>/dev/null | python3 -c "
import sys,json
try:
    d=json.load(sys.stdin)
    for m in d.get('models',[]): print(m['name'])
except: pass
" 2>/dev/null)

for model in $MODELS; do
    curl -s http://127.0.0.1:11434/api/chat -d "{\"model\":\"$model\",\"messages\":[],\"keep_alive\":0}" > /dev/null 2>&1
    echo "  Unloaded: $model"
done

# 3. Reset DB statuses
cd "$(dirname "$0")/.."
source venv/bin/activate 2>/dev/null || true
python3 -c "
import psycopg2,json,sys;sys.path.insert(0,'.')
from core.config import Config
c=psycopg2.connect(host=Config.PG_HOST,port=Config.PG_PORT,dbname=Config.PG_DB,user=Config.PG_USER,password=Config.PG_PASSWORD)
cur=c.cursor()
cur.execute(\"UPDATE agents SET status='offline', updated_at=NOW()\")
cur.execute(\"UPDATE global_settings SET value=%s WHERE key='orchestrator_mode'\", (json.dumps({'enabled':False}),))
c.commit();c.close()
print('DB: all agents offline, orchestrator OFF')
" 2>/dev/null || echo "DB reset failed (non-critical)"

echo "=== Kill complete ==="
