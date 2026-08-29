"""
Discord Giveaway Bot - Single File Edition (English)
Built with discord.py 2.x | Prefix: ?

Main Features:
  - Start/End/Reroll/Cancel giveaways
  - Persistent join button
  - "Lock Winner" system - guarantees a specific user will win when the giveaway ends
    (locked-winner list is only ever shown to hosts/admins, never in the public embed
    or announcement, so entrants just see a normal winner announcement)
  - Optional required-role gate to join
  - Manually add/remove participants
  - Admin Control Panel: run ?gpanel to create a private channel with buttons and
    forms so you can start/end/reroll/cancel/lock/unlock without typing commands
  - Persists state to JSON file
  - Mini web server (Flask) for Render Web Service keep-alive

Environment Variables:
  DISCORD_TOKEN   = your bot token (required)
  PREFIX          = command prefix (default "?")
  PORT            = port for keep-alive server (Render sets this automatically)
"""

import os
import re
import json
import random
import threading
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands, tasks
from flask import Flask

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
TOKEN = os.environ.get("DISCORD_TOKEN", "")
PREFIX = os.environ.get("PREFIX", "?")
DATA_FILE = "giveaways.json"
EMBED_COLOR = 0x2ECC71
EMBED_COLOR_END = 0x992D22
GIFT_EMOJI = "🎉"
PANEL_CHANNEL_NAME = "giveaway-control"

# ---------------------------------------------------------------------------
# KEEP-ALIVE WEB SERVER (for Render Web Service)
# ---------------------------------------------------------------------------
app = Flask(__name__)


@app.route("/")
def home():
    return "Giveaway bot is alive!"


def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)


def keep_alive():
    t = threading.Thread(target=run_flask)
    t.daemon = True
    t.start()


# ---------------------------------------------------------------------------
# STORAGE HELPERS
# ---------------------------------------------------------------------------
def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# giveaways: { message_id(str): {...} }
giveaways = load_json(DATA_FILE, {})


def save_giveaways():
    save_json(DATA_FILE, giveaways)


# ---------------------------------------------------------------------------
# TIME PARSING
# ---------------------------------------------------------------------------
TIME_REGEX = re.compile(r"^(\d+)([smhd])$")


def parse_duration(text: str):
    """Parse '10m' '2h' '1d' '30s' -> seconds (int)"""
    m = TIME_REGEX.match(text.strip().lower())
    if not m:
        return None
    value, unit = int(m.group(1)), m.group(2)
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    return value * mult


# ---------------------------------------------------------------------------
# BOT SETUP
# ---------------------------------------------------------------------------
intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)


def has_host_perms(member: discord.Member) -> bool:
    return bool(member and member.guild_permissions.manage_guild)


# ---------------------------------------------------------------------------
# GIVEAWAY BUTTON VIEW (public join button)
# ---------------------------------------------------------------------------
class GiveawayView(discord.ui.View):
    """Persistent join button shown on the giveaway message."""

    def __init__(self, message_id: int):
        super().__init__(timeout=None)
        self.message_id = message_id
        self.join_button.custom_id = f"gw_join_{message_id}"

    @discord.ui.button(label="Join", emoji=GIFT_EMOJI, style=discord.ButtonStyle.green)
    async def join_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        mid = str(self.message_id)
        gw = giveaways.get(mid)

        if gw is None or gw.get("ended"):
            await interaction.response.send_message(
                "❌ This giveaway has already ended.", ephemeral=True
            )
            return

        user = interaction.user

        role_id = gw.get("required_role_id")
        if role_id:
            member = interaction.guild.get_member(user.id) or await interaction.guild.fetch_member(user.id)
            if not member or role_id not in [r.id for r in member.roles]:
                role = interaction.guild.get_role(role_id)
                rname = role.mention if role else "the required role"
                await interaction.response.send_message(
                    f"❌ You need the role {rname} to join.", ephemeral=True,
                )
                return

        participants = gw["participants"]
        if user.id in participants:
            participants.remove(user.id)
            save_giveaways()
            await interaction.response.send_message("↩️ You left the giveaway.", ephemeral=True)
        else:
            participants.append(user.id)
            save_giveaways()
            await interaction.response.send_message(
                "✅ You successfully joined the giveaway!", ephemeral=True,
            )

        await update_giveaway_embed(gw, mid)


