import os

class Config:
    DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
    GUILD_ID = int(os.getenv("GUILD_ID")) if os.getenv("GUILD_ID") else None
    CHANNEL_ID = int(os.getenv("CHANNEL_ID")) if os.getenv("CHANNEL_ID") else None
    ADMIN_ROLE_ID = int(os.getenv("ADMIN_ROLE_ID")) if os.getenv("ADMIN_ROLE_ID") else None

    CONTAINER_NAME = os.getenv("CONTAINER_NAME")
    RCON_HOST = os.getenv("RCON_HOST", "127.0.0.1")
    RCON_PORT = int(os.getenv("RCON_PORT", 25575))
    RCON_PASSWORD = os.getenv("RCON_PASSWORD")

    STATUS_UPDATE_INTERVAL = int(os.getenv("STATUS_UPDATE_INTERVAL", 60))
