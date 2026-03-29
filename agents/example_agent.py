"""Example AIMOS Agent Profile.

This file documents the agent's purpose. The actual configuration
(system prompt, skills, secrets) lives in PostgreSQL and is managed
via the Dashboard Wizard.

To create a new agent:
1. Open the Dashboard (http://server:8080)
2. Click "New Agent"
3. Fill in the Wizard (name, type, skills, Telegram token)
4. Save — the Orchestrator will manage the agent lifecycle automatically
"""

# Agent: example
# Type: Work Agent (deep memory, sequential GPU access)
# Skills: brave_search, email, file_ops, scheduler
# Channel: Telegram
