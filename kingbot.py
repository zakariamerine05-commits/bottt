import discord
from discord.ext import commands
import asyncio
import sqlite3
import re
import datetime
import time
import logging
import os   # <-- تمت إضافته لدعم متغيرات البيئة
from collections import defaultdict, deque
from typing import Optional

# ========================== إعدادات التسجيل (Logging) ==========================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger('king_bot')

# ========================== التوكن - يدعم التشغيل المحلي وعبر Railway ==========================
TOKEN = os.environ.get("DISCORD_TOKEN")  # أولوية لمتغير البيئة (على Railway)
if not TOKEN:
    try:
        with open("token.txt", "r") as f:
            TOKEN = f.read().strip()
    except FileNotFoundError:
        TOKEN = "ضع_التوكن_هنا"   # بديل أخير للتجربة المحلية

# ======================================================================
# قاعدة البيانات
# ======================================================================
conn = sqlite3.connect('kingbot.db')
c = conn.cursor()

# إضافة حقول جديدة للتحكم في السبام
c.executescript('''
    CREATE TABLE IF NOT EXISTS whitelist (
        user_id INTEGER PRIMARY KEY,
        reason TEXT,
        added_by INTEGER,
        added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS blacklist (
        user_id INTEGER PRIMARY KEY,
        reason TEXT,
        added_by INTEGER,
        added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS guild_config (
        guild_id INTEGER PRIMARY KEY,
        prefix TEXT DEFAULT '.',
        log_channel INTEGER,
        welcome_channel INTEGER,
        welcome_message TEXT DEFAULT 'أهلاً بك {member} في {server}',
        leave_channel INTEGER,
        verify_role INTEGER,
        mute_role INTEGER,
        admin_role INTEGER,
        antinuke BOOLEAN DEFAULT 1,
        antiraid BOOLEAN DEFAULT 1,
        antilink BOOLEAN DEFAULT 1,
        automod BOOLEAN DEFAULT 1,
        antihoist BOOLEAN DEFAULT 1,
        antibot BOOLEAN DEFAULT 1,
        max_joins INTEGER DEFAULT 5,
        join_window INTEGER DEFAULT 10,
        ticket_category INTEGER,
        spam_limit INTEGER DEFAULT 10,        -- الحد الأقصى للرسائل
        spam_window INTEGER DEFAULT 60        -- الفترة الزمنية بالثواني (دقيقة)
    );
    CREATE TABLE IF NOT EXISTS warnings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        guild_id INTEGER,
        reason TEXT,
        moderator TEXT,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS tickets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        channel_id INTEGER,
        user_id INTEGER,
        guild_id INTEGER,
        status TEXT DEFAULT 'open',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
''')
conn.commit()

# ======================================================================
# دالة جلب البريفكس حسب السيرفر
# ======================================================================
async def get_prefix(bot, message):
    if not message.guild:
        return "."
    c.execute("SELECT prefix FROM guild_config WHERE guild_id = ?", (message.guild.id,))
    row = c.fetchone()
    return row[0] if row else "."

# ======================================================================
# إعدادات البوت مع الكاشات المحسنة
# ======================================================================
intents = discord.Intents.all()
bot = commands.Bot(command_prefix=get_prefix, intents=intents, help_command=None)

# كاشات للتحكم بالرايد والسبام (باستخدام deque مع maxlen)
join_cache = defaultdict(lambda: deque(maxlen=100))          # آخر 100 انضمام للرصد
message_cache = defaultdict(lambda: defaultdict(lambda: deque(maxlen=50)))  # آخر 50 طابع لكل مستخدم

# ======================================================================
# دوال مساعدة محسنة (مع أمان SQL)
# ======================================================================
# الأعمدة المسموح تعديلها في قاعدة البيانات (حماية من SQL Injection)
ALLOWED_CONFIG_KEYS = {
    "prefix", "log_channel", "welcome_channel", "welcome_message", "leave_channel",
    "verify_role", "mute_role", "admin_role", "antinuke", "antiraid", "antilink",
    "automod", "antihoist", "antibot", "max_joins", "join_window", "ticket_category",
    "spam_limit", "spam_window"
}

def get_config(guild_id: int):
    c.execute("SELECT * FROM guild_config WHERE guild_id = ?", (guild_id,))
    data = c.fetchone()
    if not data:
        # إدراج إعدادات افتراضية مع القيم الجديدة للسبام
        c.execute("""INSERT INTO guild_config 
        (guild_id, prefix, log_channel, welcome_channel, welcome_message, leave_channel,
        verify_role, mute_role, admin_role, antinuke, antiraid, antilink, automod,
        antihoist, antibot, max_joins, join_window, ticket_category, spam_limit, spam_window)
        VALUES (?, '.', NULL, NULL, ?, NULL, NULL, NULL, NULL, 1, 1, 1, 1, 1, 1, 5, 10, NULL, 10, 60)""",
        (guild_id, "أهلاً بك {member} في {server}"))
        conn.commit()
        return {
            "guild_id": guild_id, "prefix": ".", "log_channel": None,
            "welcome_channel": None, "welcome_message": "أهلاً بك {member} في {server}",
            "leave_channel": None, "verify_role": None, "mute_role": None,
            "admin_role": None, "antinuke": True, "antiraid": True,
            "antilink": True, "automod": True, "antihoist": True, "antibot": True,
            "max_joins": 5, "join_window": 10, "ticket_category": None,
            "spam_limit": 10, "spam_window": 60
        }
    # تحويل القيم من قاعدة البيانات إلى قاموس
    return {
        "guild_id": data[0], "prefix": data[1], "log_channel": data[2],
        "welcome_channel": data[3], "welcome_message": data[4],
        "leave_channel": data[5], "verify_role": data[6], "mute_role": data[7],
        "admin_role": data[8], "antinuke": bool(data[9]), "antiraid": bool(data[10]),
        "antilink": bool(data[11]), "automod": bool(data[12]),
        "antihoist": bool(data[13]), "antibot": bool(data[14]),
        "max_joins": data[15], "join_window": data[16], "ticket_category": data[17],
        "spam_limit": data[18], "spam_window": data[19]
    }

