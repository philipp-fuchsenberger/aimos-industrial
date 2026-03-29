#!/bin/bash
# AIMOS Safe Restart — Updates code without losing messages
# Usage: bash scripts/safe_restart.sh
#
# Unlike start_clean.sh, this script:
# 1. Waits for all agents to finish their current work (idle/offline)
# 2. Preserves ALL pending messages (no expiry, no deletion)
# 3. Restarts Dashboard + Orchestrator + Listener with fresh code
# 4. Agents resume processing where they left off

cd "$(dirname "$0")/.."
source venv/bin/activate

echo "=== AIMOS Safe Restart v4.3.5 ==="
echo ""

# Step 1: Wait for agents to finish
echo "--- Step 1: Waiting for agents to finish current work ---"
MAX_WAIT=120  # max 2 minutes
WAITED=0
while true; do
    ACTIVE=$(python3 -c "
import psycopg2,os
c=psycopg2.connect(host='172.28.0.2',port=5432,dbname='aimos',user='n8n_user',password=os.getenv('PG_PASSWORD',''))
cur=c.cursor()
cur.execute(\"SELECT name FROM agents WHERE status IN ('active','running','starting')\")
names=[r[0] for r in cur.fetchall()]
c.close()
print(','.join(names) if names else '')
" 2>/dev/null)

    if [ -z "$ACTIVE" ]; then
        echo "  All agents idle/offline. Safe to restart."
        break
    fi

    if [ $WAITED -ge $MAX_WAIT ]; then
        echo "  WARNING: Agents still active after ${MAX_WAIT}s: $ACTIVE"
        echo "  Force-stopping them..."
        break
    fi

    echo "  Waiting for: $ACTIVE ($WAITED/${MAX_WAIT}s)"
    sleep 5
    WAITED=$((WAITED + 5))
done

# Step 2: Disable orchestrator (prevent new spawns)
echo ""
echo "--- Step 2: Disabling orchestrator ---"
python3 -c "
import psycopg2,json,os
c=psycopg2.connect(host='172.28.0.2',port=5432,dbname='aimos',user='n8n_user',password=os.getenv('PG_PASSWORD',''))
cur=c.cursor()
cur.execute(\"UPDATE global_settings SET value=%s WHERE key='orchestrator_mode'\", (json.dumps({'enabled':False}),))
c.commit();c.close()
print('Orchestrator disabled')
" 2>/dev/null

# Step 3: Kill processes (but NOT messages)
echo ""
echo "--- Step 3: Stopping processes ---"
pkill -9 -f "core.dashboard" 2>/dev/null
pkill -9 -f "core.orchestrator" 2>/dev/null
pkill -9 -f "shared_listener" 2>/dev/null
pkill -9 -f "main.py.*--id" 2>/dev/null
pkill -9 -f "test_monitor" 2>/dev/null
fuser -k 8080/tcp 2>/dev/null
sleep 2
echo "Processes stopped."

# Step 4: Clean DB state (but preserve messages!)
echo ""
echo "--- Step 4: Cleaning agent state (messages preserved) ---"
python3 -c "
import psycopg2,json,os
c=psycopg2.connect(host='172.28.0.2',port=5432,dbname='aimos',user='n8n_user',password=os.getenv('PG_PASSWORD',''))
cur=c.cursor()
# Reset agent status but DO NOT touch pending_messages
cur.execute(\"UPDATE agents SET status='offline', pid=NULL WHERE TRUE\")
# Re-enable orchestrator
cur.execute(\"INSERT INTO global_settings (key, value, updated_at) VALUES ('orchestrator_mode', %s, NOW()) ON CONFLICT (key) DO UPDATE SET value=%s, updated_at=NOW()\", (json.dumps({'enabled':True}), json.dumps({'enabled':True})))
# Count preserved messages
cur.execute(\"SELECT COUNT(*) FROM pending_messages WHERE processed=FALSE\")
preserved=cur.fetchone()[0]
c.commit();c.close()
print(f'Agents reset. {preserved} pending messages preserved.')
" 2>/dev/null

# Step 5: CUDA libs
export LD_LIBRARY_PATH="$(python3 -c 'import nvidia.cublas.lib, nvidia.cudnn.lib; print(nvidia.cublas.lib.__path__[0] + ":" + nvidia.cudnn.lib.__path__[0])' 2>/dev/null):$LD_LIBRARY_PATH"

# Step 6: Start fresh
echo ""
echo "--- Step 5: Starting fresh processes ---"
mkdir -p logs

nohup python3 -m core.dashboard.app > logs/dashboard.log 2>&1 &
echo "  Dashboard PID=$!"

nohup python3 scripts/shared_listener.py > logs/shared_listener.log 2>&1 &
echo "  Listener PID=$!"

nohup python3 -m core.orchestrator > logs/orchestrator.log 2>&1 &
echo "  Orchestrator PID=$!"

sleep 2
echo ""
echo "=== AIMOS v4.3.5 Safe Restart Complete ==="
PENDING=$(python3 -c "
import psycopg2,os
c=psycopg2.connect(host='172.28.0.2',port=5432,dbname='aimos',user='n8n_user',password=os.getenv('PG_PASSWORD',''))
cur=c.cursor()
cur.execute('SELECT COUNT(*) FROM pending_messages WHERE processed=FALSE')
print(cur.fetchone()[0])
c.close()
" 2>/dev/null)
echo "  Pending messages: $PENDING (all preserved)"
echo "  Orchestrator: enabled (auto-pilot)"
echo "  No messages lost. Agents will resume automatically."