# ---------------------------------------------------------------------------
# EMBED BUILDERS
# ---------------------------------------------------------------------------
def build_embed(gw: dict, ended: bool = False) -> discord.Embed:
    """Public-facing embed. Locked-winner info is intentionally NEVER shown here."""
    desc_lines = [
        f"🎁 **Prize:** {gw['prize']}",
        f"👤 **Host:** <@{gw['host_id']}>",
        f"🏆 **Winners:** {gw['winners']}",
        f"👥 **Entries:** {len(gw['participants'])}",
    ]
    if gw.get("required_role_id"):
        desc_lines.append(f"🔑 **Required role:** <@&{gw['required_role_id']}>")

    if ended:
        desc_lines.append("⏰ **Status:** Ended")
        title = f"{GIFT_EMOJI} GIVEAWAY ENDED {GIFT_EMOJI}"
        color = EMBED_COLOR_END
    else:
        desc_lines.append(f"⏰ **Ends:** <t:{gw['end_time']}:R> (<t:{gw['end_time']}:f>)")
        title = f"{GIFT_EMOJI} GIVEAWAY {GIFT_EMOJI}"
        color = EMBED_COLOR

    embed = discord.Embed(title=title, description="\n".join(desc_lines), color=color)
    embed.set_footer(text=f"ID: {gw.get('_id', '')}")
    return embed


def build_admin_embed(gw: dict) -> discord.Embed:
    """Host-only embed. This is the ONLY place locked winners are ever shown."""
    embed = build_embed(gw, ended=gw.get("ended", False))
    locked = gw.get("locked_winners", [])
    if locked:
        mentions = ", ".join(f"<@{u}>" for u in locked)
        embed.add_field(name="🔒 Locked winner(s) (admin-only)", value=mentions, inline=False)
    else:
        embed.add_field(name="🔒 Locked winner(s) (admin-only)", value="None", inline=False)
    return embed


async def update_giveaway_embed(gw: dict, mid: str):
    try:
        channel = bot.get_channel(gw["channel_id"]) or await bot.fetch_channel(gw["channel_id"])
        msg = await channel.fetch_message(int(mid))
        gw["_id"] = mid
        await msg.edit(embed=build_embed(gw, ended=gw.get("ended", False)))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# GIVEAWAY LOGIC
# ---------------------------------------------------------------------------
def pick_winners(gw: dict, count: int = None):
    """Locked users always win; remaining slots are picked randomly."""
    target = count or gw["winners"]
    locked = list(gw.get("locked_winners", []))
    pool = [uid for uid in gw["participants"] if uid not in locked]

    remaining_slots = max(0, target - len(locked))
    random_winners = random.sample(pool, min(remaining_slots, len(pool))) if pool else []

    return locked + random_winners


async def finish_giveaway(mid: str, gw: dict, cancelled: bool = False):
    gw["ended"] = True
    guild = bot.get_guild(gw["guild_id"])
    channel = bot.get_channel(gw["channel_id"]) or (guild and guild.get_channel(gw["channel_id"]))

    try:
        msg = await channel.fetch_message(int(mid))
    except Exception:
        msg = None

    if cancelled:
        if msg:
            embed = build_embed(gw, ended=True)
            embed.description += "\n\n❌ **Cancelled**"
            await msg.edit(embed=embed, view=None)
        save_giveaways()
        return

    winners = pick_winners(gw)
    gw["last_winners"] = winners
    save_giveaways()

    if msg:
        await msg.edit(embed=build_embed(gw, ended=True), view=None)

    if not winners:
        if channel:
            await channel.send(f"😢 No winner for **{gw['prize']}** (no valid entries).")
        return

    # Same announcement regardless of locked vs random winners - entrants can't tell the difference.
    mentions = ", ".join(f"<@{w}>" for w in winners)
    if channel:
        await channel.send(
            f"🎉 Congratulations {mentions}! You won **{gw['prize']}** (ID: `{mid}`)"
        )


@tasks.loop(seconds=10)
async def check_giveaways():
    now = int(datetime.now(timezone.utc).timestamp())
    for mid, gw in list(giveaways.items()):
        if not gw.get("ended") and gw["end_time"] <= now:
            gw["_id"] = mid
            await finish_giveaway(mid, gw)


@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} (prefix: {PREFIX})")
    for mid, gw in giveaways.items():
        if not gw.get("ended"):
            bot.add_view(GiveawayView(int(mid)))
    bot.add_view(PanelView())
    if not check_giveaways.is_running():
        check_giveaways.start()