def update_config(guild_id: int, key: str, value):
    """تحديث إعداد معين مع التحقق من أن المفتاح مسموح به"""
    if key not in ALLOWED_CONFIG_KEYS:
        raise ValueError(f"مفتاح غير مسموح: {key}")
    c.execute(f"UPDATE guild_config SET {key} = ? WHERE guild_id = ?", (value, guild_id))
    conn.commit()

async def log_action(guild: discord.Guild, action: str, target: str, moderator: str, reason: str = ""):
    config = get_config(guild.id)
    if config["log_channel"]:
        channel = guild.get_channel(config["log_channel"])
        if channel:
            embed = discord.Embed(
                title=f"📋 {action}",
                description=f"**المستخدم:** {target}\n**بواسطة:** {moderator}\n**السبب:** {reason}",
                color=0x3498db,
                timestamp=datetime.datetime.now()
            )
            try:
                await channel.send(embed=embed)
            except Exception as e:
                logger.error(f"فشل إرسال سجل: {e}")

# ======================================================================
# نظام ANTI-NUKE + ANTI-RAID (محسن مع استثناء المالك)
# ======================================================================
class AntiNukeSystem:
    def __init__(self):
        self.ban_cache = defaultdict(lambda: deque(maxlen=20))
        self.channel_cache = defaultdict(lambda: deque(maxlen=20))
        self.role_cache = defaultdict(lambda: deque(maxlen=20))
        self.ban_threshold = 3
        self.window = 5

    async def check_raid(self, guild: discord.Guild, action_type: str, moderator_id: int) -> bool:
        # استثناء المالك من العقاب
        if moderator_id == guild.owner.id:
            return False

        cache_map = {
            "ban": self.ban_cache,
            "channel": self.channel_cache,
            "role": self.role_cache
        }
        cache = cache_map.get(action_type, self.ban_cache)
        
        now = time.time()
        cache[moderator_id].append(now)
        # تنظيف الطوابع القديمة (deque يقوم بذلك تلقائياً لكن نضمنه)
        while cache[moderator_id] and now - cache[moderator_id][0] > self.window:
            cache[moderator_id].popleft()
        
        if len(cache[moderator_id]) >= self.ban_threshold:
            member = guild.get_member(moderator_id)
            if member and not member.guild_permissions.administrator:
                await self.punish(guild, member, f"هجوم {action_type}")
                return True
        return False

    async def punish(self, guild: discord.Guild, member: discord.Member, reason: str):
        try:
            await member.ban(reason=f"[KING-BOT] {reason}")
            await log_action(guild, "🚫 KING PROTECTION", str(member), "Auto", reason)
        except:
            try:
                await member.kick(reason=f"[KING-BOT] {reason}")
            except Exception as e:
                logger.error(f"فشل معاقبة {member}: {e}")

antinuke = AntiNukeSystem()

# مهمة لاستعادة إعدادات السيرفر بعد الـRaid
async def reset_guild_settings(guild: discord.Guild, original_level, original_filter):
    await asyncio.sleep(60)  # انتظر دقيقة
    try:
        await guild.edit(verification_level=original_level, explicit_content_filter=original_filter)
        logger.info(f"تمت استعادة إعدادات السيرفر {guild.name}")
    except Exception as e:
        logger.error(f"فشل استعادة إعدادات {guild.name}: {e}")

# ======================================================================
# أحداث الحماية
# ======================================================================

@bot.event
async def on_ready():
    print(f"""
╔══════════════════════════════════╗
║     👑 KING SECURITY BOT       ║
║     {bot.user}                ║
║     Ping: {round(bot.latency * 1000)}ms          ║
║     Servers: {len(bot.guilds)}           ║
╚══════════════════════════════════╝
    """)
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.watching,
            name=f"👑 {len(bot.guilds)} servers | .help"
        )
    )

@bot.event
async def on_guild_join(guild: discord.Guild):
    try:
        category = await guild.create_category("👑 KING BOT")
        log_channel = await guild.create_text_channel("📋-logs", category=category)
        welcome_channel = await guild.create_text_channel("👋-welcome", category=category)

        update_config(guild.id, "log_channel", log_channel.id)
        update_config(guild.id, "welcome_channel", welcome_channel.id)

        embed = discord.Embed(
            title="👑 **KING SECURITY**",
            description="تم تثبيت نظام الحماية بنجاح!\n"
                        "استخدم `.setup` للإعدادات\n"
                        "استخدم `.help` لعرض الأوامر",
            color=0xFFD700
        )
        embed.set_footer(text="King Bot - حماية ملكية")
        await log_channel.send(embed=embed)
        await welcome_channel.send(embed=embed)
    except Exception as e:
        logger.error(f"خطأ في on_guild_join: {e}")

