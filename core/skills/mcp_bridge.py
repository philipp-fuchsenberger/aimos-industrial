"""
MCP Bridge — Exposes AIMOS Skills as MCP-compatible tool providers.
=====================================================================
CR-121 Phase 3: Model Context Protocol compatibility layer.

This module wraps existing BaseSkill instances into MCP-compatible
tool definitions. It does NOT replace BaseSkill — it runs alongside it.

Two modes:
1. **Export mode**: Generate MCP tool manifest (JSON) from SKILL_REGISTRY
2. **Server mode**: Run as MCP stdio server (for external MCP clients)

Usage:
    # Generate MCP manifest for all skills
    python -m core.skills.mcp_bridge --manifest

    # Run as MCP stdio server (for external agent frameworks)
    python -m core.skills.mcp_bridge --serve --agent neo

The existing BaseSkill → register_tool() → Ollama native tool-calling
pipeline remains the PRIMARY path. MCP is an ADDITIONAL interface.
"""

import json
import logging
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

log = logging.getLogger("AIMOS.mcp_bridge")


def skill_to_mcp_tools(skill_name: str, skill_cls) -> list[dict]:
    """Convert a BaseSkill's get_tools() output to MCP tool format.

    MCP tool format:
    {
        "name": "tool_name",
        "description": "...",
        "inputSchema": {
            "type": "object",
            "properties": { "param": {"type": "string", "description": "..."} },
            "required": ["param"]
        }
    }
    """
    try:
        # Instantiate skill minimally
        instance = skill_cls(agent_name="mcp_export")
    except TypeError:
        try:
            instance = skill_cls()
        except Exception:
            return []

    tools = []
    try:
        for tool_def in instance.get_tools():
            mcp_tool = {
                "name": f"{skill_name}__{tool_def['name']}",  # namespace: skill__tool
                "description": tool_def.get("description", ""),
                "inputSchema": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            }
            params = tool_def.get("parameters", {})
            for pname, pinfo in params.items():
                if isinstance(pinfo, dict):
                    mcp_tool["inputSchema"]["properties"][pname] = {
                        "type": pinfo.get("type", "string"),
                        "description": pinfo.get("description", pname),
                    }
                    if pinfo.get("required", False):
                        mcp_tool["inputSchema"]["required"].append(pname)
                else:
                    mcp_tool["inputSchema"]["properties"][pname] = {
                        "type": "string",
                        "description": pname,
                    }
            tools.append(mcp_tool)
    except Exception as exc:
        log.warning(f"Failed to convert skill '{skill_name}' to MCP: {exc}")
    return tools


def generate_mcp_manifest() -> dict:
    """Generate a complete MCP tool manifest from all registered skills."""
    from core.skills import SKILL_REGISTRY

    manifest = {
        "protocolVersion": "2024-11-05",
        "serverInfo": {
            "name": "aimos-skills",
            "version": "4.3.0",
        },
        "capabilities": {
            "tools": {},
        },
        "tools": [],
    }

    for skill_name, skill_cls in sorted(SKILL_REGISTRY.items()):
        mcp_tools = skill_to_mcp_tools(skill_name, skill_cls)
        manifest["tools"].extend(mcp_tools)
        log.info(f"MCP: {skill_name} → {len(mcp_tools)} tools")

    # Also add system tools (these are registered in main.py, not in SKILL_REGISTRY)
    system_tools = [
        {
            "name": "system__current_time",
            "description": "Returns current date, time, and weekday",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "system__remember",
            "description": "Store a fact in long-term memory",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Short identifier"},
                    "value": {"type": "string", "description": "The fact to remember"},
                    "category": {"type": "string", "description": "semantic/episodic/procedural"},
                    "importance": {"type": "integer", "description": "1-10"},
                },
                "required": ["key", "value"],
            },
        },
        {
            "name": "system__recall",
            "description": "Search long-term memory by keyword",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                },
                "required": ["query"],
            },
        },
        {
            "name": "system__send_to_agent",
            "description": "Send a message to another AI agent",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "agent_name": {"type": "string", "description": "Target agent name"},
                    "message": {"type": "string", "description": "Message text"},
                },
                "required": ["agent_name", "message"],
            },
        },
        {
            "name": "system__send_telegram_message",
            "description": "Send a text message to a Telegram user",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Message text"},
                    "chat_id": {"type": "integer", "description": "Telegram chat ID (auto-detected if 0)"},
                },
                "required": ["text"],
            },
        },
        {
            "name": "system__send_voice_message",
            "description": "Generate and send a voice message via Telegram (Piper TTS)",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Text to speak"},
                    "voice": {"type": "string", "description": "Voice: de_female, de_male, en_female, en_male, tr_male"},
                },
                "required": ["text"],
            },
        },
    ]
    manifest["tools"].extend(system_tools)

    return manifest


