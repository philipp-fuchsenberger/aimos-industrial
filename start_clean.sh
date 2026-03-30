#!/bin/bash
# AIMOS v4.6.0 - Clean Start Script
# Kills all, enables orchestrator, starts Dashboard + Listener + Orchestrator.

cd "$(dirname "$0")"

echo "--- Cleaning up old processes ---"
pkill -9 -f "core.dashboard" 2>/dev/null
pkill -9 -f "core.orchestrator" 2>/dev/null
pkill -9 -f "shared_listener" 2>/dev/null
pkill -9 -f "voice_listener" 2>/dev/null
pkill -9 -f "main.py.*--id" 2>/dev/null
rm -f /tmp/aimos_agent_*.pid
rm -f /tmp/aimos_gpu.lock
fuser -k 8080/tcp 2>/dev/null
sleep 2
echo "Processes killed."

# Virtual Environment
source venv/bin/activate

# CUDA libs from pip-installed nvidia packages (needed by faster-whisper/ctranslate2)
export LD_LIBRARY_PATH="$(python3 -c 'import nvidia.cublas.lib, nvidia.cudnn.lib; print(nvidia.cublas.lib.__path__[0] + ":" + nvidia.cudnn.lib.__path__[0])' 2>/dev/null):$LD_LIBRARY_PATH"
echo "LD_LIBRARY_PATH set for CUDA libs."

