"""
Discord Giveaway Bot - Single File Edition (Bilingual TH/EN)
เขียนด้วย discord.py 2.x | Prefix: ?
Bot ระบบ giveaway ครบวงจร รองรับ 2 ภาษา (ไทย/อังกฤษ) ในทุกข้อความ

ฟีเจอร์หลัก / Main Features:
  - เริ่ม/จบ/รีโรล/ยกเลิก giveaway | Start/End/Reroll/Cancel giveaways
  - ปุ่มกดเข้าร่วมแบบ persistent | Persistent join button
  - ระบบ "ล็อคผู้ชนะ" (Lock Winner) - การันตีว่าผู้ใช้ที่ถูกล็อคจะเป็นผู้ชนะแน่นอน
    "Lock Winner" system - guarantees a specific user will win when the giveaway ends
  - กำหนดโรลที่ต้องมีถึงจะเข้าร่วมได้ (optional) | Optional required-role gate
  - เพิ่ม/ลบผู้เข้าร่วมด้วยมือ | Manually add/remove participants
  - บันทึกสถานะลงไฟล์ JSON | Persists state to JSON file
  - แนบ mini web server (Flask) สำหรับ Render Web Service keep-alive

ตั้งค่า Environment Variables ก่อนรัน / Set these env vars before running:
  DISCORD_TOKEN   = โทเคนบอทของคุณ (จำเป็น) / your bot token (required)
  PREFIX          = คำนำหน้าคำสั่ง (default "?") / command prefix (default "?")
  PORT            = พอร์ตสำหรับ keep-alive server (Render จะเซ็ตให้เองอัตโนมัติ)
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


def bi(th: str, en: str) -> str:
    """สร้างข้อความสองภาษา (ไทย/อังกฤษ) / Build a bilingual TH/EN message"""
    return f"{th}\n🇬🇧 {en}"


# ---------------------------------------------------------------------------
# KEEP-ALIVE WEB SERVER (สำหรับ Render Web Service)
# ---------------------------------------------------------------------------
app = Flask(__name__)


@app.route("/")
def home():
    return "Giveaway bot is alive! / บอททำงานปกติ"


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
    """แปลง '10m' '2h' '1d' '30s' -> วินาที (int) / parse duration string to seconds"""
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


# ---------------------------------------------------------------------------
# GIVEAWAY BUTTON VIEW
# ---------------------------------------------------------------------------
class GiveawayView(discord.ui.View):
    """ปุ่มเข้าร่วม giveaway - persistent / Persistent join button"""

    def __init__(self, message_id: int):
        super().__init__(timeout=None)
        self.message_id = message_id
        self.join_button.custom_id = f"gw_join_{message_id}"

    @discord.ui.button(label="เข้าร่วม / Join", emoji=GIFT_EMOJI, style=discord.ButtonStyle.green)
    async def join_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        mid = str(self.message_id)
        gw = giveaways.get(mid)

        if gw is None or gw.get("ended"):
            await interaction.response.send_message(
                bi("❌ giveaway นี้จบไปแล้ว", "This giveaway has already ended."), ephemeral=True
            )
            return

        user = interaction.user

        role_id = gw.get("required_role_id")
        if role_id:
            member = interaction.guild.get_member(user.id) or await interaction.guild.fetch_member(user.id)
            if not member or role_id not in [r.id for r in member.roles]:
                role = interaction.guild.get_role(role_id)
                rname = role.mention if role else "โรลที่กำหนด / the required role"
                await interaction.response.send_message(
                    bi(f"❌ คุณต้องมีโรล {rname} ถึงจะเข้าร่วมได้",
                       f"You need the role {rname} to join."),
                    ephemeral=True,
                )
                return

        participants = gw["participants"]
        if user.id in participants:
            participants.remove(user.id)
            save_giveaways()
            await interaction.response.send_message(
                bi("↩️ ยกเลิกการเข้าร่วมแล้ว", "You left the giveaway."), ephemeral=True
            )
        else:
            participants.append(user.id)
            save_giveaways()
            await interaction.response.send_message(
                bi("✅ เข้าร่วม giveaway สำเร็จ!", "You successfully joined the giveaway!"),
                ephemeral=True,
            )

        await update_giveaway_embed(gw, mid)


# ---------------------------------------------------------------------------
# EMBED BUILDERS
# ---------------------------------------------------------------------------
def build_embed(gw: dict, ended: bool = False) -> discord.Embed:
    locked = gw.get("locked_winners", [])
    desc_lines = [
        f"🎁 **ของรางวัล / Prize:** {gw['prize']}",
        f"👤 **ผู้จัด / Host:** <@{gw['host_id']}>",
        f"🏆 **จำนวนผู้ชนะ / Winners:** {gw['winners']}",
        f"👥 **ผู้เข้าร่วม / Entries:** {len(gw['participants'])}",
    ]
    if gw.get("required_role_id"):
        desc_lines.append(f"🔑 **ต้องมีโรล / Required role:** <@&{gw['required_role_id']}>")
    if locked:
        mentions = ", ".join(f"<@{u}>" for u in locked)
        desc_lines.append(f"🔒 **ผู้ชนะที่ถูกล็อค / Locked winner(s):** {mentions}")

    if ended:
        desc_lines.append("⏰ **สถานะ / Status:** จบแล้ว / Ended")
        title = f"{GIFT_EMOJI} GIVEAWAY จบแล้ว / ENDED {GIFT_EMOJI}"
        color = EMBED_COLOR_END
    else:
        desc_lines.append(
            f"⏰ **สิ้นสุด / Ends:** <t:{gw['end_time']}:R> (<t:{gw['end_time']}:f>)"
        )
        title = f"{GIFT_EMOJI} GIVEAWAY {GIFT_EMOJI}"
        color = EMBED_COLOR

    embed = discord.Embed(title=title, description="\n".join(desc_lines), color=color)
    embed.set_footer(text=f"ID: {gw.get('_id', '')}")
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
    """
    เลือกผู้ชนะ: ผู้ที่ถูกล็อคจะชนะเสมอ ส่วนที่เหลือสุ่มจากผู้เข้าร่วม
    Pick winners: locked users always win; remaining slots are picked randomly.
    """
    target = count or gw["winners"]
    locked = list(gw.get("locked_winners", []))
    pool = [uid for uid in gw["participants"] if uid not in locked]

    remaining_slots = max(0, target - len(locked))
    random_winners = random.sample(pool, min(remaining_slots, len(pool))) if pool else []

    # ถ้าล็อคไว้เยอะกว่าจำนวนผู้ชนะที่ตั้งไว้ ผู้ที่ถูกล็อคก็ยังชนะทั้งหมด
    # If more users are locked than the configured winner count, all locked users still win.
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
            embed.description += "\n\n❌ **ยกเลิกแล้ว / Cancelled**"
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
            await channel.send(
                bi(f"😢 ไม่มีผู้ชนะสำหรับ **{gw['prize']}** (ไม่มีผู้เข้าร่วม)",
                   f"No winner for **{gw['prize']}** (no valid entries).")
            )
        return

    mentions = ", ".join(f"<@{w}>" for w in winners)
    if channel:
        await channel.send(
            bi(
                f"🎉 ยินดีด้วย {mentions}! คุณชนะ **{gw['prize']}** (ID: `{mid}`)",
                f"Congratulations {mentions}! You won **{gw['prize']}** (ID: `{mid}`)",
            )
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
    if not check_giveaways.is_running():
        check_giveaways.start()


# ---------------------------------------------------------------------------
# PERMISSION CHECK
# ---------------------------------------------------------------------------
def is_host():
    async def predicate(ctx):
        if ctx.author.guild_permissions.manage_guild:
            return True
        await ctx.send(
            bi("❌ คุณต้องมีสิทธิ์ `Manage Server` ถึงจะใช้คำสั่งนี้ได้",
               "❌ You need the `Manage Server` permission to use this command.")
        )
        return False
    return commands.check(predicate)


def get_giveaway_or_none(mid: str):
    return giveaways.get(mid)


# ---------------------------------------------------------------------------
# COMMANDS: GIVEAWAY LIFECYCLE
# ---------------------------------------------------------------------------
@bot.command(name="gstart")
@is_host()
async def gstart(ctx, duration: str, winners: int, *, prize: str):
    """?gstart <เวลา/duration เช่น 10m,2h,1d> <จำนวนผู้ชนะ/winners> <รางวัล/prize> [@โรล/role]"""
    seconds = parse_duration(duration)
    if seconds is None:
        await ctx.send(
            bi("❌ รูปแบบเวลาไม่ถูกต้อง ใช้เช่น `10s` `5m` `2h` `1d`",
               "❌ Invalid time format. Use e.g. `10s` `5m` `2h` `1d`.")
        )
        return
    if winners < 1:
        await ctx.send(bi("❌ จำนวนผู้ชนะต้องมากกว่า 0", "❌ Winner count must be greater than 0."))
        return

    required_role_id = None
    if ctx.message.role_mentions:
        required_role_id = ctx.message.role_mentions[0].id
        prize = re.sub(r"<@&\d+>", "", prize).strip()

    if not prize:
        await ctx.send(bi("❌ กรุณาระบุของรางวัล", "❌ Please specify a prize."))
        return

    end_time = int((datetime.now(timezone.utc) + timedelta(seconds=seconds)).timestamp())

    placeholder = discord.Embed(title=f"{GIFT_EMOJI} GIVEAWAY {GIFT_EMOJI}", description="กำลังสร้าง... / Setting up...", color=EMBED_COLOR)
    msg = await ctx.send(embed=placeholder)

    gw = {
        "guild_id": ctx.guild.id,
        "channel_id": ctx.channel.id,
        "host_id": ctx.author.id,
        "prize": prize,
        "winners": winners,
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
    try:
        await ctx.message.delete()
    except Exception:
        pass


@bot.command(name="gend")
@is_host()
async def gend(ctx, message_id: str):
    """?gend <message_id> - จบ giveaway ทันที / End a giveaway immediately"""
    gw = get_giveaway_or_none(message_id)
    if not gw or gw.get("ended"):
        await ctx.send(bi("❌ ไม่พบ giveaway ที่ยังไม่จบด้วย ID นี้", "❌ No active giveaway found with that ID."))
        return
    gw["_id"] = message_id
    await finish_giveaway(message_id, gw)
    await ctx.send(bi(f"✅ จบ giveaway `{message_id}` แล้ว", f"✅ Giveaway `{message_id}` has ended."))


@bot.command(name="gcancel")
@is_host()
async def gcancel(ctx, message_id: str):
    """?gcancel <message_id> - ยกเลิก giveaway โดยไม่สุ่มผู้ชนะ / Cancel without picking a winner"""
    gw = get_giveaway_or_none(message_id)
    if not gw or gw.get("ended"):
        await ctx.send(bi("❌ ไม่พบ giveaway ที่ยังไม่จบด้วย ID นี้", "❌ No active giveaway found with that ID."))
        return
    gw["_id"] = message_id
    await finish_giveaway(message_id, gw, cancelled=True)
    await ctx.send(bi(f"🗑️ ยกเลิก giveaway `{message_id}` แล้ว", f"🗑️ Giveaway `{message_id}` was cancelled."))


@bot.command(name="greroll")
@is_host()
async def greroll(ctx, message_id: str, count: int = None):
    """?greroll <message_id> [จำนวน/count] - สุ่มผู้ชนะใหม่ (ผู้ชนะที่ถูกล็อคยังคงชนะ)"""
    gw = get_giveaway_or_none(message_id)
    if not gw or not gw.get("ended"):
        await ctx.send(
            bi("❌ ต้องเป็น giveaway ที่จบแล้วเท่านั้นถึงจะรีโรลได้",
               "❌ You can only reroll a giveaway that has already ended.")
        )
        return
    winners = pick_winners(gw, count)
    if not winners:
        await ctx.send(bi("😢 ไม่มีผู้เข้าร่วมให้สุ่ม", "😢 No valid entries to pick from."))
        return
    gw["last_winners"] = winners
    save_giveaways()
    mentions = ", ".join(f"<@{w}>" for w in winners)
    await ctx.send(
        bi(f"🔄 รีโรลใหม่! ยินดีด้วย {mentions} คุณชนะ **{gw['prize']}**",
           f"🔄 Rerolled! Congratulations {mentions}, you won **{gw['prize']}**")
    )


@bot.command(name="glist")
async def glist(ctx):
    """?glist - แสดง giveaway ที่กำลังดำเนินการ / List active giveaways"""
    active = [
        (mid, gw) for mid, gw in giveaways.items()
        if gw["guild_id"] == ctx.guild.id and not gw.get("ended")
    ]
    if not active:
        await ctx.send(bi("📭 ไม่มี giveaway ที่กำลังดำเนินการอยู่", "📭 There are no active giveaways."))
        return
    embed = discord.Embed(title="📋 Giveaway ที่กำลังดำเนินการ / Active Giveaways", color=EMBED_COLOR)
    for mid, gw in active:
        locked_note = f" | 🔒 {len(gw.get('locked_winners', []))}" if gw.get("locked_winners") else ""
        embed.add_field(
            name=f"🎁 {gw['prize']} (ID: {mid})",
            value=(
                f"Winners: {gw['winners']} | Entries: {len(gw['participants'])}"
                f"{locked_note} | Ends: <t:{gw['end_time']}:R>"
            ),
            inline=False,
        )
    await ctx.send(embed=embed)


@bot.command(name="ginfo")
async def ginfo(ctx, message_id: str):
    """?ginfo <message_id> - ดูรายละเอียด giveaway / View giveaway details"""
    gw = get_giveaway_or_none(message_id)
    if not gw:
        await ctx.send(bi("❌ ไม่พบ giveaway ด้วย ID นี้", "❌ No giveaway found with that ID."))
        return
    gw["_id"] = message_id
    await ctx.send(embed=build_embed(gw, ended=gw.get("ended", False)))


# ---------------------------------------------------------------------------
# COMMANDS: PARTICIPANT MANAGEMENT
# ---------------------------------------------------------------------------
@bot.command(name="gadd")
@is_host()
async def gadd(ctx, message_id: str, member: discord.Member):
    """?gadd <message_id> @user - เพิ่มผู้เข้าร่วมด้วยมือ / Manually add a participant"""
    gw = get_giveaway_or_none(message_id)
    if not gw or gw.get("ended"):
        await ctx.send(bi("❌ ไม่พบ giveaway ที่ยังไม่จบด้วย ID นี้", "❌ No active giveaway found with that ID."))
        return
    if member.id in gw["participants"]:
        await ctx.send(bi(f"⚠️ {member.mention} เข้าร่วมอยู่แล้ว", f"⚠️ {member.mention} has already joined."))
        return
    gw["participants"].append(member.id)
    save_giveaways()
    gw["_id"] = message_id
    await update_giveaway_embed(gw, message_id)
    await ctx.send(bi(f"✅ เพิ่ม {member.mention} เข้า giveaway แล้ว", f"✅ Added {member.mention} to the giveaway."))


@bot.command(name="gremove")
@is_host()
async def gremove(ctx, message_id: str, member: discord.Member):
    """?gremove <message_id> @user - นำผู้เข้าร่วมออก / Remove a participant"""
    gw = get_giveaway_or_none(message_id)
    if not gw:
        await ctx.send(bi("❌ ไม่พบ giveaway ด้วย ID นี้", "❌ No giveaway found with that ID."))
        return
    if member.id not in gw["participants"]:
        await ctx.send(bi(f"⚠️ {member.mention} ไม่ได้อยู่ในรายชื่อผู้เข้าร่วม", f"⚠️ {member.mention} is not in the entry list."))
        return
    gw["participants"].remove(member.id)
    save_giveaways()
    gw["_id"] = message_id
    await update_giveaway_embed(gw, message_id)
    await ctx.send(bi(f"🗑️ นำ {member.mention} ออกจาก giveaway แล้ว", f"🗑️ Removed {member.mention} from the giveaway."))


# ---------------------------------------------------------------------------
# COMMANDS: LOCK WINNER SYSTEM
# "ล็อค" ในที่นี้ = การันตีว่าผู้ใช้คนนี้จะเป็นผู้ชนะเมื่อ giveaway จบ
# "Lock" here means guaranteeing that this user will be a winner when the giveaway ends.
# ---------------------------------------------------------------------------
@bot.command(name="glock")
@is_host()
async def glock(ctx, message_id: str, member: discord.Member):
    """?glock <message_id> @user - ล็อคผู้ใช้นี้ให้เป็นผู้ชนะแน่นอน / Lock this user as a guaranteed winner"""
    gw = get_giveaway_or_none(message_id)
    if not gw or gw.get("ended"):
        await ctx.send(bi("❌ ไม่พบ giveaway ที่ยังไม่จบด้วย ID นี้", "❌ No active giveaway found with that ID."))
        return

    gw.setdefault("locked_winners", [])
    if member.id in gw["locked_winners"]:
        await ctx.send(bi(f"⚠️ {member.mention} ถูกล็อคเป็นผู้ชนะอยู่แล้ว", f"⚠️ {member.mention} is already locked as a winner."))
        return

    gw["locked_winners"].append(member.id)
    # เพิ่มเข้ารายชื่อผู้เข้าร่วมด้วย ถ้ายังไม่ได้เข้าร่วม / also add them to entries if not already joined
    if member.id not in gw["participants"]:
        gw["participants"].append(member.id)
    save_giveaways()
    gw["_id"] = message_id
    await update_giveaway_embed(gw, message_id)

    await ctx.send(
        bi(f"🔒 ล็อค {member.mention} ให้เป็นผู้ชนะของ giveaway นี้แล้ว (ชนะแน่นอนตอนจับรางวัล)",
           f"🔒 Locked {member.mention} as a guaranteed winner of this giveaway.")
    )


@bot.command(name="gunlock")
@is_host()
async def gunlock(ctx, message_id: str, member: discord.Member):
    """?gunlock <message_id> @user - ปลดล็อคผู้ชนะที่ถูกล็อคไว้ / Remove a locked winner"""
    gw = get_giveaway_or_none(message_id)
    if not gw:
        await ctx.send(bi("❌ ไม่พบ giveaway ด้วย ID นี้", "❌ No giveaway found with that ID."))
        return

    locked = gw.get("locked_winners", [])
    if member.id not in locked:
        await ctx.send(bi(f"⚠️ {member.mention} ไม่ได้ถูกล็อคเป็นผู้ชนะอยู่", f"⚠️ {member.mention} is not currently locked as a winner."))
        return

    locked.remove(member.id)
    save_giveaways()
    gw["_id"] = message_id
    await update_giveaway_embed(gw, message_id)
    await ctx.send(bi(f"🔓 ปลดล็อค {member.mention} แล้ว", f"🔓 Unlocked {member.mention}."))


@bot.command(name="glockedlist")
async def glockedlist(ctx, message_id: str):
    """?glockedlist <message_id> - แสดงผู้ชนะที่ถูกล็อคของ giveaway นี้ / Show locked winners for this giveaway"""
    gw = get_giveaway_or_none(message_id)
    if not gw:
        await ctx.send(bi("❌ ไม่พบ giveaway ด้วย ID นี้", "❌ No giveaway found with that ID."))
        return
    locked = gw.get("locked_winners", [])
    if not locked:
        await ctx.send(bi("📭 giveaway นี้ยังไม่มีผู้ชนะที่ถูกล็อค", "📭 This giveaway has no locked winners."))
        return
    lines = "\n".join(f"🔒 <@{uid}>" for uid in locked)
    embed = discord.Embed(
        title="ผู้ชนะที่ถูกล็อค / Locked Winners",
        description=lines,
        color=EMBED_COLOR_END,
    )
    await ctx.send(embed=embed)


# ---------------------------------------------------------------------------
# HELP
# ---------------------------------------------------------------------------
@bot.command(name="ghelp")
async def ghelp(ctx):
    embed = discord.Embed(
        title=f"{GIFT_EMOJI} คำสั่ง Giveaway Bot / Giveaway Bot Commands",
        description=f"Prefix: `{PREFIX}`",
        color=EMBED_COLOR,
    )
    embed.add_field(
        name="จัดการ Giveaway / Giveaway Management",
        value=(
            f"`{PREFIX}gstart <เวลา/time> <ผู้ชนะ/winners> <รางวัล/prize> [@role]` - เริ่ม / Start\n"
            f"`{PREFIX}gend <id>` - จบทันที / End now\n"
            f"`{PREFIX}gcancel <id>` - ยกเลิก / Cancel\n"
            f"`{PREFIX}greroll <id> [count]` - สุ่มใหม่ / Reroll\n"
            f"`{PREFIX}glist` - รายการที่กำลังทำงาน / List active\n"
            f"`{PREFIX}ginfo <id>` - รายละเอียด / Details"
        ),
        inline=False,
    )
    embed.add_field(
        name="จัดการผู้เข้าร่วม / Entry Management",
        value=(
            f"`{PREFIX}gadd <id> @user` - เพิ่ม / Add entry\n"
            f"`{PREFIX}gremove <id> @user` - นำออก / Remove entry"
        ),
        inline=False,
    )
    embed.add_field(
        name="ระบบล็อคผู้ชนะ / Lock Winner System",
        value=(
            f"`{PREFIX}glock <id> @user` - ล็อคให้เป็นผู้ชนะแน่นอน / Guarantee this user wins\n"
            f"`{PREFIX}gunlock <id> @user` - ปลดล็อค / Unlock\n"
            f"`{PREFIX}glockedlist <id>` - ดูรายชื่อผู้ชนะที่ถูกล็อค / View locked winners"
        ),
        inline=False,
    )
    embed.set_footer(text="ตัวอย่าง / Example: ?gstart 10m 1 Discord Nitro")
    await ctx.send(embed=embed)


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CheckFailure):
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(bi(f"❌ ใส่พารามิเตอร์ไม่ครบ ดูตัวอย่างที่ `{PREFIX}ghelp`",
                           f"❌ Missing arguments. See `{PREFIX}ghelp` for usage."))
        return
    if isinstance(error, commands.BadArgument):
        await ctx.send(bi("❌ พารามิเตอร์ไม่ถูกต้อง (เช่น ไม่พบผู้ใช้ หรือใส่ตัวเลขผิด)",
                           "❌ Invalid argument (e.g. user not found or bad number)."))
        return
    if isinstance(error, commands.CommandNotFound):
        return
    print(f"Error: {error}")
    await ctx.send(bi(f"⚠️ เกิดข้อผิดพลาด: {error}", f"⚠️ An error occurred: {error}"))


# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("❌ กรุณาตั้งค่า Environment Variable ชื่อ DISCORD_TOKEN ก่อนรันบอท / Please set DISCORD_TOKEN")
    keep_alive()
    bot.run(TOKEN)
