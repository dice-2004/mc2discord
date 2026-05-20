import re
import threading
import asyncio
import logging
import time
import os
from typing import Optional
from functools import partial

import docker
from concurrent.futures import ProcessPoolExecutor
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
        self.event_thread: Optional[threading.Thread] = None
        self.loop_ready = asyncio.Event()
        self.last_players: set[str] = set()

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
        if not player_notifier.is_running():
            player_notifier.start()
        # start docker events watcher
        if not (self.event_thread and self.event_thread.is_alive()):
            et = threading.Thread(target=self._docker_events_loop, daemon=True)
            et.start()
            self.event_thread = et
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
            # attempt to ensure container reference
            if not self.container:
                try:
                    self.container = self.docker_client.containers.get(Config.CONTAINER_NAME)
                except Exception:
                    asyncio.run_coroutine_threadsafe(self._notify_channel(f"⚠️ コンテナ `{Config.CONTAINER_NAME}` を取得できませんでした。"), self.loop)
                    time.sleep(5)
                    continue
                    # try streaming logs with exponential backoff on failures
            backoff = 1
            while True:
                try:
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
                    # if logs iterator ends normally, break to re-resolve
                    break
                except docker.errors.NotFound as e:
                    logger.warning(f"Container disappeared, will re-resolve: {e}")
                    self.container = None
                    time.sleep(2)
                    break
                except Exception:
                    logger.exception("Log stream error")
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 30)
                    continue

    def _docker_events_loop(self):
        # listen to docker events and react to container lifecycle
        try:
            for event in self.docker_client.events(decode=True):
                try:
                    actor = event.get('Actor', {})
                    attrs = actor.get('Attributes', {})
                    name = attrs.get('name')
                    status = event.get('status')
                    if name != Config.CONTAINER_NAME:
                        continue
                    if status in ('start', 'restart'):
                        asyncio.run_coroutine_threadsafe(self._notify_channel(f"🟢 サーバーコンテナ `{name}` が起動しました"), self.loop)
                        try:
                            self.container = self.docker_client.containers.get(Config.CONTAINER_NAME)
                            self.start_log_thread()
                        except Exception:
                            logger.warning("Failed to re-resolve container after start event")
                    elif status in ('die', 'stop', 'destroy'):
                        asyncio.run_coroutine_threadsafe(self._notify_channel(f"🔴 サーバーコンテナ `{name}` が停止しました"), self.loop)
                        self.container = None
                except Exception:
                    logger.exception("Error handling docker event")
        except Exception:
            logger.exception("Docker events loop terminated")

    def _handle_log_line(self, line: str):
        line = line.rstrip()
        logger.debug(line)
        event = parse_player_event(line)
        if event:
            player, kind = event
            logger.info(f"Detected player {kind}: {player}")
            if kind == "join":
                asyncio.run_coroutine_threadsafe(self._notify_channel(f"🟢 `{player}` がサーバーに参加しました"), self.loop)
            else:
                asyncio.run_coroutine_threadsafe(self._notify_channel(f"🔴 `{player}` がサーバーから退出しました"), self.loop)
            return

        if is_server_ready_line(line):
            asyncio.run_coroutine_threadsafe(self._notify_channel("✅ サーバーの起動が完了しました。"), self.loop)
            return

        relay_message = classify_relayable_log_line(line)
        if relay_message:
            asyncio.run_coroutine_threadsafe(self._notify_log_channel(relay_message), self.loop)

    async def _notify_channel(self, message: str):
        await self._send_message_to_channel(Config.CHANNEL_ID, message)

    async def _notify_log_channel(self, message: str):
        await self._send_message_to_channel(Config.CHANNEL_ID, message)

    async def _send_message_to_channel(self, channel_id: Optional[int], message: str):
        if not channel_id:
            logger.warning("CHANNEL_ID not set, cannot send message")
            return
        channel = self.get_channel(channel_id)
        if not channel:
            try:
                channel = await self.fetch_channel(channel_id)
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
    normalized = line.strip()

    # Vanilla / Paper style
    joined = re.search(r"(?:\] )?(?:<[^>]+> )?([^\s]+) joined the game", normalized)
    if joined:
        return joined.group(1), "join"

    left = re.search(r"(?:\] )?(?:<[^>]+> )?([^\s]+) left the game", normalized)
    if left:
        return left.group(1), "leave"

    # Some server variants emit login/logout related lines instead of the exact vanilla message.
    login = re.search(r"(?:\] )?(?:<[^>]+> )?([^\s]+) logged in", normalized, re.IGNORECASE)
    if login:
        return login.group(1), "join"

    logout = re.search(r"(?:\] )?(?:<[^>]+> )?([^\s]+) lost connection", normalized, re.IGNORECASE)
    if logout:
        return logout.group(1), "leave"

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