@bot.event
async def on_member_join(member: discord.Member):
    config = get_config(member.guild.id)
    guild = member.guild
    now = time.time()

    # Anti-Bot
    if config["antibot"] and member.bot:
        c.execute("SELECT * FROM whitelist WHERE user_id = ?", (member.id,))
        if not c.fetchone():
            try:
                await member.ban(reason="[KING] Bot غير مصرح به")
                await log_action(guild, "🤖 Anti-Bot", str(member), "Auto", "Bot غير مصرح به")
            except Exception as e:
                logger.error(f"فشل حظر البوت {member}: {e}")
            return

    # Anti-Raid
    if config["antiraid"]:
        join_cache[guild.id].append(now)
        # تنظيف المدخلات القديمة
        while join_cache[guild.id] and now - join_cache[guild.id][0] > config["join_window"]:
            join_cache[guild.id].popleft()

        if len(join_cache[guild.id]) >= config["max_joins"]:
            # حفظ الإعدادات الأصلية قبل التغيير
            original_level = guild.verification_level
            original_filter = guild.explicit_content_filter
            await guild.edit(
                verification_level=discord.VerificationLevel.high,
                explicit_content_filter=discord.ContentFilter.all_members
            )
            # جدولة استعادة الإعدادات بعد دقيقة
            asyncio.create_task(reset_guild_settings(guild, original_level, original_filter))
            
            if config["log_channel"]:
                channel = guild.get_channel(config["log_channel"])
                if channel:
                    embed = discord.Embed(
                        title="🚨 **ANTI-RAID**",
                        description=f"تم اكتشاف هجوم!\n"
                                    f"**داخلين:** {len(join_cache[guild.id])}\n"
                                    f"**خلال:** {config['join_window']}ثانية\n"
                                    f"✅ تم رفع الحماية مؤقتاً",
                        color=0xFF0000
                    )
                    await channel.send(embed=embed)

    # Blacklist check
    c.execute("SELECT * FROM blacklist WHERE user_id = ?", (member.id,))
    if c.fetchone():
        try:
            await member.ban(reason="[KING] قائمة سوداء")
        except Exception as e:
            logger.error(f"فشل حظر المدرج في القائمة السوداء {member}: {e}")
        return

    # Welcome
    if config["welcome_channel"] and not member.bot:
        channel = guild.get_channel(config["welcome_channel"])
        if channel:
            msg = config["welcome_message"].replace("{member}", member.mention).replace("{server}", guild.name)
            embed = discord.Embed(
                title="👋 مرحباً!",
                description=msg,
                color=0x00FF00
            )
            embed.set_thumbnail(url=member.display_avatar.url)
            await channel.send(embed=embed)

@bot.event
async def on_member_remove(member: discord.Member):
    config = get_config(member.guild.id)
    if config["leave_channel"]:
        channel = member.guild.get_channel(config["leave_channel"])
        if channel:
            embed = discord.Embed(
                title="👋 وداعاً",
                description=f"{member.mention} غادر السيرفر",
                color=0xFF0000
            )
            await channel.send(embed=embed)

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    if not message.guild:
        return

    config = get_config(message.guild.id)
    guild = message.guild

    # ---- نظام السبام الجديد (10 رسائل في الدقيقة) ----
    if config["automod"] and not message.author.guild_permissions.manage_messages:
        user_id = message.author.id
        now = time.time()
        # الحصول على قائمة الطوابع الزمنية لهذا المستخدم في هذا السيرفر
        user_timestamps = message_cache[guild.id][user_id]
        # إضافة الطابع الحالي
        user_timestamps.append(now)
        # إزالة الطوابع الأقدم من النافذة الزمنية (spam_window ثانية)
        while user_timestamps and now - user_timestamps[0] > config["spam_window"]:
            user_timestamps.popleft()
        
        # التحقق من تجاوز الحد المسموح (spam_limit)
        if len(user_timestamps) > config["spam_limit"]:
            try:
                await message.delete()
                warn_msg = await message.channel.send(
                    f"{message.author.mention} ❌ **تم منعك من الإرسال مؤقتاً**: أرسلت أكثر من {config['spam_limit']} رسالة في {config['spam_window']} ثانية.",
                    delete_after=5
                )
                # تسجيل الحادثة
                await log_action(guild, "⛔ Anti-Spam", str(message.author), "Auto", 
                                 f"{len(user_timestamps)} رسالة خلال {config['spam_window']} ثانية")
            except Exception as e:
                logger.error(f"فشل معالجة سبام: {e}")
            return  # لا نمرر الأمر إذا كان سباماً

    # ---- Anti-Link (بدون تغيير) ----
    if config["antilink"] and not message.author.guild_permissions.manage_messages:
        link_pattern = re.compile(r'(https?://|discord\.gg/|discord\.com/invite/)')
        if link_pattern.search(message.content):
            try:
                await message.delete()
                await message.channel.send(f"{message.author.mention} ❌ ممنوع إرسال الروابط", delete_after=3)
                await log_action(guild, "🔗 Anti-Link", str(message.author), "Auto", "رابط")
                return
            except:
                pass

    # ---- Anti-Hoist ----
    if config["antihoist"]:
        if message.author.display_name and message.author.display_name[0] in "!@#$%^&*()_+-=[]{}|;:',.<>?/~`":
            try:
                new_nick = message.author.display_name.lstrip('!@#$%^&*()_+-=[]{}|;:,.<>?/~`')
                if new_nick:
                    await message.author.edit(nick=f"_{new_nick}")
            except:
                pass

    # معالجة الأوامر
    await bot.process_commands(message)

@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    if before.author.bot:
        return
    await on_message(after)

@bot.event
async def on_guild_channel_create(channel: discord.abc.GuildChannel):
    if isinstance(channel, (discord.TextChannel, discord.VoiceChannel)):
        guild = channel.guild
        async for entry in guild.audit_logs(limit=1, action=discord.AuditLogAction.channel_create):
            if entry.user.id != bot.user.id:
                await antinuke.check_raid(guild, "channel", entry.user.id)

@bot.event
async def on_guild_channel_delete(channel: discord.abc.GuildChannel):
    guild = channel.guild
    async for entry in guild.audit_logs(limit=1, action=discord.AuditLogAction.channel_delete):
        if entry.user.id != bot.user.id:
            await antinuke.check_raid(guild, "channel", entry.user.id)

@bot.event
async def on_member_ban(guild: discord.Guild, user: discord.User):
    async for entry in guild.audit_logs(limit=1, action=discord.AuditLogAction.ban):
        if entry.user.id != bot.user.id and entry.user.id != guild.owner.id:
            await antinuke.check_raid(guild, "ban", entry.user.id)