def generate_a2a_agent_card(agent_name: str, agent_config: dict) -> dict:
    """Generate an A2A-compatible Agent Card (JSON-LD) for agent discovery.

    CR-121 Phase 4: A2A Protocol compatibility.
    """
    skills = agent_config.get("skills", [])
    character = agent_config.get("character", {})

    return {
        "@context": "https://a2a-protocol.org/context/v1",
        "@type": "AgentCard",
        "name": agent_name,
        "displayName": agent_config.get("display_name", agent_name),
        "description": character.get("description", ""),
        "version": "4.3.0",
        "provider": {
            "name": "AIMOS",
            "url": "https://github.com/philipp-fuchsenberger/AIMOS",
        },
        "capabilities": {
            "skills": skills,
            "languages": ["de", "en", "tr"],
            "interAgent": agent_config.get("inter_agent_messaging", True),
            "voiceInput": True,
            "voiceOutput": True,
        },
        "authentication": {
            "type": "none",  # local system, no auth needed
        },
        "endpoints": {
            "a2a": f"http://localhost:8080/api/agents/{agent_name}/a2a",
            "telegram": f"https://t.me/{agent_name}_bot",
        },
    }


# ── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="AIMOS MCP Bridge")
    parser.add_argument("--manifest", action="store_true", help="Generate MCP tool manifest")
    parser.add_argument("--agent-card", type=str, help="Generate A2A Agent Card for agent name")
    parser.add_argument("--output", type=str, help="Output file (default: stdout)")
    args = parser.parse_args()

    if args.manifest:
        manifest = generate_mcp_manifest()
        out = json.dumps(manifest, indent=2, ensure_ascii=False)
        if args.output:
            Path(args.output).write_text(out, encoding="utf-8")
            print(f"MCP manifest written to {args.output} ({len(manifest['tools'])} tools)")
        else:
            print(out)

    elif args.agent_card:
        import psycopg2
        import psycopg2.extras
        from core.config import Config
        conn = psycopg2.connect(
            host=Config.PG_HOST, port=Config.PG_PORT, dbname=Config.PG_DB,
            user=Config.PG_USER, password=Config.PG_PASSWORD,
            cursor_factory=psycopg2.extras.RealDictCursor,
        )
        cur = conn.cursor()
        cur.execute("SELECT config FROM agents WHERE name=%s", (args.agent_card,))
        row = cur.fetchone()
        conn.close()
        if not row:
            print(f"Agent '{args.agent_card}' not found", file=sys.stderr)
            sys.exit(1)
        cfg = row["config"] if isinstance(row["config"], dict) else json.loads(row["config"])
        card = generate_a2a_agent_card(args.agent_card, cfg)
        out = json.dumps(card, indent=2, ensure_ascii=False)
        if args.output:
            Path(args.output).write_text(out, encoding="utf-8")
            print(f"A2A Agent Card written to {args.output}")
        else:
            print(out)

    else:
        parser.print_help()
