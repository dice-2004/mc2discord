import re
import threading
import asyncio
import logging
import time
from typing import Optional

import docker
from mcrcon import MCRcon
import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import Config

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mc2discord")


intents = discord.Intents.default()
intents.message_content = False
intents.messages = True


class MCDiscordBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)
        self.docker_client = docker.from_env()
        self.container = None
        self.log_thread: Optional[threading.Thread] = None
        self.loop_ready = asyncio.Event()

    async def setup_hook(self):
        # register commands to guild if provided for faster registration
        if Config.GUILD_ID:
            guild = discord.Object(id=Config.GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

        # background task starts after gateway is ready (on_ready)

    async def on_ready(self):
        logger.info(f"Logged in as {self.user}")
        self.loop_ready.set()
        if not status_updater.is_running():
            status_updater.start()
        # obtain container handle
        try:
            self.container = self.docker_client.containers.get(Config.CONTAINER_NAME)
            logger.info(f"Found container: {Config.CONTAINER_NAME}")
            self.start_log_thread()
        except Exception as e:
            logger.warning(f"Could not get container {Config.CONTAINER_NAME}: {e}")

    def start_log_thread(self):
        if self.log_thread and self.log_thread.is_alive():
            return
        t = threading.Thread(target=self._log_stream_loop, daemon=True)
        t.start()
        self.log_thread = t

    def _log_stream_loop(self):
        # blocking docker log stream running in a thread
        while True:
            try:
                if not self.container:
                    try:
                        self.container = self.docker_client.containers.get(Config.CONTAINER_NAME)
                    except Exception:
                        asyncio.run_coroutine_threadsafe(self._notify_channel(f"⚠️ コンテナ `{Config.CONTAINER_NAME}` を取得できませんでした。"), self.loop)
                        time.sleep(10)
                        continue

                try:
                    since_ts = int(time.time())
                    logs = self.container.logs(stream=True, follow=True, since=since_ts)
                except docker.errors.InvalidArgument:
                    logs = self.container.logs(stream=True, follow=True)
                for raw in logs:
                    try:
                        line = raw.decode('utf-8', errors='ignore')
                    except Exception:
                        line = str(raw)
                    self._handle_log_line(line)
            except Exception as e:
                logger.exception("Log stream error")
                time.sleep(5)

    def _handle_log_line(self, line: str):
        logger.debug(line.strip())
        event = parse_player_event(line)
        if event:
            player, kind = event
            if kind == "join":
                asyncio.run_coroutine_threadsafe(self._notify_channel(f"🟢 `{player}` がサーバーに参加しました"), self.loop)
            else:
                asyncio.run_coroutine_threadsafe(self._notify_channel(f"🔴 `{player}` がサーバーから退出しました"), self.loop)
            return

        if is_server_ready_line(line):
            asyncio.run_coroutine_threadsafe(self._notify_channel("✅ サーバーの起動が完了しました。"), self.loop)

    async def _notify_channel(self, message: str):
        if not Config.CHANNEL_ID:
            logger.warning("CHANNEL_ID not set, cannot send message")
            return
        channel = self.get_channel(Config.CHANNEL_ID)
        if not channel:
            try:
                channel = await self.fetch_channel(Config.CHANNEL_ID)
            except Exception as e:
                logger.warning(f"Cannot fetch channel: {e}")
                return
        try:
            await channel.send(message)
        except Exception as e:
            logger.exception("Failed to send message")

    def has_admin(self, interaction: discord.Interaction) -> bool:
        # check role or administrator perm
        if interaction.user.guild_permissions.administrator:
            return True
        if Config.ADMIN_ROLE_ID:
            return any(r.id == Config.ADMIN_ROLE_ID for r in interaction.user.roles)
        return False


def parse_player_event(line: str) -> Optional[tuple[str, str]]:
    joined = re.search(r"(\w+) joined the game", line)
    if joined:
        return joined.group(1), "join"

    left = re.search(r"(\w+) left the game", line)
    if left:
        return left.group(1), "leave"

    return None


def is_server_ready_line(line: str) -> bool:
    return ("Done (" in line and "For help, type \"help\"" in line) or line.strip().endswith("Done")


def build_presence(status: str, player_count: Optional[int] = None) -> discord.Activity:
    if status == 'running':
        suffix = f" | {player_count}人プレイ中" if player_count is not None else ""
        return discord.Activity(type=discord.ActivityType.playing, name=f"🟢 稼働中{suffix}")
    if status in ('exited', 'dead'):
        return discord.Activity(type=discord.ActivityType.playing, name="🔴 停止中")
    return discord.Activity(type=discord.ActivityType.playing, name="⚪ 状態不明")


bot = MCDiscordBot()

mc = app_commands.Group(name="mc", description="Minecraft server controls")


@mc.command(name="start", description="Start the Minecraft container")
async def mc_start(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not bot.has_admin(interaction):
        await interaction.followup.send("権限がありません。", ephemeral=True)
        return
    try:
        container = bot.docker_client.containers.get(Config.CONTAINER_NAME)
        if container.status == 'running':
            await interaction.followup.send("既に稼働中です。", ephemeral=True)
            return
        container.start()
        await interaction.followup.send("サーバーを起動しました。", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"起動に失敗しました: {e}", ephemeral=True)


@mc.command(name="stop", description="Stop the Minecraft server (save then stop)")
async def mc_stop(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not bot.has_admin(interaction):
        await interaction.followup.send("権限がありません。", ephemeral=True)
        return
    try:
        container = bot.docker_client.containers.get(Config.CONTAINER_NAME)
        if container.status != 'running':
            await interaction.followup.send("サーバーは稼働していません。", ephemeral=True)
            return
        # run save-all via RCON
        try:
            with MCRcon(Config.RCON_HOST, Config.RCON_PASSWORD, port=Config.RCON_PORT) as m:
                m.command("save-all")
        except Exception as e:
            logger.warning(f"RCON save-all failed: {e}")
        container.stop()
        await interaction.followup.send("サーバーを停止しました。", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"停止に失敗しました: {e}", ephemeral=True)


@mc.command(name="restart", description="Restart the Minecraft server (announce, save, restart)")
async def mc_restart(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not bot.has_admin(interaction):
        await interaction.followup.send("権限がありません。", ephemeral=True)
        return
    try:
        container = bot.docker_client.containers.get(Config.CONTAINER_NAME)
        if container.status == 'running':
            try:
                with MCRcon(Config.RCON_HOST, Config.RCON_PASSWORD, port=Config.RCON_PORT) as m:
                    m.command('say サーバーを再起動します。数秒後に切断されます。')
                    m.command('save-all')
            except Exception as e:
                logger.warning(f"RCON announce/save failed: {e}")
        container.restart()
        await interaction.followup.send("サーバーを再起動しました。", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"再起動に失敗しました: {e}", ephemeral=True)


@mc.command(name="status", description="Show server status and players")
async def mc_status(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        container = bot.docker_client.containers.get(Config.CONTAINER_NAME)
        status = container.status
    except Exception as e:
        await interaction.followup.send(f"コンテナ取得エラー: {e}")
        return

    player_list = "(不明)"
    player_count = 0
    try:
        with MCRcon(Config.RCON_HOST, Config.RCON_PASSWORD, port=Config.RCON_PORT) as m:
            r = m.command('list')
            # parse: "There are X of a max of Y players online: name1, name2"
            mres = re.search(r"There are (\d+) of a max of (\d+) players online:?\s*(.*)", r)
            if mres:
                player_count = int(mres.group(1))
                names = mres.group(3).strip()
                player_list = names if names else "(なし)"
            else:
                player_list = r
    except Exception as e:
        logger.warning(f"RCON list failed: {e}")

    embed = discord.Embed(title="Minecraft サーバー状態")
    embed.add_field(name="コンテナ状態", value=status, inline=False)
    embed.add_field(name="プレイヤー数", value=str(player_count), inline=False)
    embed.add_field(name="プレイヤー一覧", value=player_list, inline=False)

    await interaction.followup.send(embed=embed)


@tasks.loop(seconds=Config.STATUS_UPDATE_INTERVAL)
async def status_updater():
    # update bot activity
    if not bot.is_ready() or bot.ws is None:
        return

    try:
        container = bot.docker_client.containers.get(Config.CONTAINER_NAME)
        status = container.status
    except Exception:
        status = 'unknown'

    player_count = None
    try:
        with MCRcon(Config.RCON_HOST, Config.RCON_PASSWORD, port=Config.RCON_PORT) as m:
            r = m.command('list')
            mres = re.search(r"There are (\d+) of a max of (\d+) players online", r)
            if mres:
                player_count = int(mres.group(1))
    except Exception:
        pass

    activity = build_presence(status, player_count)

    try:
        await bot.change_presence(activity=activity)
    except Exception:
        logger.exception("Failed to update presence")


async def main():
    token = Config.DISCORD_TOKEN
    if not token:
        raise RuntimeError("DISCORD_TOKEN is not set")
    await bot.start(token)


bot.tree.add_command(mc)


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down")