@bot.event
async def on_guild_role_delete(role: discord.Role):
    guild = role.guild
    async for entry in guild.audit_logs(limit=1, action=discord.AuditLogAction.role_delete):
        if entry.user.id != bot.user.id:
            await antinuke.check_raid(guild, "role", entry.user.id)

# ======================================================================
# الأوامر (تم تحديث أمر automod ليدعم السبام)
# ======================================================================

@bot.command(name="help")
async def help_cmd(ctx):
    embed = discord.Embed(title="👑 **KING BOT - الأوامر**", description="بوت الحماية الشامل | صنع بـ ❤️", color=0xFFD700)
    embed.add_field(name="🛡️ **الحماية**", value="`.antinuke` - تشغيل/إيقاف حماية النيوك\n`.antiraid` - تشغيل/إيقاف حماية الريد\n`.antibot` - تشغيل/إيقاف منع البوتات\n`.antilink` - تشغيل/إيقاف منع الروابط\n`.automod [عدد] [ثواني]` - ضبط نظام السبام (مثال: `.automod 10 60`)\n`.antihoist` - تشغيل/إيقاف منع الرموز بالأسماء", inline=False)
    embed.add_field(name="👮 **الإدارة**", value="`.warn @user [سبب]` - تحذير عضو\n`.warnings @user` - عرض تحذيرات العضو\n`.clear [عدد]` - مسح الرسائل\n`.kick @user [سبب]` - طرد عضو\n`.ban @user [سبب]` - حظر عضو\n`.unban @user` - إلغاء حظر\n`.mute @user [وقت]` - كتم صوت\n`.unmute @user` - إلغاء كتم\n`.lock` - قفل الروم\n`.unlock` - فتح الروم\n`.slowmode [ثواني]` - وضع بطيء", inline=False)
    embed.add_field(name="⚙️ **الإعدادات**", value="`.setup` - إعداد البوت\n`.setprefix [رمز]` - تغيير بريفكس البوت\n`.setlog #روم` - تحديد روم السجلات\n`.setwelcome #روم` - تحديد روم الترحيب\n`.setleave #روم` - تحديد روم المغادرة\n`.welcomemsg [رسالة]` - تغيير رسالة الترحيب\n`.setadmin @رول` - تحديد رول الإدارة", inline=False)
    embed.add_field(name="🎫 **التذاكر**", value="`.ticket @user [سبب]` - فتح تذكرة\n`.close` - إغلاق التذكرة", inline=False)
    embed.add_field(name="📋 **القوائم**", value="`.whitelist @user [سبب]` - إضافة للقائمة البيضاء\n`.blacklist @user [سبب]` - إضافة للقائمة السوداء\n`.removewl @user` - إزالة من البيضاء\n`.removebl @user` - إزالة من السوداء\n`.listwl` - عرض القائمة البيضاء\n`.listbl` - عرض القائمة السوداء", inline=False)
    embed.add_field(name="ℹ️ **معلومات**", value="`.ping` - سرعة الاتصال\n`.stats` - إحصائيات البوت\n`.serverinfo` - معلومات السيرفر\n`.userinfo @user` - معلومات العضو", inline=False)
    embed.set_footer(text=f"King Bot v2.0 | {len(bot.guilds)} servers")
    await ctx.send(embed=embed)

@bot.command(name="ping")
async def ping_cmd(ctx):
    start = time.time()
    msg = await ctx.send("🏓 جاري القياس...")
    end = time.time()
    embed = discord.Embed(title="🏓 **PONG!**", description=f"**سرعة البوت:** `{round((end - start) * 1000)}ms`\n**WebSocket:** `{round(bot.latency * 1000)}ms`", color=0x00FF00)
    await msg.edit(embed=embed)

@bot.command(name="setup")
@commands.has_permissions(administrator=True)
async def setup_cmd(ctx):
    guild = ctx.guild
    embed = discord.Embed(title="⚙️ **إعداد KING BOT**", description="جاري تهيئة نظام الحماية...", color=0xFFD700)
    msg = await ctx.send(embed=embed)
    try:
        category = discord.utils.get(guild.categories, name="👑 KING BOT")
        if not category:
            category = await guild.create_category("👑 KING BOT")
        channels = {"📋-logs": "log_channel", "👋-welcome": "welcome_channel", "👋-leave": "leave_channel", "🎫-tickets": None}
        for name, config_key in channels.items():
            existing = discord.utils.get(guild.channels, name=name)
            if not existing:
                channel = await guild.create_text_channel(name, category=category)
                if config_key:
                    update_config(guild.id, config_key, channel.id)
        roles = {"👑 King Admin": 0x8B0000, "🛡️ King Mod": 0x006400, "✅ Verified": 0x00FF00, "🔇 Muted": 0x808080}
        for role_name, color in roles.items():
            existing = discord.utils.get(guild.roles, name=role_name)
            if not existing:
                role = await guild.create_role(name=role_name, color=color, reason="King Bot Setup")
                if role_name == "✅ Verified":
                    update_config(guild.id, "verify_role", role.id)
                elif role_name == "🔇 Muted":
                    update_config(guild.id, "mute_role", role.id)
                elif role_name == "👑 King Admin":
                    update_config(guild.id, "admin_role", role.id)
        embed.description = "✅ **تم إعداد البوت بنجاح!**\n\n**ما تم إنشاؤه:**\n📁 تصنيف: 👑 KING BOT\n📋 روم: السجلات\n👋 روم: الترحيب\n🎫 روم: التذاكر\n👑 رول: King Admin\n🛡️ رول: King Mod\n✅ رول: Verified\n🔇 رول: Muted\n\nاستخدم `.help` لعرض جميع الأوامر"
        await msg.edit(embed=embed)
    except Exception as e:
        embed.description = f"❌ حصل خطأ: {e}"
        await msg.edit(embed=embed)