def strip_minecraft_log_prefix(line: str) -> str:
    cleaned = re.sub(r"^\[[0-9:.,]+\]\s*\[[^\]]+\]:\s*", "", line)
    cleaned = re.sub(r"^\[[^\]]+\]:\s*", "", cleaned)
    return cleaned.strip()


def truncate_for_discord(text: str, limit: int = 1800) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def classify_relayable_log_line(line: str) -> Optional[str]:
    cleaned = strip_minecraft_log_prefix(line)
    if not cleaned:
        return None

    if parse_player_event(cleaned) or is_server_ready_line(cleaned):
        return None

    chat = re.search(r"^<([^>]+)>\s+(.+)$", cleaned)
    if chat:
        player = chat.group(1)
        message = chat.group(2).strip()
        return truncate_for_discord(f"💬 `{player}`: {message}")

    advancement = re.search(
        r"^([^\s].*?)\s+(?:has\s+)?(?:made|completed|reached)\s+the\s+(?:advancement|challenge)\s+\[(.+?)\]$",
        cleaned,
        re.IGNORECASE,
    )
    if advancement:
        player = advancement.group(1).strip()
        title = advancement.group(2).strip()
        return truncate_for_discord(f"🏆 `{player}` が実績 `{title}` を解除しました")

    death = re.search(
        r"^([^\s].*?)\s+(was|fell|drowned|burned|burnt|blown up|shot|slain|killed|froze|suffocated|impaled|hit|tried)\b.*$",
        cleaned,
        re.IGNORECASE,
    )
    if death and any(word in cleaned.lower() for word in (" was ", " fell ", " drowned", " burned", " burnt", " blown up", " shot", " slain", " killed", " froze", " suffocated", " impaled", " hit the ground", " tried to swim in lava")):
        return truncate_for_discord(f"☠️ `{cleaned}`")

    if Config.FORWARD_ALL_LOG_LINES:
        return truncate_for_discord(f"📝 {cleaned}")

    return None


def parse_rcon_player_names(list_response: str) -> set[str]:
    # Example: There are 2 of a max of 20 players online: Steve, Alex
    m = re.search(r"There are\s+(\d+)\s+of a max of\s+\d+\s+players online:?\s*(.*)", list_response)
    if not m:
        return set()
    count = int(m.group(1))
    names_part = (m.group(2) or "").strip()
    if count == 0 or not names_part:
        return set()
    return {n.strip() for n in names_part.split(',') if n.strip()}


def build_rcon_candidates() -> list[tuple[str, int]]:
    """Build deterministic RCON endpoints from Docker metadata.

    Preference:
    1) target container IP on a network shared with this bot container
    2) target container DNS name (only when a shared network exists)
    3) host-published port reachable from container (host.docker.internal / gateway)
    4) explicit env fallback (RCON_HOST:RCON_PORT)
    """
    candidates: list[tuple[str, int]] = []
    bot_networks = {}

    try:
        client = docker.from_env()
        target = client.containers.get(Config.CONTAINER_NAME)
        target_networks = target.attrs.get('NetworkSettings', {}).get('Networks', {})

        bot_container_id = os.getenv("HOSTNAME")
        if bot_container_id:
            try:
                bot_container = client.containers.get(bot_container_id)
                bot_networks = bot_container.attrs.get('NetworkSettings', {}).get('Networks', {})
            except Exception:
                bot_networks = {}

        shared_network = None
        for net_name in target_networks:
            if net_name in bot_networks:
                shared_network = net_name
                break

        if shared_network:
            shared_ip = target_networks.get(shared_network, {}).get('IPAddress')
            if shared_ip:
                candidates.append((shared_ip, Config.RCON_PORT))
            candidates.append((Config.CONTAINER_NAME, Config.RCON_PORT))

        ports = target.attrs.get('NetworkSettings', {}).get('Ports', {})
        key = f"{Config.RCON_PORT}/tcp"
        if key in ports and ports[key]:
            host_ip = ports[key][0].get('HostIp')
            host_port = int(ports[key][0].get('HostPort'))
            if host_ip and host_ip not in ('0.0.0.0', '::'):
                candidates.append((host_ip, host_port))
            candidates.append(('host.docker.internal', host_port))
            for net in bot_networks.values():
                gw = net.get('Gateway')
                if gw:
                    candidates.append((gw, host_port))
    except Exception:
        pass

    candidates.append((Config.RCON_HOST, Config.RCON_PORT))

    uniq: list[tuple[str, int]] = []
    seen = set()
    for host, port in candidates:
        k = (host, port)
        if k in seen:
            continue
        seen.add(k)
        uniq.append((host, port))
    return uniq