# ---------------------------------------------------------------------------
# PERMISSION CHECK
# ---------------------------------------------------------------------------
def is_host():
    async def predicate(ctx):
        if ctx.author.guild_permissions.manage_guild:
            return True
        await ctx.send("❌ You need the `Manage Server` permission to use this command.")
        return False
    return commands.check(predicate)


def get_giveaway_or_none(mid: str):
    return giveaways.get(mid)


async def create_giveaway(channel, host_id, guild_id, prize, winners_count, seconds, required_role_id=None):
    end_time = int((datetime.now(timezone.utc) + timedelta(seconds=seconds)).timestamp())
    placeholder = discord.Embed(
        title=f"{GIFT_EMOJI} GIVEAWAY {GIFT_EMOJI}", description="Setting up...", color=EMBED_COLOR
    )
    msg = await channel.send(embed=placeholder)

    gw = {
        "guild_id": guild_id,
        "channel_id": channel.id,
        "host_id": host_id,
        "prize": prize,
        "winners": winners_count,
        "end_time": end_time,
        "participants": [],
        "locked_winners": [],
        "required_role_id": required_role_id,
        "ended": False,
        "_id": str(msg.id),
    }
    giveaways[str(msg.id)] = gw
    save_giveaways()

    view = GiveawayView(msg.id)
    bot.add_view(view)
    await msg.edit(embed=build_embed(gw), view=view)
    return msg, gw


# ---------------------------------------------------------------------------
# COMMANDS: GIVEAWAY LIFECYCLE
# ---------------------------------------------------------------------------
@bot.command(name="gstart")
@is_host()
async def gstart(ctx, duration: str, winners: int, *, prize: str):
    """?gstart <duration e.g. 10m,2h,1d> <winner count> <prize> [@role]"""
    seconds = parse_duration(duration)
    if seconds is None:
        await ctx.send("❌ Invalid time format. Use e.g. `10s` `5m` `2h` `1d`.")
        return
    if winners < 1:
        await ctx.send("❌ Winner count must be greater than 0.")
        return

    required_role_id = None
    if ctx.message.role_mentions:
        required_role_id = ctx.message.role_mentions[0].id
        prize = re.sub(r"<@&\d+>", "", prize).strip()

    if not prize:
        await ctx.send("❌ Please specify a prize.")
        return

    await create_giveaway(ctx.channel, ctx.author.id, ctx.guild.id, prize, winners, seconds, required_role_id)
    try:
        await ctx.message.delete()
    except Exception:
        pass


@bot.command(name="gend")
@is_host()
async def gend(ctx, message_id: str):
    """?gend <message_id> - End a giveaway immediately"""
    gw = get_giveaway_or_none(message_id)
    if not gw or gw.get("ended"):
        await ctx.send("❌ No active giveaway found with that ID.")
        return
    gw["_id"] = message_id
    await finish_giveaway(message_id, gw)
    await ctx.send(f"✅ Giveaway `{message_id}` has ended.")


@bot.command(name="gcancel")
@is_host()
async def gcancel(ctx, message_id: str):
    """?gcancel <message_id> - Cancel without picking a winner"""
    gw = get_giveaway_or_none(message_id)
    if not gw or gw.get("ended"):
        await ctx.send("❌ No active giveaway found with that ID.")
        return
    gw["_id"] = message_id
    await finish_giveaway(message_id, gw, cancelled=True)
    await ctx.send(f"🗑️ Giveaway `{message_id}` was cancelled.")


@bot.command(name="greroll")
@is_host()
async def greroll(ctx, message_id: str, count: int = None):
    """?greroll <message_id> [count] - Reroll winners (locked winners still win)"""
    gw = get_giveaway_or_none(message_id)
    if not gw or not gw.get("ended"):
        await ctx.send("❌ You can only reroll a giveaway that has already ended.")
        return
    winners = pick_winners(gw, count)
    if not winners:
        await ctx.send("😢 No valid entries to pick from.")
        return
    gw["last_winners"] = winners
    save_giveaways()
    mentions = ", ".join(f"<@{w}>" for w in winners)
    await ctx.send(f"🔄 Rerolled! Congratulations {mentions}, you won **{gw['prize']}**")