@bot.command(name="antinuke")
@commands.has_permissions(administrator=True)
async def antinuke_cmd(ctx, status: str = None):
    config = get_config(ctx.guild.id)
    if status is None:
        status = "off" if config["antinuke"] else "on"
    new_status = status.lower() in ["on", "1", "true", "yes", "تشغيل"]
    update_config(ctx.guild.id, "antinuke", new_status)
    await ctx.send(f"✅ **حماية النيوك:** {'🟢 مفعلة' if new_status else '🔴 معطلة'}")

@bot.command(name="antiraid")
@commands.has_permissions(administrator=True)
async def antiraid_cmd(ctx, status: str = None):
    config = get_config(ctx.guild.id)
    if status is None:
        status = "off" if config["antiraid"] else "on"
    new_status = status.lower() in ["on", "1", "true", "yes", "تشغيل"]
    update_config(ctx.guild.id, "antiraid", new_status)
    await ctx.send(f"✅ **حماية الريد:** {'🟢 مفعلة' if new_status else '🔴 معطلة'}")

@bot.command(name="antibot")
@commands.has_permissions(administrator=True)
async def antibot_cmd(ctx, status: str = None):
    config = get_config(ctx.guild.id)
    if status is None:
        status = "off" if config["antibot"] else "on"
    new_status = status.lower() in ["on", "1", "true", "yes", "تشغيل"]
    update_config(ctx.guild.id, "antibot", new_status)
    await ctx.send(f"✅ **منع البوتات:** {'🟢 مفعل' if new_status else '🔴 معطل'}")

@bot.command(name="antilink")
@commands.has_permissions(manage_messages=True)
async def antilink_cmd(ctx, status: str = None):
    config = get_config(ctx.guild.id)
    if status is None:
        status = "off" if config["antilink"] else "on"
    new_status = status.lower() in ["on", "1", "true", "yes", "تشغيل"]
    update_config(ctx.guild.id, "antilink", new_status)
    await ctx.send(f"✅ **منع الروابط:** {'🟢 مفعل' if new_status else '🔴 معطل'}")

@bot.command(name="automod")
@commands.has_permissions(administrator=True)
async def automod_cmd(ctx, limit: int = None, window: int = None):
    """
    تشغيل/إيقاف الفلترة التلقائية (السبام) أو تغيير الإعدادات.
    الاستخدامات:
    `.automod`               -> عرض الحالة
    `.automod 10 60`         -> ضبط الحد الأقصى 10 رسائل خلال 60 ثانية
    `.automod on/off`        -> تشغيل أو إيقاف
    """
    config = get_config(ctx.guild.id)
    
    # إذا لم يحدد أي شيء، عرض الحالة
    if limit is None and window is None:
        status = "🟢 مفعلة" if config["automod"] else "🔴 معطلة"
        await ctx.send(f"✅ **الفلترة التلقائية:** {status}\n**الحد الأقصى:** {config['spam_limit']} رسالة خلال {config['spam_window']} ثانية")
        return
    
    # محاولة التعامل مع on/off
    if isinstance(limit, str) and limit.lower() in ["on", "off", "true", "false", "1", "0"]:
        new_status = limit.lower() in ["on", "1", "true"]
        update_config(ctx.guild.id, "automod", new_status)
        await ctx.send(f"✅ **الفلترة التلقائية:** {'🟢 مفعلة' if new_status else '🔴 معطلة'}")
        return
    
    # ضبط القيم الجديدة
    if limit is not None and window is not None:
        if limit < 1 or window < 1:
            await ctx.send("❌ يجب أن يكون الحد الأقصى والنافذة الزمنية أرقاماً موجبة")
            return
        update_config(ctx.guild.id, "spam_limit", limit)
        update_config(ctx.guild.id, "spam_window", window)
        # التأكد من تفعيل النظام إذا كان معطلاً
        if not config["automod"]:
            update_config(ctx.guild.id, "automod", True)
        await ctx.send(f"✅ **تم تحديث نظام السبام:** {limit} رسالة خلال {window} ثانية")
    else:
        await ctx.send("❌ الاستخدام الصحيح: `.automod [عدد] [ثواني]` أو `.automod on/off`")

@bot.command(name="antihoist")
@commands.has_permissions(administrator=True)
async def antihoist_cmd(ctx, status: str = None):
    config = get_config(ctx.guild.id)
    if status is None:
        status = "off" if config["antihoist"] else "on"
    new_status = status.lower() in ["on", "1", "true", "yes", "تشغيل"]
    update_config(ctx.guild.id, "antihoist", new_status)
    await ctx.send(f"✅ **منع الرموز بالأسماء:** {'🟢 مفعل' if new_status else '🔴 معطل'}")

@bot.command(name="setprefix")
@commands.has_permissions(administrator=True)
async def setprefix_cmd(ctx, prefix: str):
    if len(prefix) > 3:
        await ctx.send("❌ البريفكس يجب أن يكون 3 أحرف أو أقل")
        return
    update_config(ctx.guild.id, "prefix", prefix)
    await ctx.send(f"✅ **تم تغيير البريفكس إلى:** `{prefix}`")

@bot.command(name="setlog")
@commands.has_permissions(administrator=True)
async def setlog_cmd(ctx, channel: discord.TextChannel = None):
    if not channel:
        channel = ctx.channel
    update_config(ctx.guild.id, "log_channel", channel.id)
    await ctx.send(f"✅ **تم تحديد روم السجلات:** {channel.mention}")

@bot.command(name="setwelcome")
@commands.has_permissions(administrator=True)
async def setwelcome_cmd(ctx, channel: discord.TextChannel = None):
    if not channel:
        channel = ctx.channel
    update_config(ctx.guild.id, "welcome_channel", channel.id)
    await ctx.send(f"✅ **تم تحديد روم الترحيب:** {channel.mention}")

