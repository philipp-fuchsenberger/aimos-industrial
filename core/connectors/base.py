import asyncio
import logging

class AIMOSConnector:
    """
    Base class for all AIMOS Connectors (Telegram, Spotify, etc.)
    Version: 4.1.0 (Shard & Connect)
    """
    def __init__(self, agent_id: str, config: dict):
        self.agent_id = agent_id
        self.config = config
        self.active = False
        self.logger = logging.getLogger(f"AIMOS.{self.agent_id}.connector")

    async def connect(self):
        """Establish connection / Authenticate with service."""
        raise NotImplementedError("Connectors must implement connect()")

    async def execute(self, action: str, params: dict = None):
        """The main interface for tools: e.g. 'play_song', 'send_message'."""
        raise NotImplementedError("Connectors must implement execute()")

    async def disconnect(self):
        """Graceful shutdown / cleanup."""
        self.active = False
        self.logger.info("Connector disconnected.")