@bot.command(name="glist")
async def glist(ctx):
    """?glist - List active giveaways"""
    active = [
        (mid, gw) for mid, gw in giveaways.items()
        if gw["guild_id"] == ctx.guild.id and not gw.get("ended")
    ]
    if not active:
        await ctx.send("📭 There are no active giveaways.")
        return
    embed = discord.Embed(title="📋 Active Giveaways", color=EMBED_COLOR)
    for mid, gw in active:
        embed.add_field(
            name=f"🎁 {gw['prize']} (ID: {mid})",
            value=f"Winners: {gw['winners']} | Entries: {len(gw['participants'])} | Ends: <t:{gw['end_time']}:R>",
            inline=False,
        )
    await ctx.send(embed=embed)


@bot.command(name="ginfo")
async def ginfo(ctx, message_id: str):
    """?ginfo <message_id> - View giveaway details (public view, no locked-winner info)"""
    gw = get_giveaway_or_none(message_id)
    if not gw:
        await ctx.send("❌ No giveaway found with that ID.")
        return
    gw["_id"] = message_id
    await ctx.send(embed=build_embed(gw, ended=gw.get("ended", False)))


@bot.command(name="gadmininfo")
@is_host()
async def gadmininfo(ctx, message_id: str):
    """?gadmininfo <message_id> - View giveaway details including locked winners (host-only)"""
    gw = get_giveaway_or_none(message_id)
    if not gw:
        await ctx.send("❌ No giveaway found with that ID.")
        return
    gw["_id"] = message_id
    await ctx.send(embed=build_admin_embed(gw))


# ---------------------------------------------------------------------------
# COMMANDS: PARTICIPANT MANAGEMENT
# ---------------------------------------------------------------------------
@bot.command(name="gadd")
@is_host()
async def gadd(ctx, message_id: str, member: discord.Member):
    """?gadd <message_id> @user - Manually add a participant"""
    gw = get_giveaway_or_none(message_id)
    if not gw or gw.get("ended"):
        await ctx.send("❌ No active giveaway found with that ID.")
        return
    if member.id in gw["participants"]:
        await ctx.send(f"⚠️ {member.mention} has already joined.")
        return
    gw["participants"].append(member.id)
    save_giveaways()
    gw["_id"] = message_id
    await update_giveaway_embed(gw, message_id)
    await ctx.send(f"✅ Added {member.mention} to the giveaway.")


@bot.command(name="gremove")
@is_host()
async def gremove(ctx, message_id: str, member: discord.Member):
    """?gremove <message_id> @user - Remove a participant"""
    gw = get_giveaway_or_none(message_id)
    if not gw:
        await ctx.send("❌ No giveaway found with that ID.")
        return
    if member.id not in gw["participants"]:
        await ctx.send(f"⚠️ {member.mention} is not in the entry list.")
        return
    gw["participants"].remove(member.id)
    save_giveaways()
    gw["_id"] = message_id
    await update_giveaway_embed(gw, message_id)
    await ctx.send(f"🗑️ Removed {member.mention} from the giveaway.")


# ---------------------------------------------------------------------------
# COMMANDS: LOCK WINNER SYSTEM (host-only, never shown to entrants)
# ---------------------------------------------------------------------------
@bot.command(name="glock")
@is_host()
async def glock(ctx, message_id: str, member: discord.Member):
    """?glock <message_id> @user - Lock this user as a guaranteed winner (host-only, not disclosed publicly)"""
    gw = get_giveaway_or_none(message_id)
    if not gw or gw.get("ended"):
        await ctx.send("❌ No active giveaway found with that ID.")
        return

    gw.setdefault("locked_winners", [])
    if member.id in gw["locked_winners"]:
        await ctx.send(f"⚠️ {member.mention} is already locked as a winner.")
        return

    gw["locked_winners"].append(member.id)
    if member.id not in gw["participants"]:
        gw["participants"].append(member.id)
    save_giveaways()
    gw["_id"] = message_id
    await update_giveaway_embed(gw, message_id)

    try:
        await ctx.author.send(f"🔒 Locked {member.mention} as a guaranteed winner of giveaway `{message_id}`.")
        await ctx.message.delete()
    except Exception:
        await ctx.send(f"🔒 Locked {member.mention} as a guaranteed winner.", delete_after=5)