@bot.command(name="setleave")
@commands.has_permissions(administrator=True)
async def setleave_cmd(ctx, channel: discord.TextChannel = None):
    if not channel:
        channel = ctx.channel
    update_config(ctx.guild.id, "leave_channel", channel.id)
    await ctx.send(f"✅ **تم تحديد روم المغادرة:** {channel.mention}")

@bot.command(name="welcomemsg")
@commands.has_permissions(administrator=True)
async def welcomemsg_cmd(ctx, *, message: str):
    if len(message) > 500:
        await ctx.send("❌ الرسالة طويلة جداً (الحد الأقصى 500 حرف)")
        return
    update_config(ctx.guild.id, "welcome_message", message)
    await ctx.send(f"✅ **تم تغيير رسالة الترحيب**\n`{message}`")

@bot.command(name="setadmin")
@commands.has_permissions(administrator=True)
async def setadmin_cmd(ctx, role: discord.Role):
    update_config(ctx.guild.id, "admin_role", role.id)
    await ctx.send(f"✅ **تم تحديد رول الإدارة:** {role.mention}")

# باقي الأوامر (warn, kick, ban, mute, clear, lock, unlock, slowmode, whitelist, blacklist, ticket, close, stats, serverinfo, userinfo)

@bot.command(name="warn")
@commands.has_permissions(manage_messages=True)
async def warn_cmd(ctx, member: discord.Member, *, reason: str = "مخالفة"):
    c.execute("INSERT INTO warnings (user_id, guild_id, reason, moderator) VALUES (?, ?, ?, ?)",
              (member.id, ctx.guild.id, reason, str(ctx.author)))
    conn.commit()
    c.execute("SELECT COUNT(*) FROM warnings WHERE user_id = ? AND guild_id = ?", (member.id, ctx.guild.id))
    warn_count = c.fetchone()[0]
    embed = discord.Embed(title="⚠️ **تحذير**", description=f"**المستخدم:** {member.mention}\n**السبب:** {reason}\n**عدد التحذيرات:** {warn_count}\n**بواسطة:** {ctx.author.mention}", color=0xFFA500)
    await ctx.send(embed=embed)
    try:
        await member.send(f"⚠️ لقد تلقيت تحذير في {ctx.guild.name}\nالسبب: {reason}")
    except:
        pass
    await log_action(ctx.guild, "⚠️ Warn", str(member), str(ctx.author), reason)
    if warn_count >= 3:
        mute_role = ctx.guild.get_role(get_config(ctx.guild.id)["mute_role"])
        if mute_role:
            await member.add_roles(mute_role, reason="3 تحذيرات")
            await ctx.send(f"🔇 {member.mention} تم كتمه تلقائياً (3 تحذيرات)")

@bot.command(name="warnings")
@commands.has_permissions(manage_messages=True)
async def warnings_cmd(ctx, member: discord.Member):
    c.execute("SELECT reason, moderator, timestamp FROM warnings WHERE user_id = ? AND guild_id = ? ORDER BY timestamp DESC", (member.id, ctx.guild.id))
    warns = c.fetchall()
    if not warns:
        await ctx.send(f"✅ {member.mention} ليس لديه تحذيرات")
        return
    embed = discord.Embed(title=f"📋 تحذيرات {member.name}", description=f"**عدد التحذيرات:** {len(warns)}", color=0xFFA500)
    for i, (reason, mod, ts) in enumerate(warns[:10], 1):
        embed.add_field(name=f"⚠️ تحذير #{i}", value=f"**السبب:** {reason}\n**بواسطة:** {mod}\n**التاريخ:** {ts[:19]}", inline=False)
    await ctx.send(embed=embed)

@bot.command(name="kick")
@commands.has_permissions(kick_members=True)
async def kick_cmd(ctx, member: discord.Member, *, reason: str = "مخالفة"):
    if member.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
        await ctx.send("❌ لا يمكنك طرد هذا العضو")
        return
    try:
        await member.kick(reason=reason)
        embed = discord.Embed(title="👢 **طرد**", description=f"**المستخدم:** {member.mention}\n**السبب:** {reason}\n**بواسطة:** {ctx.author.mention}", color=0xFFA500)
        await ctx.send(embed=embed)
        await log_action(ctx.guild, "👢 Kick", str(member), str(ctx.author), reason)
    except Exception as e:
        await ctx.send(f"❌ فشل الطرد: {e}")

@bot.command(name="ban")
@commands.has_permissions(ban_members=True)
async def ban_cmd(ctx, member: discord.Member, *, reason: str = "مخالفة"):
    if member.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
        await ctx.send("❌ لا يمكنك حظر هذا العضو")
        return
    try:
        await member.ban(reason=reason, delete_message_days=0)
        embed = discord.Embed(title="🔨 **حظر**", description=f"**المستخدم:** {member.mention}\n**السبب:** {reason}\n**بواسطة:** {ctx.author.mention}", color=0xFF0000)
        await ctx.send(embed=embed)
        await log_action(ctx.guild, "🔨 Ban", str(member), str(ctx.author), reason)
    except Exception as e:
        await ctx.send(f"❌ فشل الحظر: {e}")

@bot.command(name="unban")
@commands.has_permissions(ban_members=True)
async def unban_cmd(ctx, *, user_input: str):
    banned_users = [entry async for entry in ctx.guild.bans()]
    for ban_entry in banned_users:
        user = ban_entry.user
        if str(user.id) == user_input or user.name.lower() in user_input.lower():
            await ctx.guild.unban(user)
            await ctx.send(f"✅ **تم إلغاء حظر:** {user.mention}")
            await log_action(ctx.guild, "🔓 Unban", str(user), str(ctx.author))
            return
    await ctx.send("❌ لم يتم العثور على المستخدم")