# Truncate logs
mkdir -p logs
for f in logs/*.log; do
    [ -f "$f" ] && : > "$f"
done
echo "Logs truncated."

# Enable orchestrator + expire stale messages in DB
python3 -c "
import psycopg2,json,sys;sys.path.insert(0,'.')
from core.config import Config
c=psycopg2.connect(host=Config.PG_HOST,port=Config.PG_PORT,dbname=Config.PG_DB,user=Config.PG_USER,password=Config.PG_PASSWORD)
cur=c.cursor()
cur.execute(\"INSERT INTO global_settings (key, value, updated_at) VALUES ('orchestrator_mode', %s, NOW()) ON CONFLICT (key) DO UPDATE SET value=%s, updated_at=NOW()\", (json.dumps({'enabled':True}), json.dumps({'enabled':True})))
cur.execute(\"UPDATE pending_messages SET processed=TRUE WHERE processed=FALSE AND created_at < NOW() - INTERVAL '5 minutes' AND kind IN ('text','scheduled_job','internal')\")
cur.execute(\"UPDATE pending_messages SET processed=TRUE WHERE kind LIKE 'outbound_%%' AND processed=FALSE AND created_at < NOW() - INTERVAL '5 minutes'\")
expired_out = cur.rowcount
cur.execute(\"UPDATE agents SET status='offline', updated_at=NOW()\")
c.commit();c.close()
print(f'DB: orchestrator enabled, {expired_out} outbound purged, agents reset')
" 2>/dev/null || echo "DB setup failed (non-critical)"

echo ""
echo "--- Starting Dashboard (Port 8080) ---"
nohup python3 -m core.dashboard.app > logs/dashboard.log 2>&1 &
echo "  PID=$!"

echo "--- Starting Shared Listener (Telegram + IMAP) ---"
nohup python3 scripts/shared_listener.py > logs/shared_listener.log 2>&1 &
echo "  PID=$!"

echo "--- Starting Orchestrator (Daemon Mode) ---"
nohup python3 -m core.orchestrator > logs/orchestrator.log 2>&1 &
echo "  PID=$!"

# Optional: Voice Listener (start with --voice flag)
if [[ "$1" == "--voice" || "$AIMOS_VOICE" == "1" ]]; then
    VOICE_AGENT="${VOICE_AGENT:-leyla}"
    VOICE_DEVICE="${VOICE_DEVICE:-}"
    VOICE_OUT_DEVICE="${VOICE_OUT_DEVICE:-}"
    echo "--- Starting Voice Listener (Agent: $VOICE_AGENT) ---"
    VOICE_ARGS="--agent $VOICE_AGENT"
    [ -n "$VOICE_DEVICE" ] && VOICE_ARGS="$VOICE_ARGS --device $VOICE_DEVICE"
    [ -n "$VOICE_OUT_DEVICE" ] && VOICE_ARGS="$VOICE_ARGS --output-device $VOICE_OUT_DEVICE"
    nohup python3 scripts/voice_listener.py $VOICE_ARGS > logs/voice_listener.log 2>&1 &
    echo "  PID=$!"
fi

sleep 2

# ── Post-start health checks ──────────────────────────────────────────────
echo ""
echo "--- Health Checks ---"

# Check IMAP connectivity for email-enabled agents
python3 -c "
import psycopg2, json, ssl, imaplib, sys
sys.path.insert(0, '.')
from core.config import Config

c = psycopg2.connect(host=Config.PG_HOST, port=Config.PG_PORT, dbname=Config.PG_DB, user=Config.PG_USER, password=Config.PG_PASSWORD)
cur = c.cursor()
cur.execute(\"SELECT name, config, env_secrets FROM agents WHERE config::text LIKE '%imap_polling%'\")
ok = 0; fail = 0
for name, cfg, sec in cur.fetchall():
    cfg = json.loads(cfg) if isinstance(cfg, str) else cfg
    sec = json.loads(sec) if isinstance(sec, str) else sec
    if not cfg.get('imap_polling'):
        continue
    addr = sec.get('EMAIL_ADDRESS', '')
    passwd = sec.get('EMAIL_PASSWORD', '')
    host = sec.get('EMAIL_IMAP_HOST', '')
    port = int(sec.get('EMAIL_IMAP_PORT', '993'))
    if not addr or not passwd or not host:
        print(f'  IMAP [{name}]: SKIP (credentials missing)')
        fail += 1
        continue
    try:
        ctx = ssl.create_default_context()
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        conn = imaplib.IMAP4_SSL(host, port, ssl_context=ctx)
        conn.login(addr, passwd)
        conn.select('INBOX')
        _, ids = conn.search(None, 'UNSEEN')
        n = len(ids[0].split()) if ids[0] else 0
        conn.logout()
        print(f'  IMAP [{name}]: OK ({n} unread)')
        ok += 1
    except Exception as e:
        print(f'  IMAP [{name}]: FAIL ({e})')
        fail += 1
c.close()
if fail > 0:
    print(f'  WARNING: {fail} IMAP connection(s) failed!')
elif ok == 0:
    print('  No IMAP agents configured')
else:
    print(f'  All {ok} IMAP connection(s) verified')
" 2>/dev/null || echo "  IMAP check skipped (non-critical)"

# Check Ollama is reachable
if curl -sf http://127.0.0.1:11434/api/tags > /dev/null 2>&1; then
    MODEL_COUNT=$(curl -sf http://127.0.0.1:11434/api/tags | python3 -c "import json,sys; print(len(json.load(sys.stdin).get('models',[])))" 2>/dev/null)
    echo "  Ollama: OK ($MODEL_COUNT model(s) available)"
else
    echo "  Ollama: NOT RUNNING — agents will fail!"
fi

# Check GPU
if command -v nvidia-smi &>/dev/null; then
    GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1)
    GPU_MEM=$(nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader,nounits 2>/dev/null | head -1)
    echo "  GPU: $GPU_NAME (${GPU_MEM} MiB)"
else
    echo "  GPU: nvidia-smi not found"
fi

echo ""
echo "=== AIMOS v4.6.1 Ready ==="
echo "  NOTE: This script is the ONLY way to apply code updates."
echo "  Dashboard API recycle does NOT reload Python code."
IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo "  Dashboard:    http://${IP:-localhost}:8080"
echo "  Orchestrator: enabled (auto-pilot)"
echo "  Listener:     Telegram + IMAP relay"
echo ""
echo "Check logs:"
echo "  tail -f logs/orchestrator.log"
echo "  tail -f logs/shared_listener.log"