@bot.command(name="gunlock")
@is_host()
async def gunlock(ctx, message_id: str, member: discord.Member):
    """?gunlock <message_id> @user - Remove a locked winner (host-only)"""
    gw = get_giveaway_or_none(message_id)
    if not gw:
        await ctx.send("❌ No giveaway found with that ID.")
        return

    locked = gw.get("locked_winners", [])
    if member.id not in locked:
        await ctx.send(f"⚠️ {member.mention} is not currently locked as a winner.")
        return

    locked.remove(member.id)
    save_giveaways()
    gw["_id"] = message_id
    await update_giveaway_embed(gw, message_id)
    await ctx.send(f"🔓 Unlocked {member.mention}.", delete_after=5)


@bot.command(name="glockedlist")
@is_host()
async def glockedlist(ctx, message_id: str):
    """?glockedlist <message_id> - Show locked winners for this giveaway (host-only)"""
    gw = get_giveaway_or_none(message_id)
    if not gw:
        await ctx.send("❌ No giveaway found with that ID.")
        return
    locked = gw.get("locked_winners", [])
    if not locked:
        await ctx.send("📭 This giveaway has no locked winners.")
        return
    lines = "\n".join(f"🔒 <@{uid}>" for uid in locked)
    embed = discord.Embed(title="Locked Winners (host-only)", description=lines, color=EMBED_COLOR_END)
    await ctx.send(embed=embed)


# ---------------------------------------------------------------------------
# ADMIN CONTROL PANEL
# ---------------------------------------------------------------------------
class StartGiveawayModal(discord.ui.Modal, title="Start Giveaway"):
    duration = discord.ui.TextInput(label="Duration (e.g. 10m, 2h, 1d)", placeholder="10m")
    winners = discord.ui.TextInput(label="Number of winners", placeholder="1")
    prize = discord.ui.TextInput(label="Prize", style=discord.TextStyle.paragraph, placeholder="Discord Nitro")
    channel_id = discord.ui.TextInput(label="Channel ID to post in", placeholder="123456789012345678")
    role_id = discord.ui.TextInput(label="Required role ID (optional)", required=False)

    async def on_submit(self, interaction: discord.Interaction):
        seconds = parse_duration(self.duration.value)
        if seconds is None:
            await interaction.response.send_message("❌ Invalid duration. Use e.g. 10s, 5m, 2h, 1d.", ephemeral=True)
            return
        try:
            winners_count = int(self.winners.value)
            if winners_count < 1:
                raise ValueError
        except ValueError:
            await interaction.response.send_message("❌ Winners must be a whole number greater than 0.", ephemeral=True)
            return

        channel = None
        if self.channel_id.value.strip().isdigit():
            channel = interaction.guild.get_channel(int(self.channel_id.value.strip()))
        if channel is None:
            await interaction.response.send_message("❌ Channel not found. Check the Channel ID.", ephemeral=True)
            return

        required_role_id = None
        rid = self.role_id.value.strip()
        if rid.isdigit():
            required_role_id = int(rid)

        msg, gw = await create_giveaway(
            channel, interaction.user.id, interaction.guild.id,
            self.prize.value.strip(), winners_count, seconds, required_role_id,
        )
        await interaction.response.send_message(
            f"✅ Giveaway started in {channel.mention} (ID: `{msg.id}`)", ephemeral=True
        )


class MessageIdModal(discord.ui.Modal):
    message_id = discord.ui.TextInput(label="Giveaway Message ID", placeholder="123456789012345678")

    def __init__(self, title, action):
        super().__init__(title=title)
        self.action = action

    async def on_submit(self, interaction: discord.Interaction):
        mid = self.message_id.value.strip()
        gw = get_giveaway_or_none(mid)
        if not gw:
            await interaction.response.send_message("❌ No giveaway found with that ID.", ephemeral=True)
            return
        gw["_id"] = mid

        if self.action == "end":
            if gw.get("ended"):
                await interaction.response.send_message("❌ That giveaway already ended.", ephemeral=True)
                return
            await finish_giveaway(mid, gw)
            await interaction.response.send_message(f"✅ Giveaway `{mid}` has ended.", ephemeral=True)
        elif self.action == "cancel":
            if gw.get("ended"):
                await interaction.response.send_message("❌ That giveaway already ended.", ephemeral=True)
                return
            await finish_giveaway(mid, gw, cancelled=True)
            await interaction.response.send_message(f"🗑️ Giveaway `{mid}` was cancelled.", ephemeral=True)
        elif self.action == "reroll":
            if not gw.get("ended"):
                await interaction.response.send_message("❌ You can only reroll an ended giveaway.", ephemeral=True)
                return
            winners = pick_winners(gw)
            if not winners:
                await interaction.response.send_message("😢 No valid entries to pick from.", ephemeral=True)
                return
            gw["last_winners"] = winners
            save_giveaways()
            channel = bot.get_channel(gw["channel_id"])
            mentions = ", ".join(f"<@{w}>" for w in winners)
            if channel:
                await channel.send(f"🔄 Rerolled! Congratulations {mentions}, you won **{gw['prize']}**")
            await interaction.response.send_message("✅ Rerolled.", ephemeral=True)