def rcon_execute_with_retries(command: str, retries: int = 4, return_result: bool = False, timeout: int = 5):
    delay = 1
    last_exc = None
    for attempt in range(retries):
        for host, port in build_rcon_candidates():
            try:
                logger.info(f"RCON try connecting to {host}:{port} (attempt {attempt+1})")
                with MCRcon(host, Config.RCON_PASSWORD, port=port, timeout=timeout) as m:
                    res = m.command(command)
                    if return_result:
                        return res
                    return None
            except Exception as e:
                last_exc = e
                logger.warning(f"RCON host {host}:{port} attempt failed: {e}")
                # try next candidate
                continue

        # if all candidates failed, wait and retry
        time.sleep(delay)
        delay = min(delay * 2, 30)
    raise last_exc


# process pool for RCON calls (avoid signal() in non-main threads)
process_pool = ProcessPoolExecutor(max_workers=2)


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
        # run save-all via RCON (offload blocking call to separate process)
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(process_pool, partial(rcon_execute_with_retries, 'save-all'))
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
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(process_pool, partial(rcon_execute_with_retries, 'say サーバーを再起動します。数秒後に切断されます。'))
                await loop.run_in_executor(process_pool, partial(rcon_execute_with_retries, 'save-all'))
            except Exception as e:
                logger.warning(f"RCON announce/save failed: {e}")
        container.restart()
        await interaction.followup.send("サーバーを再起動しました。", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"再起動に失敗しました: {e}", ephemeral=True)


@mc.command(name="simulate", description="Simulate a log event for testing (join/leave/ready)")
async def mc_simulate(interaction: discord.Interaction, event: str, player: str = ""):
    await interaction.response.defer(ephemeral=True)
    if not bot.has_admin(interaction):
        await interaction.followup.send("権限がありません。", ephemeral=True)
        return
    if event not in ("join", "leave", "ready"):
        await interaction.followup.send("event は join|leave|ready のいずれかを指定してください。", ephemeral=True)
        return
    if event == "join":
        line = f"{player} joined the game"
    elif event == "leave":
        line = f"{player} left the game"
    else:
        line = 'Done'
    # call handler directly
    bot._handle_log_line(line)
    await interaction.followup.send(f"Simulated: {line}", ephemeral=True)


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
        loop = asyncio.get_running_loop()
        r = await loop.run_in_executor(process_pool, partial(rcon_execute_with_retries, 'list', return_result=True))
        if not isinstance(r, str):
            raise RuntimeError(f"unexpected RCON response type: {type(r)}")
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
        loop = asyncio.get_running_loop()
        r = await loop.run_in_executor(process_pool, partial(rcon_execute_with_retries, 'list', return_result=True))
        if not isinstance(r, str):
            raise RuntimeError(f"unexpected RCON response type: {type(r)}")
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


@tasks.loop(seconds=Config.PLAYER_NOTIFY_INTERVAL)
async def player_notifier():
    # Root-cause fix: detect join/leave via RCON list diff instead of fragile log parsing.
    if not bot.is_ready() or bot.ws is None:
        return

    try:
        loop = asyncio.get_running_loop()
        r = await loop.run_in_executor(process_pool, partial(rcon_execute_with_retries, 'list', return_result=True))
        if not isinstance(r, str):
            return

        current_players = parse_rcon_player_names(r)
        joined = current_players - bot.last_players
        left = bot.last_players - current_players

        for name in sorted(joined):
            await bot._notify_channel(f"🟢 `{name}` がサーバーに参加しました")
        for name in sorted(left):
            await bot._notify_channel(f"🔴 `{name}` がサーバーから退出しました")

        bot.last_players = current_players
    except Exception as e:
        logger.warning(f"player_notifier failed: {e}")


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