@bot.command(name="mute")
@commands.has_permissions(manage_roles=True)
async def mute_cmd(ctx, member: discord.Member, time: int = 10, *, reason: str = "مخالفة"):
    config = get_config(ctx.guild.id)
    mute_role = ctx.guild.get_role(config["mute_role"])
    if not mute_role:
        mute_role = await ctx.guild.create_role(name="🔇 Muted", color=0x808080)
        update_config(ctx.guild.id, "mute_role", mute_role.id)
        for channel in ctx.guild.channels:
            try:
                await channel.set_permissions(mute_role, send_messages=False, speak=False)
            except:
                pass
    await member.add_roles(mute_role, reason=reason)
    embed = discord.Embed(title="🔇 **كتم**", description=f"**المستخدم:** {member.mention}\n**المدة:** {time} دقيقة\n**السبب:** {reason}\n**بواسطة:** {ctx.author.mention}", color=0x808080)
    await ctx.send(embed=embed)
    await asyncio.sleep(time * 60)
    if mute_role in member.roles:
        await member.remove_roles(mute_role)
        await ctx.send(f"🔊 {member.mention} تم إلغاء كتمك")

@bot.command(name="unmute")
@commands.has_permissions(manage_roles=True)
async def unmute_cmd(ctx, member: discord.Member):
    config = get_config(ctx.guild.id)
    mute_role = ctx.guild.get_role(config["mute_role"])
    if mute_role and mute_role in member.roles:
        await member.remove_roles(mute_role)
        await ctx.send(f"✅ **تم إلغاء كتم:** {member.mention}")
    else:
        await ctx.send("❌ هذا العضو ليس مكتوماً")

@bot.command(name="clear")
@commands.has_permissions(manage_messages=True)
async def clear_cmd(ctx, amount: int = 10):
    if amount > 100:
        amount = 100
    deleted = await ctx.channel.purge(limit=amount + 1)
    msg = await ctx.send(f"✅ **تم مسح {len(deleted) - 1} رسالة**")
    await asyncio.sleep(3)
    await msg.delete()

@bot.command(name="lock")
@commands.has_permissions(manage_channels=True)
async def lock_cmd(ctx, channel: discord.TextChannel = None):
    if not channel:
        channel = ctx.channel
    await channel.set_permissions(ctx.guild.default_role, send_messages=False)
    await ctx.send(f"🔒 **تم قفل:** {channel.mention}")

@bot.command(name="unlock")
@commands.has_permissions(manage_channels=True)
async def unlock_cmd(ctx, channel: discord.TextChannel = None):
    if not channel:
        channel = ctx.channel
    await channel.set_permissions(ctx.guild.default_role, send_messages=True)
    await ctx.send(f"🔓 **تم فتح:** {channel.mention}")

@bot.command(name="slowmode")
@commands.has_permissions(manage_channels=True)
async def slowmode_cmd(ctx, seconds: int = 5):
    await ctx.channel.edit(slowmode_delay=seconds)
    await ctx.send(f"🐌 **تم ضبط الوضع البطيء:** {seconds} ثانية")

@bot.command(name="whitelist")
@commands.has_permissions(administrator=True)
async def whitelist_cmd(ctx, member: discord.Member, *, reason: str = "مصرح به"):
    c.execute("INSERT OR REPLACE INTO whitelist (user_id, reason, added_by) VALUES (?, ?, ?)", (member.id, reason, ctx.author.id))
    conn.commit()
    await ctx.send(f"✅ **تم إضافة {member.mention} إلى القائمة البيضاء**\nالسبب: {reason}")

@bot.command(name="blacklist")
@commands.has_permissions(administrator=True)
async def blacklist_cmd(ctx, member: discord.Member, *, reason: str = "محظور"):
    c.execute("INSERT OR REPLACE INTO blacklist (user_id, reason, added_by) VALUES (?, ?, ?)", (member.id, reason, ctx.author.id))
    conn.commit()
    await ctx.send(f"⛔ **تم إضافة {member.mention} إلى القائمة السوداء**\nالسبب: {reason}")

@bot.command(name="removewl")
@commands.has_permissions(administrator=True)
async def removewl_cmd(ctx, member: discord.Member):
    c.execute("DELETE FROM whitelist WHERE user_id = ?", (member.id,))
    conn.commit()
    await ctx.send(f"✅ **تم إزالة {member.mention} من القائمة البيضاء**")

@bot.command(name="removebl")
@commands.has_permissions(administrator=True)
async def removebl_cmd(ctx, member: discord.Member):
    c.execute("DELETE FROM blacklist WHERE user_id = ?", (member.id,))
    conn.commit()
    await ctx.send(f"✅ **تم إزالة {member.mention} من القائمة السوداء**")

@bot.command(name="listwl")
@commands.has_permissions(administrator=True)
async def listwl_cmd(ctx):
    c.execute("SELECT user_id, reason FROM whitelist")
    data = c.fetchall()
    if not data:
        await ctx.send("📋 القائمة البيضاء فارغة")
        return
    embed = discord.Embed(title="✅ **القائمة البيضاء**", color=0x00FF00)
    for user_id, reason in data:
        user = bot.get_user(user_id)
        embed.add_field(name=f"• {user.name if user else user_id}", value=f"**السبب:** {reason}", inline=False)
    await ctx.send(embed=embed)

@bot.command(name="listbl")
@commands.has_permissions(administrator=True)
async def listbl_cmd(ctx):
    c.execute("SELECT user_id, reason FROM blacklist")
    data = c.fetchall()
    if not data:
        await ctx.send("📋 القائمة السوداء فارغة")
        return
    embed = discord.Embed(title="⛔ **القائمة السوداء**", color=0xFF0000)
    for user_id, reason in data:
        user = bot.get_user(user_id)
        embed.add_field(name=f"• {user.name if user else user_id}", value=f"**السبب:** {reason}", inline=False)
    await ctx.send(embed=embed)