class LockUnlockModal(discord.ui.Modal):
    message_id = discord.ui.TextInput(label="Giveaway Message ID", placeholder="123456789012345678")
    user_id = discord.ui.TextInput(label="User ID", placeholder="123456789012345678")

    def __init__(self, title, action):
        super().__init__(title=title)
        self.action = action

    async def on_submit(self, interaction: discord.Interaction):
        mid = self.message_id.value.strip()
        gw = get_giveaway_or_none(mid)
        if not gw:
            await interaction.response.send_message("❌ No giveaway found with that ID.", ephemeral=True)
            return
        if not self.user_id.value.strip().isdigit():
            await interaction.response.send_message("❌ User ID must be numeric.", ephemeral=True)
            return
        uid = int(self.user_id.value.strip())
        gw.setdefault("locked_winners", [])

        if self.action == "lock":
            if gw.get("ended"):
                await interaction.response.send_message("❌ That giveaway already ended.", ephemeral=True)
                return
            if uid in gw["locked_winners"]:
                await interaction.response.send_message("⚠️ Already locked.", ephemeral=True)
                return
            gw["locked_winners"].append(uid)
            if uid not in gw["participants"]:
                gw["participants"].append(uid)
            save_giveaways()
            gw["_id"] = mid
            await update_giveaway_embed(gw, mid)
            await interaction.response.send_message(f"🔒 Locked <@{uid}> as a guaranteed winner.", ephemeral=True)
        else:
            if uid not in gw["locked_winners"]:
                await interaction.response.send_message("⚠️ That user is not locked.", ephemeral=True)
                return
            gw["locked_winners"].remove(uid)
            save_giveaways()
            gw["_id"] = mid
            await update_giveaway_embed(gw, mid)
            await interaction.response.send_message(f"🔓 Unlocked <@{uid}>.", ephemeral=True)