# ======================================================================
# نظام التذاكر
# ======================================================================
@bot.command(name="ticket")
@commands.has_permissions(manage_channels=True)
async def ticket_cmd(ctx, member: discord.Member = None, *, reason: str = "استفسار"):
    if not member:
        member = ctx.author
    # منع التذاكر المتعددة لنفس المستخدم
    c.execute("SELECT * FROM tickets WHERE user_id = ? AND guild_id = ? AND status = 'open'", (member.id, ctx.guild.id))
    if c.fetchone():
        await ctx.send(f"❌ {member.mention} لديه تذكرة مفتوحة بالفعل!")
        return
    config = get_config(ctx.guild.id)
    category = ctx.guild.get_channel(config["ticket_category"]) if config["ticket_category"] else None
    overwrites = {
        ctx.guild.default_role: discord.PermissionOverwrite(read_messages=False),
        member: discord.PermissionOverwrite(read_messages=True, send_messages=True),
        ctx.guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)
    }
    ticket_channel = await ctx.guild.create_text_channel(f"🎫-ticket-{member.name}", category=category, overwrites=overwrites, reason=reason)
    c.execute("INSERT INTO tickets (channel_id, user_id, guild_id) VALUES (?, ?, ?)", (ticket_channel.id, member.id, ctx.guild.id))
    conn.commit()
    embed = discord.Embed(title="🎫 **تذكرة جديدة**", description=f"**المستخدم:** {member.mention}\n**السبب:** {reason}\n**افتحها بواسطة:** {ctx.author.mention}\n\nاكتب `.close` لإغلاق التذكرة", color=0x00FF00)
    await ticket_channel.send(embed=embed)
    await ctx.send(f"✅ **تم فتح تذكرة:** {ticket_channel.mention}")

@bot.command(name="close")
async def close_cmd(ctx):
    c.execute("SELECT * FROM tickets WHERE channel_id = ? AND status = 'open'", (ctx.channel.id,))
    ticket = c.fetchone()
    if not ticket:
        await ctx.send("❌ هذا ليس روم تذكرة")
        return
    embed = discord.Embed(title="🔒 **إغلاق التذكرة**", description="سيتم حذف الروم بعد 5 ثواني...", color=0xFF0000)
    await ctx.send(embed=embed)
    c.execute("UPDATE tickets SET status = 'closed' WHERE channel_id = ?", (ctx.channel.id,))
    conn.commit()
    await asyncio.sleep(5)
    await ctx.channel.delete()

# ======================================================================
# أوامر المعلومات
# ======================================================================
@bot.command(name="stats")
async def stats_cmd(ctx):
    embed = discord.Embed(title="👑 **KING BOT - إحصائيات**", color=0xFFD700)
    embed.add_field(name="📊 السيرفرات", value=f"`{len(bot.guilds)}`", inline=True)
    embed.add_field(name="👥 المستخدمين", value=f"`{sum(g.member_count for g in bot.guilds)}`", inline=True)
    embed.add_field(name="🏓 الباند", value=f"`{round(bot.latency * 1000)}ms`", inline=True)
    embed.add_field(name="📅 الإنشاء", value="`2024`", inline=True)
    embed.add_field(name="🔄 الحالة", value="`🟢 شغال`", inline=True)
    embed.add_field(name="📋 الباند", value=f"`discord.py {discord.__version__}`", inline=True)
    await ctx.send(embed=embed)

@bot.command(name="serverinfo")
async def serverinfo_cmd(ctx):
    guild = ctx.guild
    embed = discord.Embed(title=f"ℹ️ {guild.name}", color=0x3498db)
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    embed.add_field(name="👑 المالك", value=guild.owner.mention, inline=True)
    embed.add_field(name="🆔 المعرف", value=f"`{guild.id}`", inline=True)
    embed.add_field(name="📅 الإنشاء", value=guild.created_at.strftime("%Y-%m-%d"), inline=True)
    embed.add_field(name="👥 الأعضاء", value=f"`{guild.member_count}`", inline=True)
    embed.add_field(name="💬 الرومات", value=f"`{len(guild.channels)}`", inline=True)
    embed.add_field(name="🏷️ الرولات", value=f"`{len(guild.roles)}`", inline=True)
    await ctx.send(embed=embed)

@bot.command(name="userinfo")
async def userinfo_cmd(ctx, member: discord.Member = None):
    if not member:
        member = ctx.author
    embed = discord.Embed(title=f"ℹ️ {member.name}", color=member.color if member.color.value != 0 else 0x3498db)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="📛 الاسم", value=member.mention, inline=True)
    embed.add_field(name="🆔 المعرف", value=f"`{member.id}`", inline=True)
    embed.add_field(name="📅 الانضمام", value=member.joined_at.strftime("%Y-%m-%d") if member.joined_at else "غير معروف", inline=True)
    embed.add_field(name="📅 التسجيل", value=member.created_at.strftime("%Y-%m-%d"), inline=True)
    embed.add_field(name="🏷️ أعلى رول", value=member.top_role.mention, inline=True)
    embed.add_field(name="🤖 بوت", value="نعم" if member.bot else "لا", inline=True)
    await ctx.send(embed=embed)

# ======================================================================
# تشغيل البوت مع إعادة اتصال تلقائي
# ======================================================================
async def main():
    while True:
        try:
            await bot.start(TOKEN)
        except discord.LoginFailure:
            logger.error("فشل تسجيل الدخول - تأكد من صحة التوكن")
            break
        except Exception as e:
            logger.error(f"انقطع الاتصال: {e}. جاري إعادة الاتصال خلال 5 ثوانٍ...")
            await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(main())