class PanelView(discord.ui.View):
    """Persistent admin control panel. All actions still require Manage Server."""

    def __init__(self):
        super().__init__(timeout=None)

    async def _check(self, interaction: discord.Interaction) -> bool:
        if not has_host_perms(interaction.user):
            await interaction.response.send_message(
                "❌ You need the `Manage Server` permission to use this panel.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Start Giveaway", emoji="🎉", style=discord.ButtonStyle.green, custom_id="panel_start")
    async def start_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check(interaction):
            return
        await interaction.response.send_modal(StartGiveawayModal())

    @discord.ui.button(label="End", style=discord.ButtonStyle.blurple, custom_id="panel_end")
    async def end_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check(interaction):
            return
        await interaction.response.send_modal(MessageIdModal("End Giveaway", "end"))

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.red, custom_id="panel_cancel")
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check(interaction):
            return
        await interaction.response.send_modal(MessageIdModal("Cancel Giveaway", "cancel"))

    @discord.ui.button(label="Reroll", style=discord.ButtonStyle.gray, custom_id="panel_reroll")
    async def reroll_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check(interaction):
            return
        await interaction.response.send_modal(MessageIdModal("Reroll Giveaway", "reroll"))

    @discord.ui.button(label="Lock Winner", emoji="🔒", style=discord.ButtonStyle.gray, custom_id="panel_lock")
    async def lock_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check(interaction):
            return
        await interaction.response.send_modal(LockUnlockModal("Lock Winner", "lock"))

    @discord.ui.button(label="Unlock Winner", emoji="🔓", style=discord.ButtonStyle.gray, custom_id="panel_unlock")
    async def unlock_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check(interaction):
            return
        await interaction.response.send_modal(LockUnlockModal("Unlock Winner", "unlock"))

    @discord.ui.button(label="List Active", emoji="📋", style=discord.ButtonStyle.gray, custom_id="panel_list", row=1)
    async def list_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check(interaction):
            return
        active = [
            (mid, gw) for mid, gw in giveaways.items()
            if gw["guild_id"] == interaction.guild.id and not gw.get("ended")
        ]
        if not active:
            await interaction.response.send_message("📭 There are no active giveaways.", ephemeral=True)
            return
        embed = discord.Embed(title="📋 Active Giveaways", color=EMBED_COLOR)
        for mid, gw in active:
            locked_note = f" | 🔒 {len(gw.get('locked_winners', []))} locked" if gw.get("locked_winners") else ""
            embed.add_field(
                name=f"🎁 {gw['prize']} (ID: {mid})",
                value=f"Winners: {gw['winners']} | Entries: {len(gw['participants'])}{locked_note} | Ends: <t:{gw['end_time']}:R>",
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.command(name="gpanel")
@is_host()
async def gpanel(ctx, role: discord.Role = None):
    """?gpanel [@staff_role] - Create/refresh the private admin control panel channel."""
    guild = ctx.guild
    existing = discord.utils.get(guild.text_channels, name=PANEL_CHANNEL_NAME)

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        ctx.author: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
    }
    if role:
        overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

    if existing:
        channel = existing
        await channel.edit(overwrites=overwrites)
    else:
        channel = await guild.create_text_channel(PANEL_CHANNEL_NAME, overwrites=overwrites)

    embed = discord.Embed(
        title=f"{GIFT_EMOJI} Giveaway Control Panel",
        description=(
            "Use the buttons below to manage giveaways without typing commands.\n"
            "Only members with `Manage Server` can use these controls.\n\n"
            "🔒 Lock/Unlock and locked-winner counts are only ever visible here, "
            "never in the public giveaway messages."
        ),
        color=EMBED_COLOR,
    )
    await channel.send(embed=embed, view=PanelView())
    await ctx.send(f"✅ Control panel ready in {channel.mention}")


# ---------------------------------------------------------------------------
# HELP
# ---------------------------------------------------------------------------
@bot.command(name="ghelp")
async def ghelp(ctx):
    embed = discord.Embed(
        title=f"{GIFT_EMOJI} Giveaway Bot Commands",
        description=f"Prefix: `{PREFIX}`\nTip: run `{PREFIX}gpanel` for a button-based control panel instead of typing commands.",
        color=EMBED_COLOR,
    )
    embed.add_field(
        name="Giveaway Management",
        value=(
            f"`{PREFIX}gstart <time> <winners> <prize> [@role]` - Start\n"
            f"`{PREFIX}gend <id>` - End now\n"
            f"`{PREFIX}gcancel <id>` - Cancel\n"
            f"`{PREFIX}greroll <id> [count]` - Reroll\n"
            f"`{PREFIX}glist` - List active\n"
            f"`{PREFIX}ginfo <id>` - Public details\n"
            f"`{PREFIX}gpanel [@role]` - Open the button-based control panel"
        ),
        inline=False,
    )
    embed.add_field(
        name="Entry Management",
        value=(
            f"`{PREFIX}gadd <id> @user` - Add entry\n"
            f"`{PREFIX}gremove <id> @user` - Remove entry"
        ),
        inline=False,
    )
    embed.add_field(
        name="Lock Winner System (host-only, admin-visible only)",
        value=(
            f"`{PREFIX}glock <id> @user` - Guarantee this user wins\n"
            f"`{PREFIX}gunlock <id> @user` - Unlock\n"
            f"`{PREFIX}glockedlist <id>` - View locked winners (host-only)\n"
            f"`{PREFIX}gadmininfo <id>` - Full details including locked winners (host-only)"
        ),
        inline=False,
    )
    embed.set_footer(text=f"Example: {PREFIX}gstart 10m 1 Discord Nitro")
    await ctx.send(embed=embed)


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CheckFailure):
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"❌ Missing arguments. See `{PREFIX}ghelp` for usage.")
        return
    if isinstance(error, commands.BadArgument):
        await ctx.send("❌ Invalid argument (e.g. user not found or bad number).")
        return
    if isinstance(error, commands.CommandNotFound):
        return
    print(f"Error: {error}")
    await ctx.send(f"⚠️ An error occurred: {error}")


# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("❌ Please set the DISCORD_TOKEN environment variable before running the bot.")
    keep_alive()
    bot.run(TOKEN)
