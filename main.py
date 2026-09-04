import os
import io
import re
import json
import tempfile
import hashlib
import signal
import asyncio
import datetime
import time
import random
import secrets
import typing

import discord
from discord import app_commands
from discord.ext import commands, tasks
import httpx
import aiohttp

TOKEN = os.getenv("DISCORD_TOKEN") or os.getenv("TOKEN")
BOT_ORDER_ID = os.getenv("BOT_ORDER_ID", "")
WORKER_TOKEN = os.getenv("WORKER_TOKEN", "")

SUPABASE_URL = os.getenv("SUPABASE_URL", "https://prvqfjairnketwhmfshu.supabase.co")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InBydnFmamFpcm5rZXR3aG1mc2h1Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzY4MDM2NDIsImV4cCI6MjA5MjM3OTY0Mn0.7IRfiBSkw5tM67fxYADmd8MQ619AjEb1v7exa2ZRth8")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY") or SUPABASE_ANON_KEY
SUPABASE_FN_URL = os.getenv("SUPABASE_FN_URL", f"{SUPABASE_URL}/functions/v1")
BOT_API = os.getenv("BOT_API_NAME", "utilities-bot-api")
DASHBOARD_URL = os.getenv("DASHBOARD_URL", "https://oversite.shop/bot-dashboard")

# Which product this deployment is. The same code runs every Roblox-side base;
# the base picks the brand name, which slash commands exist, and which
# dashboard blocks are loaded. This branch IS the Oversite Roleplay codebase:
# it started as a copy of the Network bot and is free to diverge from it.
BOT_BASE = (os.getenv("BOT_BASE") or "roleplay").strip().lower()
BASE_BRANDS = {"customs": "Oversite Customs", "roleplay": "Oversite Roleplay"}
BRAND = BASE_BRANDS.get(BOT_BASE, "Oversite Customs")
SERVER_NAME = os.getenv("SERVER_NAME", BRAND)
ACCENT = 0xC9DBE6

# Slash commands each base keeps. A base not listed here keeps everything.
BASE_COMMANDS = {
    "roleplay": {
        # tickets, join message, verification, group sync, logs
        "ticketadd", "ticketremove", "joinsetup", "infraction", "promote", "infractionroles",
        "promotionroles", "grouproleupdate", "logtest", "logdebug",
        # community
        "giveaway", "shift", "session", "suggestion", "blacklist", "unblacklist", "leaderboard", "invitebonus",
        "resetinvites", "ads", "adsgrant",
        # music, radio, text to speech
        "join", "leave", "set", "play", "skip", "stop", "pause", "resume", "queue", "volume",
        "nowplaying", "favorites", "setmusic", "stopmusic", "radio", "votegenre", "musicdebug",
    },
}
# Dashboard features (bot_config rows) each base loads. Unlisted bases load all.
BASE_FEATURES = {
    "roleplay": {
        "welcome", "invite", "tickets", "roblox-verify", "customs-giveaway", "customs-infraction",
        "customs-promotion", "customs-logging", "music-addon", "auto-radio", "roleplay-shifts", "roleplay-sessions",
        "roblox-group-sync", "customs-messages", "customs-suggestions", "customs-blacklist",
        "customs-smallui", "invite-tracker", "marketplace", "ads", "customs-tts", "customs-gambling",
    },
}


def _base_allows_feature(feature):
    allowed = BASE_FEATURES.get(BOT_BASE)
    return True if allowed is None else feature in allowed


def _prune_commands_for_base():
    """Drop slash commands this base doesn't sell, before the tree syncs."""
    allowed = BASE_COMMANDS.get(BOT_BASE)
    if allowed is None:
        return 0
    removed = 0
    for cmd in list(bot.tree.get_commands()):
        if cmd.name not in allowed:
            bot.tree.remove_command(cmd.name)
            removed += 1
    return removed
BOT_START_TIME = discord.utils.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z")

WELCOME_CHANNEL_ID = os.getenv("WELCOME_CHANNEL_ID", "")
WELCOME_EMOJI_ID = int(os.getenv("WELCOME_EMOJI_ID", "1527943242115579905"))
MEMBER_COUNT_EMOJI_ID = int(os.getenv("MEMBER_COUNT_EMOJI_ID", "1474038929815507096"))
WELCOME_DASHBOARD_CHANNEL_ID = int(os.getenv("WELCOME_DASHBOARD_CHANNEL_ID", "1471291097040031916"))


def _split_ids(raw):
    return [p.strip() for p in (raw or "").split(",") if p.strip()]


TICKET_CATEGORY_ID = os.getenv("TICKET_CATEGORY_ID", "")
TICKET_LOG_CHANNEL_ID = os.getenv("TICKET_LOG_CHANNEL_ID", "")
SUPPORT_ROLE_IDS = _split_ids(os.getenv("SUPPORT_ROLE_IDS"))
CREDIT_MANAGER_ROLE_IDS = _split_ids(os.getenv("CREDIT_MANAGER_ROLE_IDS"))

BUTTON_STYLE_MAP = {
    "primary": 1, "blurple": 1,
    "secondary": 2, "grey": 2, "gray": 2,
    "success": 3, "green": 3,
    "danger": 4, "red": 4,
    "link": 5,
}

intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)

welcome_config = {"enabled": True, "channel_id": WELCOME_CHANNEL_ID, "message": ""}
invite_config = {"channel_id": "", "components": [], "embeds": [], "messages": []}
ticket_config = {
    "category_id": TICKET_CATEGORY_ID,
    "support_role_ids": SUPPORT_ROLE_IDS,
    "log_channel_id": TICKET_LOG_CHANNEL_ID,
    "open_message": "",
    "ping_support": True,
    "one_per_user": True,
    # Rich panel (posted to a channel) + a list of ticket TYPES, each with its
    # own Open button (label/color) and its own opening message. types = [
    #   {id, name, button_label, button_style, open_components:[...]}, ... ]
    "panel_channel_id": "",
    "panel_components": [],
    "panels": [],  # [{channel_id, components}, ...] — every panel, all registered/posted
    "types": [],
    "panel_refs": {},  # channel_id -> last panel message id (one panel kept per channel)
}

# Registry mapping a clicked Ticket/Ephemeral component back to the message the
# dashboard designed for it. Rebuilt from panel_components on every apply_config
# (and on boot), so it survives restarts.
ticket_msgs = {}   # key -> open_components (Ticket buttons/options)
eph_msgs = {}      # key -> open_components (Ephemeral buttons/options)
form_msgs = {}     # key -> open_components (Form buttons/options — collect {Question:} answers first)
form_titles = {}   # key -> modal title (the button/option label)
ticket_categories = {}  # key -> category name a Ticket/Form drops its channels into
ticket_access = {}      # key -> comma-separated role names that can see a Ticket/Form's channels
# Purchase buttons designed in a message (a "Purchase" component / a button set to
# Purchase). key -> {title, price, methods, msa_url}. Clicking runs the package
# purchase flow (payment picker + gift/recipient + MSA agreement).
purchase_msgs = {}

_PURCHASE_METHOD_LABELS = {"devproduct": "Dev Product", "select": "Roblox Select", "stripe": "Stripe"}


def _purchase_cfg_from(comp):
    """Normalize a Purchase component / button into its stored config."""
    methods = comp.get("methods")
    if not isinstance(methods, list) or not methods:
        methods = ["devproduct", "select", "stripe"]
    # Legacy configs stored "gamepass" — that method is now Roblox dev products.
    methods = ["devproduct" if m == "gamepass" else m for m in methods]
    seen = set()
    methods = [m for m in methods if m in _PURCHASE_METHOD_LABELS and not (m in seen or seen.add(m))] or ["stripe"]
    return {
        "title": str(comp.get("title") or comp.get("product") or comp.get("label") or "Purchase").strip(),
        "price": str(comp.get("price") or comp.get("price_line") or "").strip(),
        "methods": methods,
        "msa_url": str(comp.get("msa_url") or "").strip(),
        "button_label": str(comp.get("button_label") or comp.get("label") or "Purchase").strip() or "Purchase",
        # Donation mode: the buyer chooses USD or Robux and types the amount.
        "donation": bool(comp.get("donation")),
        # Quantity mode: a display card — the button is an unclickable badge.
        "quantity": bool(comp.get("quantity")),
        # Pre-made product's page link — used verbatim so nothing is created.
        "buy_url": str(comp.get("buy_url") or "").strip(),
    }


def _register_purchase_from_tree(tree):
    """Additively register any Purchase components found in a V2 tree, so their
    buttons keep working after a restart on any surface (panels, saved messages,
    portfolio, packages, …). Idempotent — keys are stable."""
    def _walk(items, depth):
        if depth > 8:
            return
        for c in (items or []):
            if not isinstance(c, dict):
                continue
            if c.get("type") == "purchase":
                purchase_msgs[_comp_key(c)] = _purchase_cfg_from(c)
            t = c.get("type")
            if t == "container":
                _walk(c.get("children") or c.get("components") or [], depth + 1)
    _walk(tree if isinstance(tree, list) else [], 0)


# ===================== Advertisement system =====================
# Users buy ad perks (via Purchase components) -> they land in a per-user
# inventory. To post, they spend one PING credit (Everyone/Here/No Ping) and may
# apply Instant Post or Bypass Queue add-ons. Every ad is staff-approved, then
# posted now (Instant), via a priority Bypass lane, or via the normal queue.
# ---- Economy / Gambling (UnbelievaBoat-style) ----
# Configured in the dashboard "Economy & Gambling" block. Members have a cash +
# bank balance in a server currency and gamble it with the prefix commands.
gambling_config = {
    "enabled": False,
    "prefix": "!",
    "currency_symbol": "🪙",
    "currency_name": "coins",
    "start_balance": 0,
    "max_balance": 0,                 # 0 = unlimited
    "audit_log_channel_id": "",
    "allowed_channel_ids": [],        # if set, commands only work in these channels
    "admin_role_ids": [],             # who can add/remove money etc. (else Manage Server)
    # Per-command cooldown seconds and payout/fine ranges.
    "cooldowns": {"work": 3600, "slut": 3600, "crime": 3600, "rob": 3600},
    "payouts": {"work": [20, 250], "slut": [100, 400], "crime": [250, 700]},
    "fines": {"slut": [40, 250], "crime": [100, 400]},   # min,max fine on fail
    "fail_rate": {"slut": 0.35, "crime": 0.45},          # 0..1
    "fine_type": "percent",          # "percent" of cash, or "fixed"
    "rob_success_rate": 0.5,
    # Role income (periodic) + buyable properties (passive income via collect).
    "role_income": [],   # [{"role_id","amount","interval_minutes"}]
    "properties": [],    # [{"id","name","price","income","interval_minutes","max"}]
}

# Realistic income businesses used when the owner hasn't defined a custom list
# in the dashboard. Members buy them from the shop; they pay out every cycle via
# `collect`. Prices/income are tuned so each pays for itself in ~20-25 cycles.
ECON_DEFAULT_PROPERTIES = [
    {"id": "newsstand",  "name": "🗞️ Newspaper Stand", "price": 750,     "income": 45,    "interval_minutes": 60, "max": 5},
    {"id": "vending",    "name": "🥤 Vending Machine",  "price": 2500,    "income": 130,   "interval_minutes": 60, "max": 5},
    {"id": "carwash",    "name": "🧽 Car Wash",         "price": 6000,    "income": 300,   "interval_minutes": 60, "max": 5},
    {"id": "gasstation", "name": "⛽ Gas Station",      "price": 15000,   "income": 720,   "interval_minutes": 60, "max": 5},
    {"id": "diner",      "name": "🍔 Diner",            "price": 40000,   "income": 1850,  "interval_minutes": 60, "max": 5},
    {"id": "hotel",      "name": "🏨 Hotel",            "price": 120000,  "income": 5200,  "interval_minutes": 60, "max": 3},
    {"id": "nightclub",  "name": "🪩 Nightclub",        "price": 300000,  "income": 12500, "interval_minutes": 60, "max": 3},
    {"id": "casino",     "name": "🎰 Casino",           "price": 1000000, "income": 40000, "interval_minutes": 60, "max": 2},
]


def _econ_properties():
    """The buyable property list: the owner's custom list if set, else the
    realistic default businesses above."""
    custom = gambling_config.get("properties")
    if isinstance(custom, list) and custom:
        return [p for p in custom if p.get("id")]
    return ECON_DEFAULT_PROPERTIES

# economy_data: guild_id(str) -> { user_id(str): {cash,bank,cd:{cmd:ts},
#   props:{prop_id:{n,last}}, inc_last} }.  Persisted to bot_config "economy-data".
economy_data = {}
_econ_loaded = False
_econ_dirty = False
_econ_save_task = None
_econ_chat_cd = {}  # (gid,uid) -> ts  (in-memory chat-money cooldown)


def _econ_users(gid):
    return economy_data.setdefault(str(gid), {})


def _econ_u(gid, uid):
    u = _econ_users(gid).setdefault(str(uid), {})
    if "cash" not in u:
        u["cash"] = int(gambling_config.get("start_balance") or 0)
        u["bank"] = 0
    u.setdefault("cash", 0); u.setdefault("bank", 0)
    u.setdefault("cd", {}); u.setdefault("props", {}); u.setdefault("inc_last", 0)
    return u


def _econ_sym():
    return gambling_config.get("currency_symbol") or "🪙"


def _econ_fmt(n):
    return f"{_econ_sym()} {int(n):,}"


def _econ_total(u):
    return int(u.get("cash", 0)) + int(u.get("bank", 0))


def _econ_add(gid, uid, amount, where="cash"):
    """Add (or subtract) money, clamped to >=0 and the max-balance cap on total."""
    u = _econ_u(gid, uid)
    u[where] = int(u.get(where, 0)) + int(amount)
    if u[where] < 0:
        u[where] = 0
    mx = int(gambling_config.get("max_balance") or 0)
    if mx > 0 and _econ_total(u) > mx:
        over = _econ_total(u) - mx
        u[where] = max(0, u[where] - over)
    _save_econ_soon()
    return u[where]


def _econ_is_admin(member):
    ids = set(str(x) for x in (gambling_config.get("admin_role_ids") or []))
    if ids and any(str(r.id) in ids for r in getattr(member, "roles", [])):
        return True
    return bool(getattr(getattr(member, "guild_permissions", None), "manage_guild", False))


def _save_econ_soon():
    global _econ_save_task, _econ_dirty
    _econ_dirty = True
    if _econ_save_task and not _econ_save_task.done():
        return
    _econ_save_task = asyncio.create_task(_save_econ())


async def _save_econ():
    global _econ_dirty
    await asyncio.sleep(4)
    if not _econ_loaded:
        return
    ok, err = await _bot_config_upsert("economy-data", {"guilds": economy_data})
    if ok:
        _econ_dirty = False
    else:
        # Leave _econ_dirty set so the autosave loop / shutdown flush retries.
        print(f"[Econ] debounced save failed (will retry): {err}")


async def _econ_flush_now(attempts=6):
    if not _econ_loaded:
        return False
    err = ""
    for i in range(attempts):
        ok, err = await _bot_config_upsert("economy-data", {"guilds": economy_data})
        if ok:
            global _econ_dirty
            _econ_dirty = False
            return True
        if i < attempts - 1:
            await asyncio.sleep(0.8 * (i + 1))
    print(f"[Econ] flush failed: {err}")
    return False


async def _econ_fetch(attempts=6):
    """Read economy-data, distinguishing a genuine (possibly empty) result from a
    transient failure. Returns (ok, config). ok=False means DON'T trust the data
    and DON'T enable saving — otherwise a failed read would let us overwrite
    everyone's stored balances with an empty snapshot."""
    return await _durable_config_get("economy-data", attempts=attempts)


def _econ_apply_loaded(cfg):
    guilds = (cfg or {}).get("guilds")
    if isinstance(guilds, dict):
        economy_data.clear()
        economy_data.update(guilds)
        n = sum(len(u) for u in economy_data.values())
        print(f"[Econ] restored {n} balance(s)")
    else:
        print("[Econ] no saved data yet (fresh)")


async def _econ_reload_until_loaded():
    """If the boot read failed, keep retrying in the background. Saving stays
    disabled (so stored data is never clobbered) until a read finally succeeds."""
    global _econ_loaded
    delay = 15
    while not _econ_loaded:
        await asyncio.sleep(delay)
        ok, cfg = await _econ_fetch(attempts=1)
        if ok:
            _econ_apply_loaded(cfg)
            _econ_loaded = True
            print("[Econ] background reload succeeded — persistence re-enabled")
            return
        delay = min(120, delay + 15)


@tasks.loop(seconds=90)
async def econ_autosave():
    """Belt-and-suspenders: periodically persist economy balances if there are
    unsaved changes, so an ungraceful kill (no clean shutdown) loses at most
    ~90s of activity instead of everything since the last debounce."""
    if _econ_loaded and _econ_dirty:
        await _econ_flush_now(attempts=3)


@econ_autosave.before_loop
async def _econ_autosave_before():
    await bot.wait_until_ready()


async def _load_econ():
    global _econ_loaded
    ok, cfg = await _econ_fetch()
    if not ok:
        _econ_loaded = False
        print("[Econ] load FAILED — persistence disabled this session until a "
              "background reload succeeds. Stored balances/properties are safe "
              "(nothing will be overwritten).")
        asyncio.create_task(_econ_reload_until_loaded())
        return
    _econ_apply_loaded(cfg)
    _econ_loaded = True


async def _econ_audit(guild, text):
    ch_id = gambling_config.get("audit_log_channel_id")
    if not ch_id:
        return
    ch = guild.get_channel(int(ch_id)) if str(ch_id).isdigit() else None
    if ch:
        try:
            await ch.send(embed=info_embed("Economy", text))
        except Exception:
            pass


ADS_PERK_KEYS = ["ping_everyone", "ping_here", "ping_none", "instant", "bypass"]
ADS_PING_KEYS = ["ping_everyone", "ping_here", "ping_none"]
_ADS_PING_CONTENT = {"ping_everyone": "@everyone", "ping_here": "@here", "ping_none": ""}

ads_config = {
    "enabled": False,
    "post_channel_id": "",
    "approval_channel_id": "",
    "staff_role_ids": [],
    "interval_minutes": 60,
    # Purchasable item name for each perk (owner matches these to the Purchase
    # cards). Matching a claimed purchase by name grants that perk.
    "perks": {
        "ping_everyone": "Everyone Ping",
        "ping_here": "Here Ping",
        "ping_none": "No Ping",
        "instant": "Instant Post",
        "bypass": "Bypass Queue",
    },
    "regular_design": [],    # V2 tree; tokens {advertiser} {server_link} {ping}
    "giveaway_design": [],   # V2 tree; tokens {advertiser} {prize} {winners} {duration} {ping}
    "claim_design": [],      # V2 tree for the claim panel message; token {inventory}
    "empty_design": [],      # V2 tree shown when they own nothing yet
    "noposts_design": [],    # V2 tree shown when applying an add-on with no active post
    "claim_button_label": "📢 Post an Ad",
    # Wording of the ephemeral "post an ad" panel (all customizable).
    "claim_title": "Your Ad Inventory",
    "claim_note": "",
    "ping_placeholder": "Choose an item to use",
    "type_placeholder": "Post type",
    "addon_placeholder": "Apply an add-on (optional)",
    "continue_label": "Continue",
    "regular_label": "Regular Post",
    "giveaway_label": "Sponsored Giveaway",
}
ads_data = {}  # guild_id(str) -> {"inventory": {uid: {perk: n}}, "queue": [ad], "bypass": [ad], "last_drip": 0}
_ads_save_pending = None
_ads_dirty = False
# Guard against DATA LOSS: only persist ad inventory once we've SUCCESSFULLY read
# the stored copy at boot. If the boot read failed (transient 5xx/network), the
# in-memory ads_data is empty — writing that back would wipe everyone's real
# inventory. So every save path checks this flag and no-ops until a good load.
_ads_loaded = False
_pending_perk_grant = {}  # (pkg_msg_id, buyer_id) -> (guild_id, deliver_to, perk_key)
_ads_pending = {}  # ad_id -> ad dict awaiting approval (also mirrored in ads_data)
_ads_claim_state = {}  # user_id -> {"ping","type","addon"} for the designed claim panel
_ads_render_viewer = None    # (guild_id, user_id) set while rendering a claim panel
_ads_inventory_placed = False  # True if the design contained an "inventory" select


def _ads_g(guild_id):
    return ads_data.setdefault(str(guild_id), {"inventory": {}, "queue": [], "bypass": [], "last_drip": 0})


def _ads_inventory(guild_id, user_id):
    return _ads_g(guild_id)["inventory"].get(str(user_id), {})


# Built-in perk names + short aliases. Used as a fallback so a standard card
# title ("No Ping", "Everyone Ping", …) always resolves to its perk even if the
# dashboard's perk labels were renamed or the ads config didn't load.
_ADS_DEFAULT_LABELS = {
    "ping_everyone": "Everyone Ping",
    "ping_here": "Here Ping",
    "ping_none": "No Ping",
    "instant": "Instant Post",
    "bypass": "Bypass Queue",
}
_ADS_NAME_ALIASES = {
    "everyone ping": "ping_everyone", "ping everyone": "ping_everyone", "everyone": "ping_everyone",
    "here ping": "ping_here", "ping here": "ping_here", "here": "ping_here",
    "no ping": "ping_none", "ping none": "ping_none", "none": "ping_none",
    "instant post": "instant", "instant": "instant",
    "bypass queue": "bypass", "skip queue": "bypass", "bypass": "bypass",
}


def _ads_norm_name(s):
    """Lowercase, drop a trailing 'Donation …' suffix, strip emoji/punctuation
    (keep letters, digits, spaces), and collapse whitespace. So '🔔 No Ping!' and
    'no  ping' both normalize to 'no ping'."""
    s = re.sub(r"\s+donation.*$", "", str(s or "").lower())
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _ads_perk_for_name(name):
    """Match a card/product name to a perk key. Tries the dashboard's configured
    perk labels first, then the built-in default names and short aliases, so
    matching never silently fails just because a label drifted."""
    n = _ads_norm_name(name)
    if not n:
        return None
    for key, label in (ads_config.get("perks") or {}).items():
        if key in ADS_PERK_KEYS and _ads_norm_name(label) == n:
            return key
    for key, label in _ADS_DEFAULT_LABELS.items():
        if _ads_norm_name(label) == n:
            return key
    return _ADS_NAME_ALIASES.get(n)


# Pre-made Roblox developer products for the ad perks (owner-created, so nothing
# is created at buy time and no Open Cloud API key is needed). Keyed by perk key.
# The web product page is roblox.com/developer-product/<experience>/product/<id>.
ADS_STORE_EXPERIENCE_ID = "10357040169"
ADS_PERK_PRODUCT_IDS = {
    "instant": "3710170569",
    "bypass": "3710171039",
    "ping_everyone": "3710171057",
    "ping_here": "3710171068",
    "ping_none": "3710171078",
}


def _ads_product_buy_url(name):
    """Direct buy link for a pre-made ad-perk product, matched by the card title.
    Returns '' if the title isn't one of the known perks."""
    perk = _ads_perk_for_name(name)
    pid = ADS_PERK_PRODUCT_IDS.get(perk or "")
    if not pid:
        return ""
    return f"https://www.roblox.com/developer-product/{ADS_STORE_EXPERIENCE_ID}/product/{pid}"


def _ads_grant(guild_id, user_id, perk_key, n=1):
    if perk_key not in ADS_PERK_KEYS:
        return
    inv = _ads_g(guild_id)["inventory"].setdefault(str(user_id), {})
    inv[perk_key] = int(inv.get(perk_key, 0)) + n
    _save_ads_soon()


def _ads_consume(guild_id, user_id, perk_key, n=1):
    inv = _ads_g(guild_id)["inventory"].get(str(user_id), {})
    if int(inv.get(perk_key, 0)) < n:
        return False
    inv[perk_key] = int(inv.get(perk_key, 0)) - n
    if inv[perk_key] <= 0:
        inv.pop(perk_key, None)
    _save_ads_soon()
    return True


def _ads_perk_label(perk_key):
    return (ads_config.get("perks") or {}).get(perk_key) or perk_key


def _save_ads_soon():
    global _ads_save_pending, _ads_dirty
    _ads_dirty = True  # a periodic loop also flushes this, as a crash safety net
    if _ads_save_pending and not _ads_save_pending.done():
        return
    _ads_save_pending = asyncio.create_task(_save_ads())


async def _save_ads():
    global _ads_dirty
    await asyncio.sleep(4)
    if not _ads_loaded:
        # Never overwrite the stored inventory with an in-memory copy we couldn't
        # verify at boot.
        return
    await _bot_config_upsert("ads-data", {"guilds": ads_data})
    _ads_dirty = False


def _ads_inventory_count():
    """How many member inventories are currently held (for snapshot logging)."""
    return sum(len(gd.get("inventory", {})) for gd in ads_data.values())


async def _ads_flush_now(attempts=5):
    """Snapshot the full ad state (every member's inventory + the queues) to
    storage RIGHT NOW, retrying transient failures. This is the 'save everyone's
    inventory before a redeploy' step — the boot restore hands it all back.
    Returns True once the write is confirmed, False if it couldn't be saved."""
    if not _ads_loaded:
        # Boot read failed this session — the stored copy is the good one, so
        # leave it untouched rather than snapshotting an unverified state.
        return False
    global _ads_dirty
    err = ""
    for i in range(attempts):
        ok, err = await _bot_config_upsert("ads-data", {"guilds": ads_data})
        if ok:
            _ads_dirty = False
            return True
        if i < attempts - 1:
            await asyncio.sleep(0.8 * (i + 1))
    print(f"[Ads] snapshot failed after {attempts} tries: {err}")
    return False


def _normalize_invite(link):
    """Every advertised link here is a Discord invite, so always normalize it to
    https://discord.gg/<code> WITHOUT ever changing the code itself. Handles a
    bare code ('ovs'), a missing scheme, a misspelled domain ('dicord.gg'), and
    discord.com/invite/<code>."""
    s = (link or "").strip()
    if not s:
        return s
    segs = [seg for seg in re.split(r"[\\/]+", s) if seg]
    # Drop scheme/domain tokens (they contain '.' or ':') and any 'invite' path
    # word; whatever's left, its last item, is the invite code — kept exactly.
    path = [seg for seg in segs if "." not in seg and ":" not in seg and seg.lower() != "invite"]
    code = path[-1] if path else (segs[-1] if segs else s)
    return f"https://discord.gg/{code}"


async def _ads_fetch_data():
    """Read the stored ad state directly from PostgREST with retries.

    Returns (ok, guilds). ok=False means the read genuinely FAILED (network /
    5xx after retries) — the caller must NOT persist, or it would overwrite the
    stored inventory with an empty one. ok=True means we got a definitive answer:
    `guilds` is the saved dict, or None when there's simply no saved row yet."""
    if not (SUPABASE_URL and SUPABASE_KEY and BOT_ORDER_ID):
        return True, None  # no backend configured — nothing to lose
    url = f"{SUPABASE_URL}/rest/v1/bot_config?bot_id=eq.{BOT_ORDER_ID}&feature=eq.ads-data&select=config"
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    attempts = 5
    for i in range(attempts):
        last = i == attempts - 1
        try:
            async with _http() as client:
                r = await client.get(url, headers=headers, timeout=15)
            if r.status_code == 200:
                rows = r.json()
                if not rows:
                    return True, None  # fresh bot — no saved inventory yet
                cfg = rows[0].get("config") or {}
                return True, cfg.get("guilds")
            if r.status_code >= 500 and not last:
                print(f"[Ads] load HTTP {r.status_code}, retry {i+1}/{attempts-1}")
                await asyncio.sleep(1.0 * (i + 1))
                continue
            print(f"[Ads] load HTTP {r.status_code} — treating as read failure")
            return False, None
        except Exception as e:
            if not last:
                print(f"[Ads] load failed: {e}; retry {i+1}/{attempts-1}")
                await asyncio.sleep(1.0 * (i + 1))
                continue
            print(f"[Ads] load failed: {e}")
            return False, None
    return False, None


async def _load_ads():
    global _ads_loaded
    ok, guilds = await _ads_fetch_data()
    if not ok:
        # Read failed after retries. Do NOT mark loaded — this keeps every save
        # path disabled so we never overwrite the stored inventory with the empty
        # in-memory one. The next redeploy (or a later retry) can still restore it.
        _ads_loaded = False
        print("[Ads] load FAILED — persistence disabled this session to protect stored inventory")
        return
    _ads_loaded = True  # safe to persist now (even if there was nothing to load)
    if isinstance(guilds, dict):
        ads_data.clear()
        ads_data.update(guilds)
        n_inv = sum(len(gd.get("inventory", {})) for gd in ads_data.values())
        n_q = sum(len(gd.get("queue", [])) + len(gd.get("bypass", [])) for gd in ads_data.values())
        n_p = sum(len(gd.get("pending", {})) for gd in ads_data.values())
        print(f"[Ads] restored inventory:{n_inv} queued:{n_q} pending:{n_p}")
    else:
        print("[Ads] no saved data yet (fresh) — persistence enabled")


# Marketplace = a second, independent ticket system (its own category, support
# roles, log, and panels). Same open/claim/close flow as Tickets; which settings
# apply is chosen per button by the source its panel came from (see _key_source).
marketplace_config = {
    "category_id": "",
    "support_role_ids": [],
    "log_channel_id": "",
    "open_message": "",
    "ping_support": True,
    "one_per_user": True,
    "panel_channel_id": "",
    "panel_components": [],
    "panels": [],
    "types": [],
    "panel_refs": {},
}
# Which settings block a given Ticket/Form button uses, keyed by its message key.
_source_settings = {"tickets": ticket_config, "marketplace": marketplace_config}
_key_source = {}  # message key -> source feature ("tickets" | "marketplace")


def _settings_for_category(category):
    """Pick the settings block (Tickets vs Marketplace) for a clicked button.
    `category` is the custom_id path (e.g. 'ticket_msg:<key>')."""
    mk = category.split(":", 1)[1] if category and ":" in str(category) else category
    return _source_settings.get(_key_source.get(mk, "tickets"), ticket_config)

# ---- Giveaways ----
# Look designed in the dashboard "Giveaway" block (feature "customs-giveaway").
# Every field is optional — the bot has sensible defaults so /giveaway works with
# no config at all.
giveaway_config = {
    "title": "🎉 GIVEAWAY 🎉",
    "color": ACCENT,
    "button_label": "🎉 Enter",
    "host_line": "",          # extra line under the prize (rules, host note, etc.)
    "ping": "",               # optional role/text pinged with the giveaway post
    "default_winners": 1,
    "default_duration": "1d",
    "manager_role_ids": [],   # roles (besides Manage Server) allowed to run /giveaway
    "components": [],         # optional V2 design shown while the giveaway runs
    "ended_components": [],   # optional V2 design shown once the giveaway ends
}
# Live giveaways this process is tracking. Keyed by a short giveaway id (gid) that
# also lives in the Enter button's custom_id, so entries route back here.
# gid -> {message_id, channel_id, guild_id, prize, winners, end_ts, host_id,
#         entrants:set[str], ended:bool}
active_giveaways = {}

# Saved messages built in the dashboard "Messages" block. Works like ticket
# panels: a library of messages, one per channel, each re-posted (replacing the
# previous one in that channel) when saved. `refs` tracks the live message id per
# channel so a re-save edits in place instead of stacking duplicates.
saved_messages_config = {"messages": [], "refs": {}}  # messages: [{channel_id, components}]

# ---- Music / DJ (dashboard "Music Add-On" + "Auto Radio" blocks) ----
# Voice playback is delegated to a shared Lavalink node (see /music) via wavelink.
# The bot never touches audio — no PyNaCl/ffmpeg/yt-dlp — so there's no YouTube
# bot-check treadmill. `enabled` flips true once the dashboard saves the Music
# Add-On config. Needs LAVALINK_URI + LAVALINK_PASSWORD env vars.
music_config = {
    "enabled": False,
    "dj_role_ids": [],
    "everyone_can_queue": True,
    "max_queue_length": 100,
    "default_volume": 50,
    "auto_leave": True,
    "now_playing_v2": False,
    "radio_channel_id": "",
    "radio_genre": "pop",
}

# wavelink talks to the shared Lavalink node. Imported here so the boot status
# line can report it; the actual node connection happens in setup_hook.
try:
    import wavelink
except Exception:
    wavelink = None
# e.g. https://oversite-music.up.railway.app  (wavelink derives wss:// from https)
LAVALINK_URI = os.getenv("LAVALINK_URI") or os.getenv("LAVALINK_URL") or ""
LAVALINK_PASSWORD = os.getenv("LAVALINK_PASSWORD") or os.getenv("LAVALINK_SERVER_PASSWORD") or ""
_lavalink_connected = False

# ---- Logging ----
# The dashboard "Logging" block. Purchase logs post every completed Stripe
# (/payment) and Roblox group game-pass purchase to a channel.
logging_config = {"purchase_log_channel_id": "", "purchase_components": []}

# ---- Form logs (/orderlog, /infraction, /promote) ----
# Each pops a form built from the {Question:} tokens in its design, then posts
# the completed message (answers filled in) to its configured channel.
FORM_LOG_DEFS = {
    "customs-infraction": {"key": "infraction", "title": "Infraction Log"},
    "customs-promotion":  {"key": "promotion",  "title": "Promotion Log"},
}
form_log_configs = {
    d["key"]: {"components": [], "channel_id": "", "allowed_role_ids": [],
               "run_role_ids": [], "watched_role_ids": [], "groups": []}
    for d in FORM_LOG_DEFS.values()
}
form_log_titles = {d["key"]: d["title"] for d in FORM_LOG_DEFS.values()}


def _parse_role_groups(cfg):
    """Watched role SETS from a saved config: group{i}_roles + group{i}_min.
    A set triggers when >= min of its roles change (min<=0/>len ⇒ all of them)."""
    groups = []
    for i in range(1, 7):
        rids = [str(x) for x in (cfg.get(f"group{i}_roles") or []) if x]
        if not rids:
            continue
        try:
            mn = int(cfg.get(f"group{i}_min") or 0)
        except Exception:
            mn = 0
        groups.append({"roles": set(rids), "min": mn})
    # Back-compat: a flat watched_role_ids list = a set where any one triggers.
    legacy = [str(x) for x in (cfg.get("watched_role_ids") or []) if x]
    if legacy:
        groups.append({"roles": set(legacy), "min": 1})
    return groups

def _msg_key(open_components, label=""):
    raw = json.dumps(open_components or [], sort_keys=True) + "|" + (label or "")
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:12]

def _comp_key(x):
    # Prefer the component's stable id (untouched by {user} substitution). Fall
    # back to a content hash only for components that carry no id (e.g. options).
    cid = x.get("id")
    if cid:
        return str(cid)[:64]
    return _msg_key(x.get("open_components"), x.get("label", ""))

def _register_ticket_components(panels):
    """Register the interactive components (Ticket/Form/Ephemeral) from EVERY
    panel so all posted panels keep working — not just the most recent one.
    `panels` accepts a list of (source, tree) pairs (source = the feature the
    panel came from, e.g. 'tickets' or 'marketplace'), or a plain list of trees /
    a single tree (treated as source 'tickets') for backward compatibility."""
    # NOTE: eph_msgs is intentionally NOT cleared here — ephemeral messages live on
    # many surfaces (saved messages, packages, …), not just ticket panels, so
    # wiping it on a ticket save used to break their buttons ("Nothing here").
    # Keys are stable, so stale entries are harmless.
    ticket_msgs.clear(); form_msgs.clear(); form_titles.clear(); ticket_categories.clear(); ticket_access.clear(); _key_source.clear()

    def _reg(x, source):
        oc = x.get("open_components") or []
        if "ticket" in x:
            k = _comp_key(x)
            ticket_msgs[k] = oc
            ticket_categories[k] = (x.get("category_name") or "").strip()
            ticket_access[k] = (x.get("access_roles") or "").strip()
            _key_source[k] = source
        elif "form" in x:
            k = _comp_key(x)
            form_msgs[k] = oc
            form_titles[k] = x.get("label") or "Application"
            ticket_categories[k] = (x.get("category_name") or "").strip()
            ticket_access[k] = (x.get("access_roles") or "").strip()
            _key_source[k] = source
        elif "ephemeral" in x:
            eph_msgs[_comp_key(x)] = oc
        # A Ticket/Ephemeral message can itself contain more Ticket/Ephemeral
        # buttons, so register the ones nested inside it too.
        if oc:
            walk(oc, 0, source)

    def walk(items, depth, source):
        if depth > 8:
            return
        for c in (items or []):
            if not isinstance(c, dict):
                continue
            t = c.get("type")
            if t == "purchase":
                purchase_msgs[_comp_key(c)] = _purchase_cfg_from(c)
            if t == "container":
                walk(c.get("children") or c.get("components") or [], depth + 1, source)
            elif t in ("buttonRow", "button_row", "buttons", "action_row"):
                for b in (c.get("buttons") or []):
                    if isinstance(b, dict):
                        _reg(b, source)
            elif t in ("select_menu", "select"):
                for o in (c.get("options") or []):
                    if isinstance(o, dict):
                        _reg(o, source)
            elif t == "section":
                b = c.get("button")
                if isinstance(b, dict):
                    _reg(b, source)

    # Normalize into a list of (source, tree). Accept (source, tree) pairs, a
    # single tree, or a plain list of trees.
    sourced = []
    for entry in (panels or []):
        if isinstance(entry, tuple) and len(entry) == 2:
            sourced.append(entry)
        elif isinstance(entry, list):
            sourced.append(("tickets", entry))
        elif isinstance(entry, dict):
            # a bare tree passed as a single list of dict items
            sourced = [("tickets", panels)]
            break
    for source, tree in sourced:
        if isinstance(tree, list):
            walk(tree, 0, source)
    print(f"[Tickets] registry: {len(ticket_msgs)} ticket + {len(form_msgs)} form + {len(eph_msgs)} ephemeral messages")
    print(f"[Tickets] registry built: tickets={{{', '.join(f'{k}:{len(v)}' for k,v in ticket_msgs.items())}}} eph={{{', '.join(f'{k}:{len(v)}' for k,v in eph_msgs.items())}}}")
    _schedule_eph_save()


# ---- Ephemeral-message registry persistence ----
# The value baked into a posted select option / button is "eph:<key>", where the
# key is the design component's id or a CONTENT HASH. Editing that option in the
# dashboard changes the hash, so an already-posted message points at a key the
# fresh config no longer registers — the click found nothing. Persisting every
# key->content pair we ever register keeps old posted messages working forever.
_eph_persist_task = None


async def _load_eph_registry():
    cfg = await _bot_config_get("eph-registry")
    saved = (cfg or {}).get("messages")
    if isinstance(saved, dict):
        n = 0
        for k, v in saved.items():
            if isinstance(v, list) and v and k not in eph_msgs:
                eph_msgs[k] = v
                n += 1
        print(f"[Tickets] eph registry: restored {n} entr{'y' if n == 1 else 'ies'} for older posted messages (total {len(eph_msgs)})")


def _schedule_eph_save():
    """Debounced persist of the ephemeral registry (content included)."""
    global _eph_persist_task
    if _eph_persist_task and not _eph_persist_task.done():
        return

    async def _run():
        await asyncio.sleep(5)
        try:
            data = {k: v for k, v in eph_msgs.items() if isinstance(v, list) and v}
            if len(data) > 300:  # cap defensively; oldest entries drop first
                data = dict(list(data.items())[-300:])
            await _bot_config_upsert("eph-registry", {"messages": data})
        except Exception as e:
            print(f"[Tickets] eph registry save failed: {e}")

    try:
        _eph_persist_task = asyncio.create_task(_run())
    except Exception:
        pass


def _register_eph_from_tree(tree):
    """Additively register Ephemeral-message buttons/options found anywhere in a
    V2 component tree, so they work after a restart on surfaces that aren't
    reposted on boot (e.g. saved messages, package/portfolio panels). Idempotent."""
    def _reg(x):
        if isinstance(x, dict) and "ephemeral" in x:
            eph_msgs[_comp_key(x)] = x.get("open_components") or []
        oc = x.get("open_components") if isinstance(x, dict) else None
        if oc:
            _walk(oc, 0)

    def _walk(items, depth):
        if depth > 8:
            return
        for c in (items or []):
            if not isinstance(c, dict):
                continue
            t = c.get("type")
            if t == "purchase":
                purchase_msgs[_comp_key(c)] = _purchase_cfg_from(c)
            if t == "container":
                _walk(c.get("children") or c.get("components") or [], depth + 1)
            elif t in ("buttonRow", "button_row", "buttons", "action_row"):
                for b in (c.get("buttons") or []):
                    _reg(b)
            elif t in ("select_menu", "select"):
                for o in (c.get("options") or []):
                    _reg(o)
            elif t == "section":
                _reg(c.get("button") or {})

    _walk(tree if isinstance(tree, list) else [], 0)
    _schedule_eph_save()


# Ticket panels can come from more than one dashboard block — the main "Tickets"
# block and the "Order Log" block. Each registers its panels + types here under
# its feature key; the registry, posted-panel list, and type list are rebuilt
# from ALL sources so neither block wipes the other's buttons. Order-log tickets
# route by each Ticket/Form button's own category + access roles, so they open in
# their own category independently of regular tickets.
_ticket_sources = {}  # feature -> {"panels": [{channel_id, components}], "types": [type defs]}


def _parse_ticket_panels(cfg):
    raw_panels = cfg.get("panels")
    panels = []
    if isinstance(raw_panels, list) and raw_panels:
        for p in raw_panels:
            if not isinstance(p, dict):
                continue
            comps = p.get("components")
            panels.append({
                "channel_id": str(p.get("channel_id") or ""),
                "components": comps if isinstance(comps, list) else [],
            })
    if not panels:
        pc = cfg.get("panel_components")
        panels.append({
            "channel_id": str(cfg.get("panel_channel_id") or ""),
            "components": pc if isinstance(pc, list) else [],
        })
    return panels


def _parse_ticket_types(cfg):
    raw_types = cfg.get("ticket_types")
    if isinstance(raw_types, list) and raw_types:
        types = []
        for t in raw_types:
            if not isinstance(t, dict) or not t.get("id"):
                continue
            types.append({
                "id": str(t.get("id")),
                "name": str(t.get("name") or "Ticket"),
                "button_label": str(t.get("button_label") or "Open Ticket"),
                "button_style": str(t.get("button_style") or "primary"),
                "open_components": t.get("open_components") if isinstance(t.get("open_components"), list) else [],
            })
        return types
    oc = cfg.get("open_components")
    return [{
        "id": "support", "name": "Support",
        "button_label": str(cfg.get("open_button_label") or "Open Ticket"),
        "button_style": str(cfg.get("open_button_style") or "primary"),
        "open_components": oc if isinstance(oc, list) else [],
    }]


def _rebuild_ticket_registry():
    """Rebuild the interactive-component registry AND the union panel/type lists
    from every registered ticket source (main Tickets + Order Log)."""
    trees = []
    for feature, src in _ticket_sources.items():
        for p in src.get("panels", []):
            comps = p.get("components")
            if isinstance(comps, list):
                trees.append((feature, comps))
    _register_ticket_components(trees)
    ticket_config["panels"] = [p for src in _ticket_sources.values() for p in src.get("panels", [])]
    ticket_config["types"] = [t for src in _ticket_sources.values() for t in src.get("types", [])]


def _form_log_can_run(key, member):
    """Allowed if no roles set (open to all), member has an allowed role, or has
    Manage Server."""
    cfg = form_log_configs.get(key, {})
    # Quality Check separates "who can run /qualitycheck" (run_role_ids) from
    # "who can Accept/Deny" (allowed_role_ids). Other form logs use allowed_role_ids.
    role_ids = (cfg.get("run_role_ids") if key == "qualitycheck" else cfg.get("allowed_role_ids")) or []
    if not role_ids:
        return True
    try:
        if member.guild_permissions.manage_guild:
            return True
    except Exception:
        pass
    return has_any_role(member, role_ids)
credits_config = {"manager_role_ids": CREDIT_MANAGER_ROLE_IDS, "currency_name": "credits", "log_channel_id": ""}
_credits_memory = {}
# Roblox OAuth verification config (from the dashboard "Verification" block).
roblox_config = {
    "channel_id": "",
    "verified_role_ids": [],
    "remove_role_ids": [],
    "set_nickname": True,
    "log_channel_id": "",
    "client_id": "",
    "client_secret": "",
    "components": [],
    "button_label": "Verify",
    "button_style": "primary",
}

# Discord-role -> Roblox-group-rank sync. Owner maps role sets to rank numbers in
# the dashboard ("Roblox Group Sync" block); the actual rank change runs in the
# roblox-group-rank edge function (which holds ROBLOX_COOKIE), never here.
group_sync_config = {
    "enabled": False,
    "group_id": "",
    "tiers": [],  # [{"rank": int, "role_ids": {str, ...}}], sorted HIGHEST rank first
    "demote_rank": None,  # rank for members holding NO mapped role (None = leave them)
}


def _parse_group_sync_tiers(cfg):
    """Read flat tierN_roles / tierN_rank keys into tiers sorted highest-first."""
    tiers = []
    for i in range(1, 26):
        roles = cfg.get(f"tier{i}_roles")
        rank = cfg.get(f"tier{i}_rank")
        if not isinstance(roles, list) or not roles:
            continue
        try:
            rank = int(rank)
        except (TypeError, ValueError):
            continue
        tiers.append({"rank": rank, "role_ids": {str(r) for r in roles if r}})
    tiers.sort(key=lambda t: t["rank"], reverse=True)
    return tiers


def _desired_group_rank(member):
    """The rank number a member should hold: the highest tier whose role set the
    member has ANY of. With no mapped role, fall back to the configured demote
    rank (so they're deranked), or None to leave them alone if none is set."""
    have = {str(r.id) for r in member.roles}
    for tier in group_sync_config["tiers"]:
        if have & tier["role_ids"]:
            return tier["rank"]
    return group_sync_config["demote_rank"]


async def _group_sync_call(action, payload):
    """Call the roblox-group-rank edge function (which holds ROBLOX_COOKIE)."""
    try:
        session = await get_poll_session()
        async with session.post(
            f"{SUPABASE_FN_URL}/roblox-group-rank",
            headers=_fn_headers(),
            json={"action": action, "bot_id": BOT_ORDER_ID,
                  "group_id": group_sync_config["group_id"], **payload},
        ) as r:
            data = await r.json()
        return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"[GroupSync] {action} call failed: {e}")
        return {"error": str(e)[:150]}


_group_sync_pending = {}  # member_id -> asyncio.Task (debounce rapid role edits)


async def _group_sync_member_later(member):
    try:
        await asyncio.sleep(3)
        rank = _desired_group_rank(member)
        if rank is None:
            return
        res = await _group_sync_call("set", {"discord_user_id": str(member.id), "rank_number": rank})
        if res.get("changed"):
            print(f"[GroupSync] {member.id} -> rank {rank} (was {res.get('from')})")
        elif res.get("error"):
            print(f"[GroupSync] {member.id} set error: {res.get('error')}")
    except asyncio.CancelledError:
        pass
    except Exception as e:
        print(f"[GroupSync] member sync error: {e}")
    finally:
        _group_sync_pending.pop(member.id, None)


def _schedule_group_sync(member):
    if not (group_sync_config["enabled"] and group_sync_config["group_id"] and group_sync_config["tiers"]):
        return
    old = _group_sync_pending.get(member.id)
    if old and not old.done():
        old.cancel()
    _group_sync_pending[member.id] = asyncio.create_task(_group_sync_member_later(member))


async def _group_sync_scan(guild):
    """Rank every member of `guild` to match their Discord roles. Returns
    (result, error): result is a totals/breakdown dict, error a string on a
    hard failure. Shared by /grouproleupdate and the daily auto-check."""
    desired = []
    for member in guild.members:
        if member.bot:
            continue
        rank = _desired_group_rank(member)  # tier rank, demote rank, or None
        if rank is not None:
            desired.append({"discord_user_id": str(member.id), "rank_number": rank})
    totals = {"changed": 0, "unchanged": 0, "skipped": 0, "failed": 0}
    skip_reasons, no_perm, other_fails = {}, [], []
    for i in range(0, len(desired), 40):
        res = await _group_sync_call("sync", {"desired": desired[i:i + 40]})
        if res.get("error"):
            return None, res["error"]
        for k in totals:
            totals[k] += int(res.get(k) or 0)
        for d in (res.get("details") or []):
            err = d.get("error")
            if err:
                if "permission to manage" in str(err).lower():
                    no_perm.append(d.get("discordId"))
                elif len(other_fails) < 5:
                    other_fails.append((d.get("discordId"), str(err)[:100]))
            elif d.get("reason"):
                skip_reasons[d["reason"]] = skip_reasons.get(d["reason"], 0) + 1
    return {"checked": len(desired), "totals": totals, "skip_reasons": skip_reasons,
            "no_perm": no_perm, "other_fails": other_fails}, None


@tasks.loop(hours=24)
async def daily_group_sync():
    """Once a day, re-rank everyone to match their Discord roles (and derank
    anyone with no mapped role, if a demote rank is set)."""
    if not (group_sync_config["enabled"] and group_sync_config["group_id"] and group_sync_config["tiers"]):
        return
    for guild in bot.guilds:
        try:
            result, err = await _group_sync_scan(guild)
            if err:
                print(f"[GroupSync] daily scan {guild.id} failed: {err}")
            elif result:
                t = result["totals"]
                print(f"[GroupSync] daily {guild.id}: checked {result['checked']} "
                      f"updated {t['changed']} same {t['unchanged']} skipped {t['skipped']} failed {t['failed']}")
        except Exception as e:
            print(f"[GroupSync] daily scan error {guild.id}: {e}")


def success_embed(title, description=None):
    return discord.Embed(title=title, description=description, color=0x57F287)


def error_embed(title, description=None):
    return discord.Embed(title=title, description=description, color=0xED4245)


def info_embed(title, description=None):
    return discord.Embed(title=title, description=description, color=ACCENT)


def _fn_headers():
    return {
        "x-worker-token": WORKER_TOKEN,
        "Content-Type": "application/json",
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
    }


_poll_session = None
_auth_warned = False


# ── Shared pooled HTTPS client ─────────────────────────────────────────────
# One connection pool for the whole process. Every `async with _http() as
# client:` call site reuses warm keep-alive connections instead of paying a
# fresh TCP+TLS handshake (~100-300ms) per request — the AboutMe poll alone
# was doing that every 20 seconds. The wrapper's __aexit__ is a no-op so the
# 35 existing `async with` call sites keep their exact shape; the pool lives
# for the life of the process. Default timeout stays httpx's 5s, same as the
# per-call clients this replaces (most sites pass their own timeout anyway).
_shared_http = None


class _SharedHttp:
    async def __aenter__(self):
        global _shared_http
        if _shared_http is None or _shared_http.is_closed:
            _shared_http = httpx.AsyncClient(
                limits=httpx.Limits(max_connections=50, max_keepalive_connections=20))
        return _shared_http

    async def __aexit__(self, *exc):
        return False  # never close the shared pool


def _http():
    return _SharedHttp()


async def get_poll_session():
    global _poll_session
    if _poll_session is None or _poll_session.closed:
        _poll_session = aiohttp.ClientSession()
    return _poll_session


async def runtime_rpc(name, payload):
    try:
        async with _http() as client:
            r = await client.post(
                f"{SUPABASE_URL}/rest/v1/rpc/{name}",
                headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json"},
                json=payload,
                timeout=10,
            )
            if r.status_code == 200:
                return r.json()
            print(f"[RPC] {name} failed {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"[RPC] {name} error: {e}")
    return None


async def _bot_secret(key):
    """A credential the owner saved under API keys & credentials (e.g.
    ROBLOX_GROUP_ID), decrypted server-side for this bot only. '' if unset."""
    if not (WORKER_TOKEN and BOT_ORDER_ID):
        return ""
    res = await runtime_rpc("runtime_get_bot_secret",
                            {"_token": WORKER_TOKEN, "_bot_id": BOT_ORDER_ID, "_key": key})
    return res.strip() if isinstance(res, str) else ""


@bot.event
async def on_ready():
    print(f"{SERVER_NAME} bot online as {bot.user}")
    # Rejoin voice + resume playback IMMEDIATELY — before the config marathon —
    # so a redeploy's silence gap is as short as possible.
    global _music_restore_done
    if not _music_restore_done:
        _music_restore_done = True
        asyncio.create_task(_restore_music_state())
    print(f"[Boot] bot {BOT_ORDER_ID} using worker token prefix {WORKER_TOKEN[:12] if WORKER_TOKEN else 'MISSING'} (len {len(WORKER_TOKEN) if WORKER_TOKEN else 0})")
    # Dropdown-in-modal (Close Order form) needs discord.py 2.6+ (discord.ui.Label).
    print(f"[Boot] discord.py {discord.__version__} | dropdown-in-modal supported: {hasattr(discord.ui, 'Label')}")
    # Music is served by a shared Lavalink node (wavelink client). The bot itself
    # Music + TTS play natively (yt-dlp/edge-tts + FFmpeg) — no Lavalink.
    try:
        print(f"[Boot] music — native engine | yt-dlp {'ok' if _ytdlp else 'MISSING'} | "
              f"ffmpeg {_ffmpeg_exe()}")
    except Exception as _me:
        print(f"[Boot] music status check failed: {_me!r}")

    if BOT_ORDER_ID and WORKER_TOKEN:
        for loop in (send_heartbeat, poll_configs, poll_shutdown, record_metrics_loop, poll_roblox_apply, poll_about_me):
            try:
                if not loop.is_running():
                    loop.start()
            except Exception as e:
                print(f"[Startup] loop start failed: {e}")
        await fire_online_status()

    try:
        await apply_bot_identity()
    except Exception as e:
        print(f"[Startup] identity failed: {e}")
    try:
        await apply_about_me()
    except Exception as e:
        print(f"[Startup] about-me failed: {e}")
    if not sync_identity.is_running():
        sync_identity.start()

    try:
        await load_all_configs()
    except Exception as e:
        print(f"[Startup] config load failed: {e}")

    # On every redeploy: re-apply the global support roles to all tickets that
    # are already open, so newly-added staff can see existing tickets without
    # the owner touching each one. Runs in the background so boot isn't blocked.
    async def _boot_ticket_perm_sync():
        try:
            n = len(ticket_config.get("support_role_ids") or [])
            if n:
                print(f"[Boot] applying {n} support role(s) to open tickets…")
                await _resync_ticket_support_perms(ticket_config)
        except Exception as e:
            print(f"[Startup] ticket perm resync failed: {e}")
    asyncio.create_task(_boot_ticket_perm_sync())
    # Older pending ad-approval cards predate the Delay button — add it to them.
    asyncio.create_task(_ads_retrofit_delay_buttons())

    try:
        await seed_secret_slots()
    except Exception as e:
        print(f"[Startup] secret-slot seed failed: {e}")

    # These restores are independent of each other, so run them CONCURRENTLY —
    # a redeploy comes back online in roughly one round-trip instead of eight
    # serial ones (shorter music/presence gap every deploy). Each is isolated:
    # one failing never blocks the others.
    async def _safe_load(label, coro):
        try:
            await coro
        except Exception as e:
            print(f"{label} load failed: {e}")

    async def _invite_boot():
        await _load_invite_tracker()
        for g in bot.guilds:
            await _cache_guild_invites(g)

    await asyncio.gather(
        _safe_load("[Ads]", _load_ads()),
        _safe_load("[TTS] nick", _load_tts_nicks()),
        # Ephemeral-message content for OLDER posted messages whose keys the
        # fresh configs no longer produce (the design was edited since posting).
        _safe_load("[Tickets] eph registry", _load_eph_registry()),
        _safe_load("[Econ]", _load_econ()),
        _safe_load("[Startup] invite tracker", _invite_boot()),
        # Every saved giveaway (entrants + timers) so redeploys never drop them.
        _safe_load("[Startup] giveaway restore", _gw_restore_all()),
    )

    if not update_status.is_running():
        update_status.start()
    if not poll_group_sales.is_running():
        poll_group_sales.start()
    if not poll_stripe_sales.is_running():
        poll_stripe_sales.start()
    if not daily_group_sync.is_running():
        daily_group_sync.start()
    if not persist_music_state.is_running():
        persist_music_state.start()
    if not ads_drip.is_running():
        ads_drip.start()
    if not ads_invite_check.is_running():
        ads_invite_check.start()
    if not persist_ads_state.is_running():
        persist_ads_state.start()
    try:
        await _load_ticket_autoclose()
    except Exception as e:
        print(f"[Ticket] autoclose state load failed: {e}")
    try:
        await _bl_load_saved()
    except Exception as e:
        print(f"[Blacklist] saved-roles load failed: {e}")
    if not ticket_inactivity_tick.is_running():
        ticket_inactivity_tick.start()
    try:
        await _session_load()
    except Exception as e:
        print(f"[Session] load failed: {e}")
    try:
        await _shift_load()
    except Exception as e:
        print(f"[Shift] load failed: {e}")
    if not shift_tick.is_running():
        shift_tick.start()
    if not ticket_staff_reply_tick.is_running():
        ticket_staff_reply_tick.start()
    if not econ_autosave.is_running():
        econ_autosave.start()
    await refresh_status()

    try:
        if os.getenv("SKIP_SYNC") == "1":
            print("Command sync skipped")
        else:
            pruned = _prune_commands_for_base()
            if pruned:
                print(f"[Boot] base {BOT_BASE}: {pruned} command(s) not in this product, removed before sync")
            # Discord's bulk command overwrite is slow and rate limited (it has
            # taken 45s on a busy day). Only sync when the command set actually
            # changed since the last successful sync.
            fp = _tree_fingerprint()
            _fp_ok, prev = await _durable_config_get("command-sync", attempts=2)
            synced = []
            if _fp_ok and isinstance(prev, dict) and prev.get("fingerprint") == fp:
                print(f"[Boot] {len(bot.tree.get_commands())} commands unchanged since the last sync, skipping")
            else:
                synced = await bot.tree.sync()
                print(f"Synced {len(synced)} commands")
                await _bot_config_upsert("command-sync", {"fingerprint": fp, "count": len(synced),
                                                          "at": int(time.time())})
            for cmd in synced:
                if cmd.name == "package":
                    for opt in getattr(cmd, "options", []):
                        if getattr(opt, "name", "") == "channel":
                            types = [int(t.value) for t in (getattr(opt, "channel_types", None) or [])]
                            print(f"[package] channel option accepts channel_types={types} "
                                  f"(15=forum, 16=media expected)")
    except Exception as e:
        print(f"Sync error: {e}")


def _tree_fingerprint():
    """A stable hash of every slash command (names, descriptions, options), so
    a boot can tell whether Discord already has this exact command set."""
    try:
        payload = [c.to_dict() for c in bot.tree.get_commands()]
        raw = json.dumps(payload, sort_keys=True, default=str)
    except Exception as e:
        return f"error:{e}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def _order_policy():
    """Fetch the order's active flag + licensed server limit for bot-side
    guards. Returns (active, server_limit). Fails SAFE (active=True, limit=None)
    so an API hiccup never makes the bot abandon a legit server."""
    data = await runtime_rpc("runtime_bot_server_policy", {"_token": WORKER_TOKEN, "_bot_id": BOT_ORDER_ID})
    if isinstance(data, dict):
        active = data.get("active", True)
        lim = data.get("limit")
        lim = int(lim) if isinstance(lim, (int, float)) or (isinstance(lim, str) and str(lim).isdigit()) else None
        return (active is not False), lim
    return True, None


@bot.event
async def on_guild_join(guild):
    """The bot was added to a server. It may only STAY if the owner is within
    their licensed server count (add more via 'Add to another server' in the
    dashboard). Otherwise it posts a short note and leaves. Fails SAFE: it only
    leaves on an explicit over-limit / inactive answer, never on an API error."""
    print(f"[Guild] joined {guild.id} ({guild.name}) — {guild.member_count} members")
    if not (BOT_ORDER_ID and WORKER_TOKEN):
        return

    # Register the join so it shows in the dashboard's server list (best effort).
    try:
        session = await get_poll_session()
        await session.post(
            f"{SUPABASE_FN_URL}/{BOT_API}/guild-join",
            headers=_fn_headers(),
            json={
                "bot_id": BOT_ORDER_ID,
                "guild_id": str(guild.id),
                "guild_name": guild.name,
                "member_count": guild.member_count or 0,
            },
        )
    except Exception:
        pass

    active, limit = await _order_policy()
    over_limit = isinstance(limit, int) and limit >= 0 and len(bot.guilds) > limit
    if active and not over_limit:
        try:
            await cache_roles(guild.id)
            await cache_channels(guild.id)
        except Exception:
            pass
        return

    # Not licensed for this server — explain, then leave.
    reason = "the owner's plan is inactive" if not active else "this exceeds the owner's licensed server count"
    print(f"[Guild] {guild.id} not allowed ({reason}) — leaving (limit={limit}, in={len(bot.guilds)})")
    try:
        target = guild.system_channel
        if target is None or not target.permissions_for(guild.me).send_messages:
            target = next(
                (c for c in guild.text_channels if c.permissions_for(guild.me).send_messages),
                None,
            )
        if target is not None:
            await target.send(embed=error_embed(
                "This bot is licensed per server",
                f"{BRAND} only runs in servers the owner has added through "
                "their **Oversite dashboard** (Add to another server). This server "
                "isn't covered by their plan, so I'm leaving. Ask the owner to add "
                "it from the dashboard, then re-invite.",
            ))
    except Exception:
        pass
    try:
        await guild.leave()
    except Exception as e:
        print(f"[Guild] leave failed: {e}")


@bot.event
async def on_guild_remove(guild):
    """Bot left / was kicked — free the server slot on the backend."""
    print(f"[Guild] removed from {guild.id} ({guild.name})")
    if not (BOT_ORDER_ID and WORKER_TOKEN):
        return
    try:
        session = await get_poll_session()
        await session.post(
            f"{SUPABASE_FN_URL}/{BOT_API}/guild-leave",
            headers=_fn_headers(),
            json={"bot_id": BOT_ORDER_ID, "guild_id": str(guild.id)},
        )
    except Exception as e:
        print(f"[Guild] guild-leave report failed: {e}")


# ======================= Invite Tracker =======================
# Attribute each join to the invite (and inviter) that was used, then keep a
# leaderboard: score = regular + bonus - left - fake, where
#   regular = people they invited who are still here (and not fake),
#   left    = people they invited who left,
#   fake    = joins from very new accounts (likely alts),
#   bonus   = manual adjustments by staff.
# Surfaced by /leaderboard invites and the {invite list} message token.
_invite_uses_cache = {}     # guild_id -> {invite_code: uses}
_invite_inviter_uses = {}   # guild_id -> {inviter_id(str): total live uses across their links}
invite_tracker = {}         # guild_id(str) -> {"invited_by":{}, "left":{}, "fake":{}, "bonus":{}}
# Owner settings from the dashboard "Invite Tracker" block (separate bot_config
# feature from the tracker data below, so they never clobber each other).
invite_tracker_config = {"enabled": True, "board_components": []}
_invite_save_pending = None


def _is_risky_join(member):
    """Risk signals evaluated at the MOMENT of join to flag likely alt/fake accounts.
    Not a 'days since joined the server' count — this looks at the account itself:
    a brand-new account (created recently) is the primary alt signal, and a
    still-default avatar on a young account is a secondary one."""
    try:
        age = discord.utils.utcnow() - member.created_at
    except Exception:
        return False
    if age.days < 30:
        return True
    if getattr(member, "avatar", None) is None and age.days < 90:
        return True
    return False


def _inv_data(guild_id):
    d = invite_tracker.setdefault(str(guild_id), {"invited_by": {}, "left": {}, "fake": {}, "bonus": {}})
    d.setdefault("baseline", {})  # inviter_id -> live uses at last /resetinvites
    return d


async def _cache_guild_invites(guild):
    """Snapshot every invite's use count so the next join can be attributed."""
    try:
        invites = await guild.invites()
        cache = {i.code: (i.uses or 0) for i in invites}
        # Per-inviter LIVE totals straight from Discord (what any invite tracker
        # reads). Includes links with 0 uses, so their owners still show up.
        inviter_uses = {}
        for i in invites:
            if i.inviter:
                iid = str(i.inviter.id)
                inviter_uses[iid] = inviter_uses.get(iid, 0) + (i.uses or 0)
        try:
            if "VANITY_URL" in getattr(guild, "features", []):
                v = await guild.vanity_invite()
                if v and v.code:
                    cache[v.code] = v.uses or 0
        except Exception:
            pass
        _invite_uses_cache[guild.id] = cache
        _invite_inviter_uses[guild.id] = inviter_uses
    except discord.Forbidden:
        print(f"[Invites] no Manage Server permission in guild {guild.id} — tracking off")
    except Exception as e:
        print(f"[Invites] cache failed for {guild.id}: {e}")


async def _attribute_join(member):
    """Find which invite the member used (its use count went up) and record it."""
    if not invite_tracker_config.get("enabled", True):
        return
    guild = member.guild
    before = _invite_uses_cache.get(guild.id, {})
    inviter_id = None
    try:
        invites = await guild.invites()
    except Exception:
        invites = []
    after = {}
    for i in invites:
        after[i.code] = i.uses or 0
        if (i.uses or 0) > before.get(i.code, 0) and inviter_id is None:
            inviter_id = str(i.inviter.id) if i.inviter else None
    # Vanity URL (no inviter attribution possible).
    try:
        if "VANITY_URL" in getattr(guild, "features", []):
            v = await guild.vanity_invite()
            if v and v.code:
                after[v.code] = v.uses or 0
    except Exception:
        pass
    _invite_uses_cache[guild.id] = after
    if not inviter_id:
        return  # couldn't determine (vanity, or bot lacks permission)
    data = _inv_data(guild.id)
    mid = str(member.id)
    # Wipe any stale record of this member from a previous stint.
    data["invited_by"].pop(mid, None)
    data["left"].pop(mid, None)
    data["fake"].pop(mid, None)
    risky = _is_risky_join(member)
    if risky:
        data["fake"][mid] = inviter_id
    else:
        data["invited_by"][mid] = inviter_id
    _save_invite_tracker_soon()


def _mark_left(member):
    data = invite_tracker.get(str(member.guild.id))
    if not data:
        return
    mid = str(member.id)
    if mid in data["invited_by"]:
        data["left"][mid] = data["invited_by"].pop(mid)
        _save_invite_tracker_soon()
    elif mid in data["fake"]:
        data["fake"].pop(mid, None)
        _save_invite_tracker_soon()


def _invite_scoreboard(guild):
    """[(inviter_id, score, regular, left, fake, bonus)] sorted by score desc.
    'regular' comes from Discord's own invite-use totals when available (so the
    board matches what an invite tracker already shows), falling back to the
    joins we've recorded ourselves. Everyone who owns an invite link is listed,
    even with 0 uses."""
    data = invite_tracker.get(str(guild.id)) or {"invited_by": {}, "left": {}, "fake": {}, "bonus": {}}
    live = _invite_inviter_uses.get(guild.id, {})
    baseline = data.get("baseline") or {}
    ids = (set(live) | set(data["invited_by"].values()) | set(data["left"].values())
           | set(data["fake"].values()) | set(data["bonus"].keys()))
    ids.discard(None)
    rows = []
    for iid in ids:
        # Only rank people who are still in the server (skip left/deleted
        # accounts that would render as "unknown-user").
        try:
            if guild.get_member(int(iid)) is None:
                continue
        except (TypeError, ValueError):
            continue
        tracked_regular = sum(1 for v in data["invited_by"].values() if v == iid)
        if iid in live:
            regular = max(0, int(live[iid]) - int(baseline.get(iid, 0)))
        else:
            regular = tracked_regular
        left = sum(1 for v in data["left"].values() if v == iid)
        fake = sum(1 for v in data["fake"].values() if v == iid)
        bonus = int(data["bonus"].get(iid, 0))
        score = regular + bonus - left - fake
        rows.append((iid, score, regular, left, fake, bonus))
    rows.sort(key=lambda r: (-r[1], -r[2]))
    return rows


def _render_invite_list(guild, page=0, per_page=10):
    """The numbered leaderboard text (also what the {invite list} token becomes)."""
    rows = _invite_scoreboard(guild)
    if not rows:
        return "No invites tracked yet.", 1
    pages = max(1, (len(rows) + per_page - 1) // per_page)
    page = max(0, min(page, pages - 1))
    lines = []
    for idx, (iid, score, regular, left, fake, bonus) in enumerate(rows[page * per_page:(page + 1) * per_page], start=page * per_page + 1):
        s = "invite" if score == 1 else "invites"
        lines.append(f"`{idx}.` <@{iid}> · **{score}** {s}. ({regular} regular, {left} left, {fake} fake, {bonus} bonus)")
    return "\n".join(lines), pages


def _save_invite_tracker_soon():
    global _invite_save_pending
    if _invite_save_pending and not _invite_save_pending.done():
        return
    _invite_save_pending = asyncio.create_task(_save_invite_tracker())


async def _save_invite_tracker():
    await asyncio.sleep(5)  # debounce a burst of joins/leaves
    await _bot_config_upsert("invite-tracker-data", {"guilds": invite_tracker})


async def _load_invite_tracker():
    cfg = await _bot_config_get("invite-tracker-data")
    guilds = (cfg or {}).get("guilds")
    if isinstance(guilds, dict):
        for gid, d in guilds.items():
            if isinstance(d, dict):
                invite_tracker[gid] = {
                    "invited_by": dict(d.get("invited_by") or {}),
                    "left": dict(d.get("left") or {}),
                    "fake": dict(d.get("fake") or {}),
                    "bonus": {k: int(v) for k, v in (d.get("bonus") or {}).items()},
                }
    print(f"[Invites] tracker loaded for {len(invite_tracker)} guild(s)")


@bot.event
async def on_invite_create(invite):
    try:
        _invite_uses_cache.setdefault(invite.guild.id, {})[invite.code] = invite.uses or 0
    except Exception:
        pass


@bot.event
async def on_invite_delete(invite):
    try:
        _invite_uses_cache.get(invite.guild.id, {}).pop(invite.code, None)
    except Exception:
        pass


class InviteLeaderboardView(discord.ui.View):
    def __init__(self, guild, page=0):
        super().__init__(timeout=180)
        self.guild = guild
        self.page = page

    def build_embed(self):
        text, pages = _render_invite_list(self.guild, self.page)
        embed = discord.Embed(title="Invites Leaderboard", description=text, color=ACCENT)
        embed.set_footer(text=f"Invite Tracker · Page {min(self.page + 1, pages)}/{pages}")
        return embed

    async def _refresh(self, interaction):
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(emoji="⏮️", style=discord.ButtonStyle.secondary)
    async def first(self, interaction, button):
        self.page = 0
        await self._refresh(interaction)

    @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction, button):
        self.page = max(0, self.page - 1)
        await self._refresh(interaction)

    @discord.ui.button(emoji="⏹️", style=discord.ButtonStyle.secondary)
    async def stop_btn(self, interaction, button):
        for c in self.children:
            c.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()

    @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.secondary)
    async def next(self, interaction, button):
        _, pages = _render_invite_list(self.guild, self.page)
        self.page = min(pages - 1, self.page + 1)
        await self._refresh(interaction)


leaderboard_group = app_commands.Group(name="leaderboard", description="Server leaderboards.")


@leaderboard_group.command(name="invites", description="Shows the invite leaderboard for this server.")
async def leaderboard_invites(interaction: discord.Interaction):
    # Refresh from Discord's live invite data first so the board matches reality
    # even before we've tracked any joins ourselves.
    try:
        await _cache_guild_invites(interaction.guild)
    except Exception:
        pass
    # If a message design is set up in the dashboard, post THAT (its {invite list}
    # token expands to the leaderboard). Otherwise fall back to the built-in
    # paged embed.
    comps = invite_tracker_config.get("board_components") or []
    if comps:
        await interaction.response.defer()
        # Mentions render (@name, clickable) but never notify — like a silent ping.
        ok = await send_v2_message(interaction.channel, comps, interaction=interaction,
                                   allowed_mentions={"parse": []})
        if not ok:
            await interaction.followup.send(
                embed=info_embed("Note", "Couldn't render the leaderboard message."), ephemeral=True)
        return
    view = InviteLeaderboardView(interaction.guild, 0)
    await interaction.response.send_message(embed=view.build_embed(), view=view)


@bot.tree.command(name="invitebonus", description="Adds or removes bonus invites for a member.")
@app_commands.describe(user="Member to adjust", amount="Bonus invites to add. Use a negative number to take some away.")
async def invitebonus_cmd(interaction: discord.Interaction, user: discord.Member, amount: int):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(embed=error_embed("Admins only", "You need Manage Server."), ephemeral=True)
        return
    data = _inv_data(interaction.guild.id)
    cur = int(data["bonus"].get(str(user.id), 0)) + amount
    if cur:
        data["bonus"][str(user.id)] = cur
    else:
        data["bonus"].pop(str(user.id), None)
    _save_invite_tracker_soon()
    await interaction.response.send_message(
        embed=success_embed("Bonus invites updated", f"{user.mention} now has **{cur}** bonus invite(s)."),
        ephemeral=True)


async def _reset_invites(guild, user=None):
    """Zero the invite leaderboard. Because 'regular' is read from Discord's own
    invite-use totals (which we can't clear), we snapshot the current uses as a
    baseline and subtract it going forward — so everyone starts at 0 and new
    invites from now on count. With a user, reset only that member."""
    await _cache_guild_invites(guild)  # refresh live uses first
    live = _invite_inviter_uses.get(guild.id, {})
    data = _inv_data(guild.id)
    if user is None:
        data["invited_by"] = {}
        data["left"] = {}
        data["fake"] = {}
        data["bonus"] = {}
        data["baseline"] = dict(live)
    else:
        uid = str(user.id)
        data["invited_by"] = {k: v for k, v in data["invited_by"].items() if v != uid}
        data["left"] = {k: v for k, v in data["left"].items() if v != uid}
        data["fake"] = {k: v for k, v in data["fake"].items() if v != uid}
        data["bonus"].pop(uid, None)
        data["baseline"][uid] = int(live.get(uid, 0))
    _save_invite_tracker_soon()


@bot.tree.command(name="resetinvites", description="Resets everyone's invite count to zero.")
@app_commands.describe(user="Only reset this member. Leave empty to reset everyone.")
async def resetinvites_cmd(interaction: discord.Interaction, user: discord.Member = None):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(
            embed=error_embed("Admins only", "You need Manage Server."), ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    await _reset_invites(interaction.guild, user)
    if user is None:
        msg = "Everyone's invite counts are back to **0**. New invites from now on will count."
    else:
        msg = f"{user.mention}'s invite count is back to **0**. Their new invites from now on will count."
    await interaction.followup.send(embed=success_embed("Invites reset", msg), ephemeral=True)


bot.tree.add_command(leaderboard_group)


@bot.event
async def on_member_join(member):
    await refresh_status()
    try:
        await _attribute_join(member)
    except Exception as e:
        print(f"[Invites] attribute join failed: {e}")
    components = invite_config.get("components") or []
    embeds_data = invite_config.get("embeds") or []
    if components or embeds_data:
        ch_id = invite_config.get("channel_id") or welcome_config.get("channel_id") or ""
        if ch_id:
            channel = member.guild.get_channel(int(ch_id))
            if channel:
                if components:
                    rendered = _render_invite_components(components, member)
                    await send_v2_message(channel, rendered)
                else:
                    rendered = _render_invite_components(embeds_data, member)
                    embeds = [build_embed(e) for e in rendered][:10]
                    try:
                        if embeds:
                            await channel.send(embeds=embeds)
                        for m in (invite_config.get("messages") or []):
                            await channel.send(_sub_placeholders(m, member))
                    except Exception as e:
                        print(f"[Invite] send failed: {e}")
        return
    if not welcome_config.get("enabled", True):
        return
    ch_id = welcome_config.get("channel_id") or ""
    if not ch_id:
        return
    channel = member.guild.get_channel(int(ch_id))
    if not channel:
        return
    await send_welcome(channel, member)


@bot.event
async def on_member_remove(member):
    await refresh_status()
    try:
        _mark_left(member)
    except Exception as e:
        print(f"[Invites] mark-left failed: {e}")


_EMOJI_SHORTCODE_RE = re.compile(r":([a-zA-Z][a-zA-Z0-9_]*)(?:~\d+)?:")
# A complete custom emoji already written out: <:name:id> or <a:name:id>.
_FULL_EMOJI_RE = re.compile(r"<a?:[a-zA-Z0-9_]+:\d+>")


def _resolve_emoji_shortcodes(text, guild):
    if ":" not in text:
        return text
    lookup = {e.name.lower(): e for e in (guild.emojis if guild else [])}
    # Bots can use custom emojis from ANY server they're in, so fall back to the
    # bot's full emoji set (e.g. a shared emoji server) for anything not found in
    # this guild. This is why an :emoji: from another server still renders.
    try:
        for e in bot.emojis:
            lookup.setdefault(e.name.lower(), e)
    except Exception:
        pass
    if not lookup:
        return text

    # Stash any already-complete custom emojis so we don't rewrite the `:name:`
    # inside <:name:id> — doing so leaves the raw emoji id dangling next to the
    # rendered emoji (e.g. "🔥 1527943242115579905>").
    saved = []

    def _stash(m):
        saved.append(m.group(0))
        return f"\x00{len(saved) - 1}\x00"

    protected = _FULL_EMOJI_RE.sub(_stash, text)

    def repl(match):
        emoji = lookup.get(match.group(1).lower())
        if emoji is None:
            return match.group(0)
        return f"<{'a' if emoji.animated else ''}:{emoji.name}:{emoji.id}>"

    resolved = _EMOJI_SHORTCODE_RE.sub(repl, protected)
    return re.sub(r"\x00(\d+)\x00", lambda m: saved[int(m.group(1))], resolved)


def _resolve_role_mentions(text, guild):
    """Turn a plain '@Role Name' typed in the dashboard into a real <@&id> role
    mention so it actually pings. Matches against the guild's real role names,
    longest first (so 'Livery Designer' wins over 'Livery')."""
    if not text or "@" not in text or not guild:
        return text
    for role in sorted(guild.roles, key=lambda r: len(r.name), reverse=True):
        if role.is_default() or not role.name:
            continue
        text = re.sub(r"@" + re.escape(role.name), f"<@&{role.id}>", text, flags=re.IGNORECASE)
    return text


_CHANNEL_TOKEN_RE = re.compile(r"#([a-zA-Z0-9_\-]+)")


def _resolve_channel_mentions(text, guild):
    """Turn a plain '#channel-name' typed in the dashboard into a real <#id>
    channel link. Only replaces names that match an actual channel, so markdown
    headings ('## Title', '-# subtext') are left alone."""
    if not text or "#" not in text or not guild:
        return text
    by_name = {}
    for ch in getattr(guild, "channels", []) or []:
        nm = (getattr(ch, "name", "") or "").lower()
        if nm:
            by_name.setdefault(nm, ch.id)
    if not by_name:
        return text

    def repl(m):
        cid = by_name.get(m.group(1).lower())
        return f"<#{cid}>" if cid else m.group(0)

    return _CHANNEL_TOKEN_RE.sub(repl, text)


# A masked link whose target is a channel: [label](<#channel-name>), [label](#name)
# or [label](<#123>). Discord masked links (in embeds / V2 text) need a real URL,
# not a <#id> mention — so we swap the target for the channel's jump URL. This
# must run BEFORE _resolve_channel_mentions, which would otherwise turn the inner
# #name into <#id> and break the link.
_CHANNEL_LINK_RE = re.compile(r"\]\(\s*<?#([a-zA-Z0-9_\-]+)>?\s*\)")


def _resolve_channel_links(text, guild):
    if not text or "](" not in text or not guild:
        return text
    by_name = {}
    for ch in getattr(guild, "channels", []) or []:
        nm = (getattr(ch, "name", "") or "").lower()
        if nm:
            by_name.setdefault(nm, ch.id)

    def repl(m):
        key = m.group(1)
        cid = by_name.get(key.lower()) or (key if key.isdigit() else None)
        if not cid:
            return m.group(0)
        return f"](https://discord.com/channels/{guild.id}/{cid})"

    return _CHANNEL_LINK_RE.sub(repl, text)


def _sub_placeholders(text, member):
    if not isinstance(text, str):
        return text
    g = member.guild
    count = str(g.member_count or 0)
    bot_count = str(sum(1 for m in g.members if m.bot))
    human_count = str(sum(1 for m in g.members if not m.bot)) if g.members else count
    boosts = str(g.premium_subscription_count or 0)
    boost_level = str(getattr(g, "premium_tier", 0) or 0)
    channel_count = str(len(g.channels))
    role_count = str(len(g.roles))
    repl = {
        "{user}": member.mention,
        "{username}": member.display_name,
        "{server}": g.name, "{server_name}": g.name, "{server name}": g.name,
        "{member_count}": count, "{members}": count, "{count}": count,
        "{player count}": count, "{player_count}": count,
        "{human_count}": human_count, "{humans}": human_count,
        "{bot_count}": bot_count, "{bot count}": bot_count, "{bots}": bot_count,
        "{boosts}": boosts, "{boost_count}": boosts,
        "{total server boosts}": boosts, "{server_boosts}": boosts,
        "{boost_level}": boost_level, "{boost_tier}": boost_level,
        "{channel_count}": channel_count, "{channels}": channel_count,
        "{role_count}": role_count, "{roles}": role_count,
        "{emoji}": f"<:e:{WELCOME_EMOJI_ID}>",
    }
    for token, value in repl.items():
        text = text.replace(token, value)
    text = _sub_invite_list(text, member.guild)
    text = _sub_queue_list(text, member.guild)
    text = _sub_server_name(text, member.guild)
    return _resolve_emoji_shortcodes(_resolve_channel_mentions(_resolve_channel_links(_resolve_role_mentions(text, member.guild), member.guild), member.guild), member.guild)


def _sub_invite_list(text, guild):
    """Replace the {invite list} / {invite_list} token with the invite leaderboard."""
    if not text or guild is None or "{invite" not in text:
        return text
    lst, _ = _render_invite_list(guild)
    return text.replace("{invite list}", lst).replace("{invite_list}", lst)


_QUEUE_LIST_RE = re.compile(r"\{queue[ _]list\}", re.IGNORECASE)
_SERVER_NAME_RE = re.compile(r"\{server[ _]?name\}", re.IGNORECASE)


def _sub_server_name(text, guild):
    """Replace {server name} / {Server Name} / {server_name} (any case) with the
    server's name. ({server} still works via the plain token maps.)"""
    if not text or guild is None or "{server" not in text.lower():
        return text
    return _SERVER_NAME_RE.sub(lambda _m: getattr(guild, "name", "") or "", text)


def _ads_queue_entries(guild):
    """The queue as [(ad, lane, post_unix_ts)] in true posting order. Simulates
    the drip slot by slot: each slot goes to the first ad (bypass lane first)
    whose scheduled date has arrived by then — an ad delayed via the Delay
    button (ad["not_before"]) waits for its day while the ads behind it fill
    the spots before it, and its shown date never lands before the day staff
    picked."""
    gd = ads_data.get(str(guild.id)) or {}
    items = [(a, "bypass") for a in (gd.get("bypass") or [])] + [(a, "normal") for a in (gd.get("queue") or [])]
    interval = max(1, int(ads_config.get("interval_minutes") or 60)) * 60
    last = int(gd.get("last_drip", 0))
    now = int(time.time())
    slot = max(now + 30, (last + interval) if last else now + 30)
    remaining = list(items)
    out = []
    while remaining:
        pick = next((it for it in remaining if int(it[0].get("not_before") or 0) <= slot), None)
        if pick is None:
            # Everything left is scheduled beyond this slot — jump to the
            # soonest scheduled date and continue the drip from there.
            pick = min(remaining, key=lambda it: int(it[0].get("not_before") or 0))
            slot = max(slot, int(pick[0].get("not_before") or 0))
        remaining.remove(pick)
        out.append((pick[0], pick[1], slot))
        slot += interval
    return out


def _ads_queue_line(a, lane, ts, n):
    star = "Bypass · " if lane == "bypass" else ""
    if a.get("type") == "giveaway":
        title = f"Giveaway: {a.get('prize') or 'Giveaway'}"
    else:
        name = a.get("server_name") or "Server"
        link = a.get("server_link") or ""
        title = f"[{name}]({link})" if link else name
    sched = " · scheduled" if int(a.get("not_before") or 0) > int(time.time()) else ""
    return f"{star}**{n}.** {title}\nUser: <@{a.get('user_id')}>\nDate: <t:{ts}:f>{sched}"


def _ads_queue_text(guild):
    """A text summary of the ad queue — used by the {queue list} token. Shows the
    first 10; the View Queue button paginates the rest."""
    entries = _ads_queue_entries(guild)
    if not entries:
        return "The ad queue is empty — your ad would post next."
    lines = [_ads_queue_line(a, lane, ts, i + 1) for i, (a, lane, ts) in enumerate(entries[:10])]
    more = f"\n\n…and {len(entries) - 10} more." if len(entries) > 10 else ""
    return "\n\n".join(lines) + more


def _sub_queue_list(text, guild):
    """Replace {queue list} / {Queue List} (any case) with the ad-queue summary."""
    if not text or guild is None or not _QUEUE_LIST_RE.search(text):
        return text
    summary = _ads_queue_text(guild)
    return _QUEUE_LIST_RE.sub(lambda _m: summary, text)


def _render_guild_text(text, guild):
    """Resolve :emoji: shortcodes and {count}-style placeholders for text posted
    to a channel (no specific member). Used everywhere a panel/embed renders
    text so custom emojis and variables work every time."""
    if not isinstance(text, str) or not text:
        return text
    if guild is not None and "{" in text:
        count = str(getattr(guild, "member_count", 0) or 0)
        members = list(getattr(guild, "members", []) or [])
        bot_count = str(sum(1 for m in members if m.bot)) if members else "0"
        human_count = str(sum(1 for m in members if not m.bot)) if members else count
        boosts = str(getattr(guild, "premium_subscription_count", 0) or 0)
        boost_level = str(getattr(guild, "premium_tier", 0) or 0)
        repl = {
            "{server}": guild.name, "{server_name}": guild.name, "{server name}": guild.name,
            "{member_count}": count, "{members}": count, "{count}": count,
            "{player count}": count, "{player_count}": count,
            "{human_count}": human_count, "{humans}": human_count,
            "{bot_count}": bot_count, "{bot count}": bot_count, "{bots}": bot_count,
            "{boosts}": boosts, "{boost_count}": boosts,
            "{total server boosts}": boosts, "{server_boosts}": boosts,
            "{boost_level}": boost_level, "{boost_tier}": boost_level,
            "{channel_count}": str(len(guild.channels)), "{channels}": str(len(guild.channels)),
            "{role_count}": str(len(guild.roles)), "{roles}": str(len(guild.roles)),
        }
        for token, value in repl.items():
            text = text.replace(token, value)
        # Custom per-service status tokens ({liveries}, {liveriesstatus}, …).
    text = _sub_invite_list(text, guild)
    text = _sub_queue_list(text, guild)
    text = _sub_server_name(text, guild)
    return _resolve_emoji_shortcodes(_resolve_channel_mentions(_resolve_channel_links(_resolve_role_mentions(text, guild), guild), guild), guild)


_INVITE_TEXT_KEYS = {"text", "content", "label", "placeholder", "title", "description", "name", "value"}


def _render_invite_components(components, member):
    def walk(node):
        if isinstance(node, dict):
            return {
                k: _sub_placeholders(v, member) if k in _INVITE_TEXT_KEYS and isinstance(v, str) else walk(v)
                for k, v in node.items()
            }
        if isinstance(node, list):
            return [walk(x) for x in node]
        return node

    return walk(components)


async def send_welcome(channel, member):
    emoji = f"<:e:{WELCOME_EMOJI_ID}>"
    body = welcome_config.get("message") or f"{emoji} Welcome {member.mention} to **{SERVER_NAME}**, glad to have you."
    body = body.replace("{user}", member.mention).replace("{server}", SERVER_NAME).replace("{emoji}", emoji)
    view = discord.ui.View()
    count_btn = discord.ui.Button(
        label=str(member.guild.member_count),
        style=discord.ButtonStyle.secondary,
        emoji=discord.PartialEmoji(name="members", id=MEMBER_COUNT_EMOJI_ID),
        disabled=True,
    )
    dash_btn = discord.ui.Button(
        label="Dashboard",
        style=discord.ButtonStyle.link,
        url=f"https://discord.com/channels/{member.guild.id}/{WELCOME_DASHBOARD_CHANNEL_ID}",
    )
    view.add_item(count_btn)
    view.add_item(dash_btn)
    try:
        await channel.send(content=body, view=view)
    except Exception as e:
        print(f"[Welcome] send failed: {e}")


async def refresh_status():
    total = sum((g.member_count or 0) for g in bot.guilds)
    try:
        await bot.change_presence(
            status=discord.Status.online,
            activity=discord.Activity(type=discord.ActivityType.watching, name=f"Watching over {total} roleplayers"),
        )
    except Exception as e:
        print(f"[Status] update failed: {e}")


@tasks.loop(minutes=10)
async def update_status():
    await refresh_status()


@tasks.loop(seconds=20)
async def poll_about_me():
    # Apply dashboard About Me edits within ~20s (only PATCHes when it changed,
    # so this is cheap and never hits Discord unless the text is new).
    await apply_about_me()


@poll_about_me.before_loop
async def before_poll_about_me():
    await bot.wait_until_ready()


@update_status.before_loop
async def before_update_status():
    await bot.wait_until_ready()


def has_any_role(member, role_ids):
    if not isinstance(member, discord.Member):
        return False
    if member.guild_permissions.administrator:
        return True
    ids = {str(r.id) for r in member.roles}
    return bool(ids & set(str(x) for x in role_ids))


async def credits_change(guild_id, user_id, amount, reason, granted_by):
    result = await runtime_rpc("runtime_credits_op", {
        "_token": WORKER_TOKEN, "_bot_id": BOT_ORDER_ID, "_op": "add",
        "_payload": {"guild_id": str(guild_id), "user_id": str(user_id), "amount": amount, "reason": reason, "granted_by": str(granted_by)},
    })
    if isinstance(result, dict) and "total" in result:
        return result["total"]
    key = (str(guild_id), str(user_id))
    mem = _credits_memory.setdefault(key, {"total": 0, "entries": []})
    mem["total"] += amount
    mem["entries"].insert(0, {"amount": amount, "reason": reason, "granted_by": str(granted_by)})
    return mem["total"]


async def credits_lookup(guild_id, user_id):
    result = await runtime_rpc("runtime_credits_op", {
        "_token": WORKER_TOKEN, "_bot_id": BOT_ORDER_ID, "_op": "balance",
        "_payload": {"guild_id": str(guild_id), "user_id": str(user_id)},
    })
    if isinstance(result, dict) and "total" in result:
        return result.get("total", 0), result.get("entries", []) or []
    mem = _credits_memory.get((str(guild_id), str(user_id)), {"total": 0, "entries": []})
    return mem["total"], mem["entries"]


async def log_credit_action(guild, text):
    ch_id = credits_config.get("log_channel_id") or ""
    if not ch_id or not guild:
        return
    channel = guild.get_channel(int(ch_id))
    if channel:
        try:
            await channel.send(embed=info_embed("Credit log", text))
        except Exception:
            pass


async def log_purchase(guild, *, discord_id=None, roblox_username=None, roblox_id=None,
                       payment_type="", amount="", payment_id="", when=None, customer_name=None):
    """Post a purchase to the Logging block's purchase-logs channel. If a message
    was designed in the dashboard, its tokens are filled in and it's posted;
    otherwise a default layout is used."""
    ch = await resolve_channel(logging_config.get("purchase_log_channel_id"))
    if not ch:
        return
    try:
        ts = int(when) if when else int(discord.utils.utcnow().timestamp())
    except Exception:
        ts = int(discord.utils.utcnow().timestamp())
    # Customer must never be blank. Prefer the linked Discord user; then an
    # explicit name/email (Stripe payer); then their Roblox account (name +
    # profile link) so every box is filled out.
    roblox_profile = f"https://www.roblox.com/users/{roblox_id}/profile" if roblox_id else ""
    if discord_id:
        cust = f"<@{discord_id}> ({discord_id})"
        cust_mention = f"<@{discord_id}>"
        cust_id = str(discord_id)
    elif customer_name:
        cust = cust_mention = str(customer_name)
        cust_id = ""
    elif roblox_username or roblox_id:
        label = roblox_username or f"Roblox {roblox_id}"
        cust = f"[{label}]({roblox_profile})" if roblox_profile else label
        cust_mention = cust
        cust_id = str(roblox_id or "")
    else:
        cust = cust_mention = "Unknown"
        cust_id = ""
    subs = {
        "{customer}": cust,
        "{customer_mention}": cust_mention,
        "{customer_id}": cust_id,
        "{roblox}": str(roblox_username or ""),
        "{roblox_account}": str(roblox_username or ""),
        "{roblox_id}": str(roblox_id or ""),
        "{payment_type}": str(payment_type or ""),
        "{amount}": str(amount or ""),
        "{payment_id}": str(payment_id or ""),
        "{purchased}": f"<t:{ts}:F>",
    }
    comps = logging_config.get("purchase_components") or []
    if comps:
        raw = json.dumps(comps)
        for tok, val in subs.items():
            raw = raw.replace(tok, json.dumps(str(val))[1:-1])
        try:
            rendered = json.loads(raw)
        except Exception:
            rendered = comps
        try:
            await send_v2_message(ch, rendered, allowed_mentions={"parse": []})
            return
        except Exception as e:
            print(f"[Purchase] designed log failed, using default: {e}")
    # Default layout.
    lines = []
    lines.append(f"Customer: {cust}")
    if roblox_username:
        lines.append(f"Roblox account: {roblox_username}")
    if roblox_id:
        lines.append(f"Roblox user ID: {roblox_id}")
    lines.append("")
    if payment_type:
        lines.append(f"Payment type: {payment_type}")
    if amount:
        lines.append(f"Amount: {amount}")
    if payment_id:
        lines.append(f"Payment ID: {payment_id}")
    lines.append(f"Purchased: <t:{ts}:F>")
    embed = info_embed("Purchase Log", "\n".join(lines))
    try:
        await ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    except Exception as e:
        print(f"[Purchase] log failed: {e}")


@bot.tree.command(name="logtest", description="Posts a sample purchase log so you can check the channel and design.")
async def logtest_cmd(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(embed=error_embed("No permission", "Only staff can run this."), ephemeral=True)
        return
    if not logging_config.get("purchase_log_channel_id"):
        await interaction.response.send_message(embed=error_embed("Not set up", "Pick a Purchase logs channel in the Logging block first."), ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    await log_purchase(
        interaction.guild, discord_id=interaction.user.id, roblox_username="SampleUser",
        roblox_id="123456789", payment_type="Sample (test)", amount="R$ 500", payment_id="#TEST",
    )
    await interaction.followup.send(
        embed=success_embed("Sent", f"Sample purchase log posted in <#{logging_config.get('purchase_log_channel_id')}>."),
        ephemeral=True)


@bot.tree.command(name="logdebug", description="Checks why a purchase log shows no customer.")
@app_commands.describe(roblox_id="The buyer's Roblox user ID, like 376043957.", roblox_username="The buyer's Roblox username, if you have it.")
async def logdebug_cmd(interaction: discord.Interaction, roblox_id: str, roblox_username: str = ""):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(embed=error_embed("No permission", "Only staff can run this."), ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    dbg = await _robux_locker_call("verify_debug", roblox_id=roblox_id.strip(), roblox_username=roblox_username.strip())
    rev = await _robux_locker_call("roblox_reverse", roblox_id=roblox_id.strip(), roblox_username=roblox_username.strip())

    def _row(r):
        if not r:
            return "— none —"
        return f"discord=`{r.get('discord_user_id')}` roblox_id=`{r.get('roblox_id')}` name=`{r.get('roblox_username')}`"

    if not (isinstance(dbg, dict) and dbg.get("ok")):
        await interaction.followup.send(
            embed=error_embed("Debug failed", f"`{(dbg or {}).get('error', 'unknown')}`\n\nIf this says *Unknown action*, the robux-locker function hasn't redeployed yet."),
            ephemeral=True)
        return

    resolved = (rev or {}).get("discord_user_id")
    lines = [
        f"**Reverse lookup result:** {'<@' + str(resolved) + '>' if resolved else '❌ blank (this is why Customer is empty)'}",
        "",
        f"**Match by roblox_id (this bot):** {_row(dbg.get('by_id'))}",
        f"**Match by username (this bot):** {_row(dbg.get('by_name'))}",
        f"**Match by roblox_id (any bot):** {_row(dbg.get('any_bot'))}",
        f"**Total verifications for this bot:** `{dbg.get('total_for_bot')}`",
    ]
    hint = ""
    if not resolved:
        if dbg.get("any_bot") and not dbg.get("by_id"):
            hint = "\n\n➡️ A row exists under a **different bot_id**, the buyer verified with another bot."
        elif dbg.get("by_name") and not dbg.get("by_id"):
            hint = "\n\n➡️ Found by username, the row's `roblox_id` is empty. The username fallback now handles this; re-run a purchase."
        elif not dbg.get("by_id") and not dbg.get("by_name") and not dbg.get("any_bot"):
            hint = "\n\n➡️ No verification row at all for this Roblox account. The buyer isn't verified in this bot's `/verify` system."
    await interaction.followup.send(
        embed=success_embed(f"Verify debug, {roblox_id}", "\n".join(lines) + hint), ephemeral=True)


async def _log_group_sale(sale):
    """Log one Roblox group sale (from the sales poller) to the purchase channel."""
    buyer_roblox_id = str(sale.get("buyerId") or "")
    buyer_name = sale.get("buyerName") or ""
    discord_id = None
    if buyer_roblox_id or buyer_name:
        rev = await _robux_locker_call(
            "roblox_reverse", roblox_id=buyer_roblox_id, roblox_username=buyer_name,
        )
        discord_id = (rev or {}).get("discord_user_id")
    item_type = (sale.get("itemType") or "Item").strip()
    amount = int(sale.get("amount") or 0)
    when = None
    if sale.get("created"):
        try:
            dt = discord.utils.parse_time(str(sale["created"]))
            when = int(dt.timestamp()) if dt else None
        except Exception:
            when = None
    await log_purchase(
        None, discord_id=discord_id, roblox_username=buyer_name, roblox_id=buyer_roblox_id,
        payment_type=f"Roblox {item_type}".strip(), amount=f"R$ {amount}",
        payment_id=f"#{sale.get('id')}", when=when,
    )


_sales_diag = {"top": None}


@tasks.loop(seconds=30)
async def poll_group_sales():
    """Poll the Roblox group's recent sales and log any new ones. Dedups via a
    persisted seen-id cursor. On the first run it seeds the cursor WITHOUT logging
    (so old sales don't spam the channel)."""
    if not logging_config.get("purchase_log_channel_id"):
        return
    res = await _robux_locker_call("sales")
    if not (isinstance(res, dict) and res.get("ok")):
        if isinstance(res, dict) and res.get("error"):
            print(f"[Purchase] sales poll: {str(res.get('error'))[:200]}")
        return
    sales = res.get("sales") or []
    if not sales:
        return
    # Diagnostic: print ONLY when the newest sale changes, so a real purchase is
    # visible in the log the moment Roblox registers it.
    top = sales[0]
    top_id = str(top.get("id") or "")
    if top_id and top_id != _sales_diag.get("top"):
        _sales_diag["top"] = top_id
        print(f"[Purchase] newest sale changed -> {top.get('itemType')} '{top.get('itemName')}' "
              f"by {top.get('buyerName')} ({top.get('amount')} R$) id={top_id}")
    st = await _robux_locker_call("log_state_get")
    if not (isinstance(st, dict) and st.get("ok")):
        if isinstance(st, dict) and st.get("error"):
            print(f"[Purchase] log_state read: {str(st.get('error'))[:200]}")
        return
    seen_list = list((st or {}).get("seen_ids") or [])
    seen = set(seen_list)
    first_run = len(seen) == 0
    to_log = []
    added = False
    for sale in reversed(sales):  # oldest first, so logs post in order
        sid = str(sale.get("id") or "")
        if not sid or sid in seen:
            continue
        seen.add(sid)
        seen_list.append(sid)
        added = True
        if not first_run:
            to_log.append(sale)
    if first_run:
        print(f"[Purchase] seeded {len(seen_list)} existing sale(s) (first run — not logging these)")
    elif to_log:
        print(f"[Purchase] {len(to_log)} new sale(s) to log")
    if added:
        await _robux_locker_call("log_state_set", seen_ids=seen_list[-500:])
    for sale in to_log:
        try:
            await _log_group_sale(sale)
        except Exception as e:
            print(f"[Purchase] group sale log failed: {e}")


@poll_group_sales.before_loop
async def _before_poll_group_sales():
    await bot.wait_until_ready()


async def _log_stripe_sale(pi):
    """Log one paid Stripe payment (from the Stripe poller) to the purchase channel.
    Stripe never sees Discord identity, so Customer is the payer's name/email from
    the Stripe charge's billing details."""
    cents = int(pi.get("amount") or 0)
    cur = str(pi.get("currency") or "usd").upper()
    sym = "$" if cur == "USD" else ""
    amount = f"{sym}{cents / 100:.2f}" + ("" if sym else f" {cur}")
    when = int(pi.get("created")) if pi.get("created") else None
    name = (pi.get("customer_name") or "").strip()
    email = (pi.get("customer_email") or "").strip()
    if name and email:
        customer = f"{name} ({email})"
    else:
        customer = name or email or "N/A"
    await log_purchase(
        None, customer_name=customer,
        payment_type="Stripe", amount=amount, payment_id=f"#{pi.get('id')}", when=when,
    )


@tasks.loop(seconds=30)
async def poll_stripe_sales():
    """Poll recent paid Stripe payments and log any new ones, deduped by a
    persisted cursor. No first-run seeding — any paid customs payment we haven't
    logged yet gets posted."""
    if not logging_config.get("purchase_log_channel_id"):
        return
    res = await _payments_call("stripe_recent")
    if not (isinstance(res, dict) and res.get("ok")):
        err = str((res or {}).get("error") or "")
        if "valid price" in err or "Unknown" in err or "method" in err:
            print("[Purchase] stripe poll: payments-create isn't deployed with stripe_recent yet "
                  "(merge the edge function to the redesign branch).")
        elif err:
            print(f"[Purchase] stripe poll: {err[:200]}")
        return
    sales = res.get("sales") or []
    if not sales:
        return
    st = await _payments_call("stripe_state_get")
    if not (isinstance(st, dict) and st.get("ok")):
        if isinstance(st, dict) and st.get("error"):
            print(f"[Purchase] stripe_state read: {str(st.get('error'))[:200]}")
        return
    seen_list = list((st or {}).get("seen_ids") or [])
    seen = set(seen_list)
    to_log = []
    added = False
    for pi in sorted(sales, key=lambda p: int(p.get("created") or 0)):  # oldest first
        pid = str(pi.get("id") or "")
        if not pid or pid in seen:
            continue
        seen.add(pid)
        seen_list.append(pid)
        added = True
        to_log.append(pi)
    if to_log:
        print(f"[Purchase] {len(to_log)} new Stripe payment(s) to log")
    if added:
        await _payments_call("stripe_state_set", seen_ids=seen_list[-500:])
    for pi in to_log:
        try:
            await _log_stripe_sale(pi)
        except Exception as e:
            print(f"[Purchase] stripe sale log failed: {e}")


@poll_stripe_sales.before_loop
async def _before_poll_stripe_sales():
    await bot.wait_until_ready()




# ============================ Giveaways ============================

_DUR_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*(mo|s|m|h|d|w|y)$")
_DUR_MULT = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, "mo": 2592000, "y": 31536000}


def _parse_duration_seconds(text):
    """'30s' '10m' '2h' '1.5d' '1w' '1mo' '1y' -> seconds. Decimals are allowed
    with a unit; a bare number means DAYS (so '1' = 1 day). 0 if invalid."""
    if not text:
        return 0
    s = str(text).strip().lower()
    if s.isdigit():
        n = int(s)
        return n * 86400 if n > 0 else 0
    m = _DUR_RE.match(s)
    if not m:
        return 0
    n = float(m.group(1))
    if n <= 0:
        return 0
    return int(round(n * _DUR_MULT.get(m.group(2), 0)))


def _giveaway_can_manage(member):
    try:
        if member.guild_permissions.manage_guild:
            return True
    except Exception:
        pass
    return has_any_role(member, giveaway_config.get("manager_role_ids", []))


def _gw_cid(g, gid):
    """Enter-button custom_id carrying the giveaway's end time + winner count, so
    the bot can re-adopt a running giveaway after a redeploy without any storage."""
    try:
        return f"gw:{gid}|{int(g['end_ts'])}|{int(g['winners'])}"
    except Exception:
        return f"gw:{gid}"


def _giveaway_button(g, gid, disabled=False):
    label = str(giveaway_config.get("button_label") or "🎉 Enter")
    label, emoji = _extract_button_emoji(label)
    btn = {"type": 2, "style": 1, "custom_id": _gw_cid(g, gid), "disabled": bool(disabled)}
    if label:
        btn["label"] = label[:80]
    if emoji:
        btn["emoji"] = emoji
    return btn


def build_giveaway_embed(g, ended=False, winner_ids=None):
    prize = g["prize"]
    end_ts = int(g["end_ts"])
    entries = len(g["entrants"])
    winners = int(g["winners"])
    color = giveaway_config.get("color", ACCENT)
    try:
        color = int(color)
    except Exception:
        color = ACCENT

    title = str(giveaway_config.get("title") or "🎉 GIVEAWAY 🎉")
    lines = [f"### {prize}"]
    host_line = str(giveaway_config.get("host_line") or "").strip()
    if host_line:
        lines.append(host_line)
    lines.append("")

    if not ended:
        lines.append(f"Click **{str(giveaway_config.get('button_label') or 'Enter').strip()}** below to join!")
        lines.append(f"Ends: <t:{end_ts}:R>  •  <t:{end_ts}:f>")
    else:
        title = "🎉 GIVEAWAY ENDED 🎉"
        if winner_ids:
            mentions = ", ".join(f"<@{w}>" for w in winner_ids)
            lines.append(f"**Winner{'s' if len(winner_ids) != 1 else ''}:** {mentions}")
        else:
            lines.append("**No valid entries, no winner drawn.**")
        lines.append(f"Ended: <t:{end_ts}:f>")

    lines.append(f"Winners: **{winners}**  •  Entries: **{entries}**")
    if g.get("host_id"):
        lines.append(f"Hosted by <@{g['host_id']}>")

    embed = discord.Embed(title=title, description="\n".join(lines), color=color)
    return embed


def _giveaway_action_row(g, gid, ended=False):
    return {"type": 1, "components": [_giveaway_button(g, gid, disabled=ended)]}


def _giveaway_tokens(g, ended, winner_ids):
    end_ts = int(g["end_ts"])
    if winner_ids:
        wl = ", ".join(f"<@{w}>" for w in winner_ids)
    elif ended:
        wl = "No winners"
    else:
        wl = "TBD"
    entrants = list(g.get("entrants") or [])
    if entrants:
        participants = ", ".join(f"<@{u}>" for u in entrants)
    else:
        participants = "No one yet"
    return {
        "{prize}": g["prize"],
        "{winners}": str(int(g["winners"])),
        "{length}": str(g.get("length") or ""),
        "{entries}": str(len(g["entrants"])),
        "{reactions}": str(len(g["entrants"])),
        "{participants}": participants,
        "{end}": f"<t:{end_ts}:R>",
        "{end_full}": f"<t:{end_ts}:F>",
        "{host}": f"<@{g['host_id']}>" if g.get("host_id") else "",
        "{winner_list}": wl,
        "{button}": str(giveaway_config.get("button_label") or "Enter"),
    }


_GW_BLANKLINES_RE = re.compile(r"\n[ \t]*\n[ \t]*(?:\n[ \t]*)+")


def _giveaway_tidy_text(nodes):
    """After stripping {Question:} tokens, text blocks can be left with runs of
    blank lines. Collapse 3+ newlines to a single blank line and trim edges so
    the posted giveaway reads cleanly."""
    for n in nodes or []:
        if not isinstance(n, dict):
            continue
        if n.get("type") == 10 and isinstance(n.get("content"), str):
            n["content"] = _GW_BLANKLINES_RE.sub("\n\n", n["content"]).strip("\n")
        for key in ("components", "items"):
            if isinstance(n.get(key), list):
                _giveaway_tidy_text(n[key])


def _giveaway_render_design(g, gid, guild, ended=False, winner_ids=None, keep_running=False):
    """Render a giveaway layout with tokens filled in. While running (or when no
    dedicated ended design exists) uses the running design + Enter button. When
    ended AND a separate ended design is configured, uses that + a Reroll button.
    Returns None if no design is configured.

    keep_running=True forces the RUNNING design even after the giveaway ends (with
    the Enter button disabled) — used to leave the original giveaway message
    intact (prize/winners/entries visible) while the winner is announced in a
    separate message below it."""
    running_design = g.get("design") or giveaway_config.get("components") or []
    ended_design = giveaway_config.get("ended_components") or []
    # Only swap to the ended design if the running message was also a V2 design —
    # otherwise the posted message is an embed and can't be edited into V2.
    # keep_running never swaps: the original message stays on the running design.
    use_ended_design = bool(ended and ended_design and running_design) and not keep_running
    design = ended_design if use_ended_design else running_design
    if not design:
        return None

    def _js(x):
        return json.dumps(str(x))[1:-1]

    raw = json.dumps(design)
    for tok, val in _giveaway_tokens(g, ended, winner_ids).items():
        raw = raw.replace(tok, _js(val))
    # {Question: LABEL} is only the QUESTION (it defines the /giveaway form field).
    # It shows nothing in the posted message — the ANSWER shows via {prize} etc.
    raw = _QUESTION_RE.sub("", raw)
    try:
        comps = json.loads(raw)
    except Exception:
        comps = design

    built = [b for b in (_build_v2(c, guild) for c in comps) if b]
    _giveaway_tidy_text(built)

    if use_ended_design:
        # Dedicated ended message: winner text only, no buttons. Staff reroll via
        # the -reroll command. Never allow an empty payload — a blank edit would
        # make the message look "deleted" — so fall back to a minimal winner line.
        if not built:
            wl = ", ".join(f"<@{w}>" for w in (winner_ids or [])) or "No winners"
            built = [{"type": 10, "content": f"**Giveaway ended.** Winner: {wl}"}]
        return built

    # Bind any user-placed Counter buttons to THIS giveaway and disable them once
    # it's ended. If the design has none, append the default Enter row.
    def _bind_counter(node):
        found = False
        if isinstance(node, dict):
            if node.get("type") == 2 and str(node.get("custom_id", "")).startswith("gw:__COUNTER__"):
                node["custom_id"] = _gw_cid(g, gid)
                if ended:
                    node["disabled"] = True
                found = True
            for v in node.get("components", []) or []:
                found = _bind_counter(v) or found
        return found

    has_counter = False
    for c in built:
        has_counter = _bind_counter(c) or has_counter

    ping = str(giveaway_config.get("ping") or "").strip()
    if ping and not ended:
        built.insert(0, {"type": 10, "content": _render_guild_text(ping, guild)})
    if not has_counter:
        # No user-placed entry button — add the default Enter row (disabled on end).
        built.append(_giveaway_action_row(g, gid, ended))
    return built


def _giveaway_render_guard(built, g, gid, ended, winner_ids):
    """Never return an empty/whitespace-only render — a blank edit makes the
    posted giveaway look deleted. Guarantees at least one visible component."""
    real = [c for c in (built or []) if isinstance(c, dict)]
    if real:
        return built
    if ended:
        wl = ", ".join(f"<@{w}>" for w in (winner_ids or [])) or "No winners"
        return [{"type": 10, "content": f"**Giveaway ended.** Winner: {wl}"}]
    return [{"type": 10, "content": "**Giveaway**, click below to enter!"},
            _giveaway_action_row(g, gid, False)]


def _giveaway_payload(g, gid, guild, ended=False, winner_ids=None, for_edit=False, keep_running=False):
    """Build the message payload for a giveaway. Uses the designed V2 layout when
    one exists, otherwise the built-in embed. keep_running keeps the running
    layout (with the Enter button disabled) even after the giveaway ends."""
    design = _giveaway_render_design(g, gid, guild, ended, winner_ids, keep_running=keep_running)
    if design is not None:
        design = _giveaway_render_guard(design, g, gid, ended, winner_ids)
        payload = {"components": design}
        if not for_edit:
            payload["flags"] = 1 << 15  # Components V2
            payload["allowed_mentions"] = {"parse": ["roles", "users"]}
        return payload
    # Embed fallback. When keeping the running message on end, show the LIVE embed
    # (so prize/winners/entries stay) but disable the Enter button.
    embed = build_giveaway_embed(g, ended=(ended and not keep_running), winner_ids=winner_ids)
    payload = {"embeds": [embed.to_dict()], "components": [_giveaway_action_row(g, gid, ended)]}
    if not for_edit:
        payload["allowed_mentions"] = {"parse": ["roles", "users"]}
        ping = str(giveaway_config.get("ping") or "").strip()
        if ping:
            payload["content"] = _render_guild_text(ping, guild)
    return payload


def _giveaway_winner_payload(g, gid, guild, winner_ids, for_edit=False):
    """Standalone winner announcement, posted as a NEW message directly below the
    (kept) giveaway so its entry count stays visible. Uses the designed ended
    layout when one exists, otherwise a compact winner embed."""
    ended_design = giveaway_config.get("ended_components") or []
    running_design = g.get("design") or giveaway_config.get("components") or []
    am = {"parse": ["roles", "users"]}
    if ended_design and running_design:
        design = _giveaway_render_design(g, gid, guild, ended=True, winner_ids=winner_ids)
        if design:
            payload = {"components": design, "allowed_mentions": am}
            if not for_edit:
                payload["flags"] = 1 << 15  # Components V2
            return payload
    embed = build_giveaway_embed(g, ended=True, winner_ids=winner_ids)
    return {"embeds": [embed.to_dict()], "allowed_mentions": am}


async def _giveaway_send(channel, g, gid):
    guild = getattr(channel, "guild", None)
    payload = _giveaway_payload(g, gid, guild, ended=False)
    route = discord.http.Route("POST", "/channels/{channel_id}/messages", channel_id=channel.id)
    resp = await bot.http.request(route, json=payload)
    return str(resp["id"]) if isinstance(resp, dict) and resp.get("id") else None


async def _giveaway_patch(g, payload):
    """PATCH the giveaway message. Returns Discord's error code on failure
    (e.g. 30046 = edit cap on messages older than 1h), else None."""
    try:
        route = discord.http.Route(
            "PATCH", "/channels/{channel_id}/messages/{message_id}",
            channel_id=int(g["channel_id"]), message_id=int(g["message_id"]),
        )
        await bot.http.request(route, json=payload)
        return None
    except discord.HTTPException as e:
        print(f"[Giveaway] edit failed: {e}")
        return getattr(e, "code", None)
    except Exception as e:
        print(f"[Giveaway] edit failed: {e}")
        return None


# Live-count repaints are throttled: entries can burst (dozens of clicks in
# seconds) and Discord both rate-limits PATCH and hard-caps total edits to
# messages older than an hour (error 30046 — this produced a 51-line 429 storm
# in production). Entries themselves are never throttled — only how often the
# on-message counter repaints. One worker per giveaway coalesces bursts into
# one edit per window; hitting 30046 pauses repaints for 30 minutes (the final
# ended-state edit is separate and unaffected).
_gw_refresh_state = {}  # gid -> {"dirty","task","last","block_until"}


def _gw_msg_age_seconds(g):
    try:
        mid = int(g.get("message_id") or 0)
        posted = ((mid >> 22) + 1420070400000) / 1000  # snowflake -> unix secs
        return max(0.0, time.time() - posted)
    except Exception:
        return 0.0


async def _gw_refresh_worker(gid):
    st = _gw_refresh_state.get(gid)
    while st and st.get("dirty"):
        g = active_giveaways.get(gid)
        if not g or g.get("ended"):
            st["dirty"] = False
            return
        now = time.time()
        if now < st.get("block_until", 0):
            st["dirty"] = False
            return
        # Young messages repaint quickly; past the 1-hour mark Discord caps
        # total edits, so pace right down and let one edit carry the burst.
        min_gap = 8.0 if _gw_msg_age_seconds(g) < 3600 else 90.0
        wait = st.get("last", 0.0) + min_gap - now
        if wait > 0:
            await asyncio.sleep(wait)
            continue  # re-check dirty/ended after the nap
        st["dirty"] = False
        st["last"] = time.time()
        channel = await resolve_channel(g["channel_id"])
        guild = getattr(channel, "guild", None) if channel else None
        code = await _giveaway_patch(g, _giveaway_payload(g, gid, guild, ended=False, for_edit=True))
        if code == 30046:
            st["block_until"] = time.time() + 1800
            st["dirty"] = False
            print(f"[Giveaway] {gid}: Discord's old-message edit cap hit — pausing live count repaints for 30 min")
            return


async def _giveaway_refresh_count(gid):
    st = _gw_refresh_state.setdefault(gid, {"dirty": False, "task": None, "last": 0.0, "block_until": 0.0})
    st["dirty"] = True
    if st["task"] and not st["task"].done():
        return
    st["task"] = asyncio.create_task(_gw_refresh_worker(gid))


def _pick_winners(entrants, count):
    pool = [e for e in entrants]
    if not pool:
        return []
    return random.sample(pool, min(count, len(pool)))


async def start_giveaway(channel, prize, winners, seconds, host_id, guild_id, design=None, length=""):
    gid = secrets.token_hex(6)
    end_ts = int(time.time()) + seconds
    g = {
        "message_id": None, "channel_id": str(channel.id), "guild_id": str(guild_id or ""),
        "prize": prize, "winners": max(1, int(winners)), "end_ts": end_ts, "length": length or "",
        "host_id": str(host_id or ""), "entrants": set(), "ended": False,
        # Optional per-giveaway design override; normally None (uses the shared
        # dashboard design, with answer tokens filled from this giveaway's values).
        "design": design if isinstance(design, list) and design else None,
    }
    active_giveaways[gid] = g
    mid = await _giveaway_send(channel, g, gid)
    if not mid:
        active_giveaways.pop(gid, None)
        return None
    g["message_id"] = mid
    await _gw_save_state(gid, g)  # persist so it survives a redeploy immediately
    asyncio.create_task(_giveaway_timer(gid, seconds))
    return gid


async def _giveaway_timer(gid, seconds):
    try:
        await asyncio.sleep(max(1, seconds))
    except asyncio.CancelledError:
        return
    await end_giveaway(gid)


async def end_giveaway(gid, actor_id=None):
    g = active_giveaways.get(gid)
    if not g or g.get("ended"):
        return None
    g["ended"] = True
    winner_ids = _pick_winners(g["entrants"], g["winners"])
    g["last_winners"] = winner_ids
    channel = await resolve_channel(g["channel_id"])
    guild = getattr(channel, "guild", None) if channel else None
    # Keep the ORIGINAL giveaway message in place (prize / winners / ENTRIES stay
    # visible) and only disable its Enter button — never replace it with the
    # winner announcement.
    await _giveaway_patch(g, _giveaway_payload(g, gid, guild, ended=True, winner_ids=winner_ids, for_edit=True, keep_running=True))
    # Announce the winner as a NEW message directly below, so the entry count on
    # the giveaway above stays readable. Remember its id so -reroll edits it.
    if channel:
        try:
            route = discord.http.Route("POST", "/channels/{channel_id}/messages", channel_id=int(g["channel_id"]))
            resp = await bot.http.request(route, json=_giveaway_winner_payload(g, gid, guild, winner_ids))
            if isinstance(resp, dict) and resp.get("id"):
                g["winner_message_id"] = str(resp["id"])
        except Exception as e:
            print(f"[Giveaway] winner announce failed: {e}")
    await _gw_save_state(gid, g)  # persist ended state + winners + winner msg id for reroll after a redeploy
    print(f"[Giveaway] {gid} ended — giveaway message kept, winner posted below as {g.get('winner_message_id')}")
    return winner_ids


def _giveaway_params_from_answers(labels, mapping):
    """Work out prize / winner-count / duration (and the raw length text) from the
    {Question:} answers by matching label keywords. A {Question:} token is only a
    QUESTION — it defines a form field; the ANSWER shows via {prize}/{winners}/etc."""
    WINNER_KW = ("winner", "how many")
    LENGTH_KW = ("length", "duration", "how long")
    prize, winners, seconds, length_str = "", 1, 0, ""
    prize_set = False
    for lbl in labels:
        ans = (mapping.get(lbl) or "").strip()
        low = lbl.lower()
        if any(k in low for k in WINNER_KW):
            try:
                winners = max(1, min(int(re.sub(r"[^0-9]", "", ans) or "1"), 50))
            except Exception:
                winners = 1
        elif any(k in low for k in LENGTH_KW):
            seconds = _parse_duration_seconds(ans)
            length_str = ans
        elif "prize" in low and not prize_set:
            prize, prize_set = ans, True
    if not prize_set:
        # No explicit prize question — use the first non-winner/non-length answer.
        for lbl in labels:
            low = lbl.lower()
            if any(k in low for k in WINNER_KW + LENGTH_KW):
                continue
            if (mapping.get(lbl) or "").strip():
                prize = mapping[lbl].strip()
                break
    if not seconds:
        seconds = _parse_duration_seconds(str(giveaway_config.get("default_duration") or "1d")) or 86400
    return prize, winners, seconds, length_str


async def handle_giveaway_form_submit(interaction):
    try:
        await interaction.response.defer(ephemeral=True, thinking=True)
    except Exception as e:
        print(f"[Giveaway] form submit defer failed: {e}")
    try:
        design = giveaway_config.get("components") or []
        labels = _parse_questions(design)
        vals = _collect_modal_values((interaction.data or {}).get("components"))
        mapping = {lbl: (vals.get(f"q{i}") or "").strip() for i, lbl in enumerate(labels)}
        prize, winners, seconds, length_str = _giveaway_params_from_answers(labels, mapping)
        gid = await start_giveaway(
            interaction.channel, prize, winners, seconds,
            host_id=interaction.user.id, guild_id=getattr(interaction.guild, "id", None),
            length=length_str,
        )
        if gid:
            await interaction.followup.send(embed=success_embed("Giveaway started", f"Ends <t:{int(time.time()) + seconds}:R>, {winners} winner(s)."), ephemeral=True)
        else:
            await interaction.followup.send(embed=error_embed("Couldn't post", "I couldn't post the giveaway here. Check my permissions in this channel."), ephemeral=True)
    except Exception as e:
        import traceback
        print(f"[Giveaway] form submit failed: {e}\n{traceback.format_exc()}")
        try:
            await interaction.followup.send(embed=error_embed("Couldn't start giveaway", "Something went wrong. Please try again."), ephemeral=True)
        except Exception:
            pass


async def _open_giveaway_question_form(interaction, questions):
    components = []
    for i, q in enumerate(questions):
        components.append({
            "type": 18,  # Label
            "label": (_clean_label(q) or q)[:45],
            "component": {
                "type": 4, "custom_id": f"q{i}", "style": _form_input_style(q),
                "required": True, "max_length": 1000,
            },
        })
    data = {"title": "Start Giveaway", "custom_id": "giveawayform", "components": components}
    route = discord.http.Route(
        "POST", "/interactions/{interaction_id}/{interaction_token}/callback",
        interaction_id=interaction.id, interaction_token=interaction.token,
    )
    await bot.http.request(route, json={"type": 9, "data": data})


class GiveawayModal(discord.ui.Modal):
    def __init__(self):
        super().__init__(title="Start Giveaway", timeout=300)
        self.prize = discord.ui.TextInput(
            label="Prize", style=discord.TextStyle.short, required=True, max_length=200,
            placeholder="Discord Nitro, $20 gift card, 1000 Robux…",
        )
        self.winners = discord.ui.TextInput(
            label="Winner(s)", style=discord.TextStyle.short, required=False, max_length=3,
            default=str(giveaway_config.get("default_winners", 1)),
            placeholder="How many winners (e.g. 1)",
        )
        self.length = discord.ui.TextInput(
            label="Length", style=discord.TextStyle.short, required=True, max_length=8,
            default=str(giveaway_config.get("default_duration", "1d")),
            placeholder="30s, 10m, 2h, 1d, 1w (or just a number = days)",
        )
        self.add_item(self.prize)
        self.add_item(self.winners)
        self.add_item(self.length)

    async def on_submit(self, interaction):
        prize = str(self.prize.value or "").strip()
        try:
            winners = int(str(self.winners.value or "1").strip() or "1")
        except Exception:
            winners = 1
        winners = max(1, min(winners, 50))
        seconds = _parse_duration_seconds(self.length.value)
        if not prize:
            await interaction.response.send_message(embed=error_embed("Prize required", "Enter what you're giving away."), ephemeral=True)
            return
        if not seconds:
            await interaction.response.send_message(embed=error_embed("Invalid length", "Use a format like 10m, 2h, 1d, 1w, or 1mo."), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        gid = await start_giveaway(
            interaction.channel, prize, winners, seconds,
            host_id=interaction.user.id, guild_id=getattr(interaction.guild, "id", None),
            length=str(self.length.value or "").strip(),
        )
        if gid:
            await interaction.followup.send(embed=success_embed("Giveaway started", f"**{prize}**, {winners} winner(s), ends <t:{int(time.time()) + seconds}:R>."), ephemeral=True)
        else:
            await interaction.followup.send(embed=error_embed("Couldn't post", "I couldn't post the giveaway here. Check my permissions in this channel."), ephemeral=True)


@bot.tree.command(name="giveaway", description="Starts a giveaway with a prize, winner count, and length.")
async def giveaway_cmd(interaction: discord.Interaction):
    if not _giveaway_can_manage(interaction.user):
        await interaction.response.send_message(embed=error_embed("No permission", "Only staff can start giveaways."), ephemeral=True)
        return
    # If the design defines {Question:} fields, the form is built from those.
    # Otherwise fall back to the standard Prize / Winner(s) / Length modal.
    questions = _parse_questions(giveaway_config.get("components") or [])
    try:
        if questions:
            await _open_giveaway_question_form(interaction, questions)
        else:
            await interaction.response.send_modal(GiveawayModal())
    except Exception as e:
        print(f"[Giveaway] modal open failed: {e!r}")
        try:
            await interaction.response.send_message(embed=error_embed("Couldn't open form", str(e)[:300]), ephemeral=True)
        except Exception:
            pass


# ===================== Form logs (/orderlog, /infraction, /promote) =====================

async def _post_form_log(interaction, key, comps, files=None):
    """Post a completed log (design with answers + {user} filled in) to the log's
    configured channel. Assumes the interaction is already deferred (ephemeral)."""
    cfg = form_log_configs.get(key, {})
    ch = await resolve_channel(cfg.get("channel_id"))
    if not ch:
        await interaction.followup.send(embed=error_embed("No channel", "Pick a channel in the dashboard, then save it."), ephemeral=True)
        return
    def _js(s):
        return json.dumps(str(s))[1:-1]
    raw = json.dumps(comps or [])
    raw = raw.replace("{user}", _js(interaction.user.mention)).replace("{username}", _js(interaction.user.display_name))
    try:
        final = json.loads(raw)
    except Exception:
        final = comps
    _V2_LAST_ERROR["msg"] = ""
    if files:
        # Embed the uploaded files INSIDE the posted message.
        mid = await _send_v2_with_files(ch, final, files, allowed_mentions={"parse": []})
        if not mid:
            mid = await send_v2_message(ch, final, allowed_mentions={"parse": []})
            if mid:
                await _post_form_files(ch, files)
    else:
        mid = await send_v2_message(ch, final, allowed_mentions={"parse": []})
    if mid:
        await interaction.followup.send(embed=success_embed("Logged", f"Posted in {ch.mention}."), ephemeral=True)
    else:
        reason = _V2_LAST_ERROR.get("msg") or "unknown error"
        await interaction.followup.send(embed=error_embed("Couldn't post", f"Discord rejected the message: {reason}"), ephemeral=True)


async def _run_form_log(interaction, key):
    """Shared /orderlog, /infraction, /promote flow: gate → pop the form built
    from {Question:} tokens → post the filled-in design to the channel."""
    if not _form_log_can_run(key, interaction.user):
        await interaction.response.send_message(embed=error_embed("No permission", "You don't have a role allowed to run this command."), ephemeral=True)
        return
    comps = form_log_configs.get(key, {}).get("components") or []
    if not comps:
        await interaction.response.send_message(embed=error_embed("Not set up", "Design this in the dashboard first, then save it."), ephemeral=True)
        return
    # Register the design so the shared form pager can read its fields.
    form_msgs[key] = comps
    form_titles[key] = form_log_titles.get(key, "Log")
    fields = _parse_form_fields(comps, limit=FORM_MAX_QUESTIONS)
    if not fields:
        # No questions/files — just post the design straight to the channel.
        await interaction.response.defer(ephemeral=True, thinking=True)
        await _post_form_log(interaction, key, comps)
        return
    _pending_form_answers.pop((interaction.user.id, key), None)
    _pending_form_files.pop((interaction.user.id, key), None)
    await _open_form_page(interaction, key, 0)


@bot.tree.command(name="infraction", description="Logs an infraction with a quick form.")
async def infraction_cmd(interaction: discord.Interaction):
    await _run_form_log(interaction, "infraction")


@bot.tree.command(name="promote", description="Logs a promotion with a quick form.")
async def promote_cmd(interaction: discord.Interaction):
    await _run_form_log(interaction, "promotion")


# ===================== Auto infraction / promotion logging =====================
# When a dashboard-configured "watched" team role is REMOVED from a member we open
# an Infraction log; when one is ADDED we open a Promotion log. The staff member
# who made the change is found via the audit log and prompted (a Reason field + a
# responsibility checkbox) before it's posted to the configured channel.
_ROLELOG = {
    "infraction": {"key": "infraction", "verb": "removed", "prep": "from", "noun": "Infraction",
                   "emoji": "⚠️", "color": 0xED4245,
                   "reason_label": "Reason For Infraction",
                   "ack": "I take full responsibility for this infraction of the user."},
    "promotion":  {"key": "promotion", "verb": "added", "prep": "to", "noun": "Promotion",
                   "emoji": "⭐", "color": 0x57F287,
                   "reason_label": "Reason For Promotion",
                   "ack": "I confirm this promotion is authorized."},
}
_ROLELOG_CODE = {"i": "infraction", "p": "promotion"}
_ROLELOG_SHORT = {"infraction": "i", "promotion": "p"}
# (kind, target_id) -> {"roles": "<mentions>", "issuer_id": int}
_pending_rolelog = {}


async def _find_role_changer(guild, target_id):
    """Best-effort: who added/removed roles on target_id, from the audit log."""
    try:
        await asyncio.sleep(1.2)  # give the audit-log entry a moment to land
        async for entry in guild.audit_logs(limit=8, action=discord.AuditLogAction.member_role_update):
            tgt = getattr(entry, "target", None)
            if tgt and tgt.id == target_id:
                return entry.user
    except Exception as e:
        print(f"[RoleLog] audit lookup failed (needs View Audit Log perm?): {e}")
    return None


_ROLELOG_EDIT_WINDOW = 600  # seconds the reason can still be added / changed


async def _rolelog_render(kind, target_txt, logger_txt, roles_text, reason, date_txt):
    """Return (v2_components_or_None, plain_text) for the log, rendered from the
    dashboard V2 design if one is set."""
    comps = form_log_configs.get(kind, {}).get("components") or []
    if comps:
        try:
            def _js(s):
                return json.dumps(str(s))[1:-1]
            roles_val = _js(roles_text or "—")
            raw = json.dumps(comps)
            raw = re.sub(r"\{Question:[^}]*\}", _js(reason), raw)
            raw = re.sub(r"\{File:[^}]*\}", "", raw)
            raw = (raw.replace("{user}", _js(logger_txt))
                      .replace("{target}", _js(target_txt)).replace("{infractor}", _js(target_txt))
                      .replace("{date}", _js(date_txt))
                      .replace("{roles}", roles_val)
                      .replace("{roles removed}", roles_val).replace("{roles_removed}", roles_val)
                      .replace("{roles added}", roles_val).replace("{roles_added}", roles_val))
            return json.loads(raw), None
        except Exception as e:
            print(f"[RoleLog] design render failed, using plain text: {e}")
    header = "Infraction Logs" if kind == "infraction" else "Promotion Logs"
    return None, (f"## **{header}**\n\nUser: {target_txt}\nReason: {reason}\n"
                  f"Logger: {logger_txt}\n\nDate: {date_txt}")


async def _rolelog_expire(log_id):
    try:
        await asyncio.sleep(_ROLELOG_EDIT_WINDOW)
    except asyncio.CancelledError:
        return
    _pending_rolelog.pop(log_id, None)


async def _rolelog_trigger(guild, member, kind, roles):
    """Post the log immediately (Reason: N/A), then DM the person who made the
    change a copy + a Reason button that fills it in (editable for 10 minutes)."""
    meta = _ROLELOG[kind]
    ch = await resolve_channel(form_log_configs.get(kind, {}).get("channel_id"))
    if not ch:
        return
    issuer = await _find_role_changer(guild, member.id)
    if issuer and issuer.bot:
        issuer = None  # change made by an integration/bot
    roles_text = ", ".join(r.mention for r in roles) if roles else "—"
    target_txt = member.mention
    logger_txt = issuer.mention if issuer else "*unknown*"
    date_txt = f"<t:{int(time.time())}:F>"

    # 1) Post the log right now with Reason: N/A.
    final, txt = await _rolelog_render(kind, target_txt, logger_txt, roles_text, "N/A", date_txt)
    if final:
        log_id = await send_v2_message(ch, final, allowed_mentions={"parse": []})
    else:
        m = await ch.send(txt, allowed_mentions=discord.AllowedMentions.none())
        log_id = str(m.id) if m else None
    if not log_id:
        print("[RoleLog] failed to post the log")
        return
    log_id = str(log_id)
    _pending_rolelog[log_id] = {
        "kind": kind, "target_txt": target_txt, "issuer_id": issuer.id if issuer else 0,
        "logger_txt": logger_txt, "roles_text": roles_text, "channel_id": str(ch.id),
        "date_txt": date_txt, "ts": time.time(),
    }
    asyncio.create_task(_rolelog_expire(log_id))

    # 2) DM the person who made the change: a copy of the log with the reason
    #    button attached directly to it (green, no emoji, "{noun} Reasoning").
    btn = {"type": 2, "style": 3, "label": f"{meta['noun']} Reasoning",
           "custom_id": f"infrreason:{log_id}"}
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label=f"{meta['noun']} Reasoning",
                                    style=discord.ButtonStyle.success, custom_id=f"infrreason:{log_id}"))
    dmed = False
    if issuer:
        try:
            dm = await issuer.create_dm()
            if final:
                mid = await send_v2_message(dm, final, allowed_mentions={"parse": []}, buttons=[btn])
                dmed = bool(mid)
            else:
                await dm.send(txt, view=view)
                dmed = True
        except Exception as e:
            print(f"[RoleLog] DM to issuer failed: {e}")
    if not dmed:
        # Couldn't DM (unknown remover or their DMs are closed) — put the button
        # in the channel so someone can still add the reason.
        try:
            who = issuer.mention if issuer else "A staff member"
            await ch.send(f"{who}, add the reason (10 min):", view=view,
                          allowed_mentions=discord.AllowedMentions(users=[issuer] if issuer else False, roles=False))
        except Exception as e:
            print(f"[RoleLog] channel prompt failed: {e}")


class _RoleLogReasonModal(discord.ui.Modal):
    def __init__(self, log_id, kind):
        meta = _ROLELOG[kind]
        super().__init__(title=meta["noun"], timeout=600)
        self._log_id = str(log_id)
        self._kind = kind
        self.reason = discord.ui.TextInput(style=discord.TextStyle.paragraph, required=True,
                                           max_length=800, placeholder="Type the reason…")
        self.ack = discord.ui.Checkbox(custom_id="ack")
        self.add_item(discord.ui.Label(text=meta["reason_label"][:45], component=self.reason))
        self.add_item(discord.ui.Label(text="Responsibility", description=meta["ack"][:100], component=self.ack))

    async def on_submit(self, interaction):
        if not self.ack.value:
            await interaction.response.send_message(
                embed=error_embed("Acknowledgement required", _ROLELOG[self._kind]["ack"]), ephemeral=True)
            return
        reason = (self.reason.value or "").strip() or "No reason provided."
        await interaction.response.defer(ephemeral=True, thinking=True)
        await _rolelog_apply_reason(interaction, self._log_id, reason)


async def _rolelog_apply_reason(interaction, log_id, reason):
    pend = _pending_rolelog.get(log_id)
    if not pend or time.time() - pend.get("ts", 0) > _ROLELOG_EDIT_WINDOW:
        _pending_rolelog.pop(log_id, None)
        await interaction.followup.send(
            embed=error_embed("Time's up", "The 10-minute window to add a reason has passed."), ephemeral=True)
        return
    kind = pend["kind"]
    ch = await resolve_channel(pend["channel_id"])
    # If the remover was unknown, whoever adds the reason becomes the logger.
    logger_txt = pend["logger_txt"] if pend.get("issuer_id") else interaction.user.mention
    final, txt = await _rolelog_render(kind, pend["target_txt"], logger_txt, pend["roles_text"], reason, pend["date_txt"])
    ok = False
    if ch:
        if final:
            ok = await edit_v2_message(ch, log_id, final, allowed_mentions={"parse": []})
        else:
            try:
                m = await ch.fetch_message(int(log_id))
                await m.edit(content=txt)
                ok = True
            except Exception as e:
                print(f"[RoleLog] edit failed: {e}")
    _pending_rolelog.pop(log_id, None)  # reason set — lock it
    if ok:
        await interaction.followup.send(embed=success_embed("Reason added", "The log has been updated."), ephemeral=True)
    else:
        await interaction.followup.send(embed=error_embed("Couldn't update", "The log couldn't be edited."), ephemeral=True)


async def _rolelog_open_reason(interaction, cid):
    # cid = infrreason:{log_id}
    log_id = cid.split(":", 1)[1] if ":" in cid else ""
    pend = _pending_rolelog.get(log_id)
    if not pend or time.time() - pend.get("ts", 0) > _ROLELOG_EDIT_WINDOW:
        _pending_rolelog.pop(log_id, None)
        await interaction.response.send_message(
            embed=error_embed("Time's up", "The 10-minute window to add a reason has passed."), ephemeral=True)
        return
    issuer_id = pend.get("issuer_id") or 0
    is_admin = interaction.guild is not None and getattr(
        getattr(interaction.user, "guild_permissions", None), "manage_guild", False)
    if issuer_id and interaction.user.id != issuer_id and not is_admin:
        await interaction.response.send_message(
            embed=error_embed("Not yours", "Only the staff member who made this change (or an admin) can add the reason."),
            ephemeral=True)
        return
    try:
        await interaction.response.send_modal(_RoleLogReasonModal(log_id, pend["kind"]))
    except Exception as e:
        await interaction.response.send_message(embed=error_embed("Couldn't open form", str(e)[:150]), ephemeral=True)


def _rolelog_groups(kind):
    return form_log_configs.get(kind, {}).get("groups", []) or []


def _rolelog_watched_ids(kind):
    ids = set()
    for g in _rolelog_groups(kind):
        ids |= g["roles"]
    return ids


def _rolelog_hits(changed_roles, groups):
    """Return the changed role objects that belong to a group whose threshold is met.
    A group fires when >= `min` of its roles changed (min<=0/>len ⇒ all of them)."""
    by_id = {str(r.id): r for r in changed_roles}
    hit = {}
    for g in groups:
        present = [rid for rid in g["roles"] if rid in by_id]
        if not present:
            continue
        need = g["min"] if 0 < g["min"] <= len(g["roles"]) else len(g["roles"])
        if len(present) >= need:
            for rid in present:
                hit[rid] = by_id[rid]
    return list(hit.values())


# member_id -> {"removed": {id: role}, "added": {id: role}, "task": Task}
# Role changes are accumulated over a window so a "set" still fires whether the
# roles are pulled all at once or one-by-one. The timer RESETS on every new role
# change for that member, so it keeps watching until you STOP removing roles for
# this long — then it logs everything you removed together (whether that's 3 or 5).
_rolelog_accum = {}
_ROLELOG_WINDOW = 12.0


async def _rolelog_eval_later(guild_id, member_id):
    try:
        await asyncio.sleep(_ROLELOG_WINDOW)
    except asyncio.CancelledError:
        return
    acc = _rolelog_accum.pop(member_id, None)
    if not acc:
        return
    guild = bot.get_guild(guild_id)
    member = guild.get_member(member_id) if guild else None
    if not member:
        return
    removed = list(acc["removed"].values())
    added = list(acc["added"].values())
    rh = _rolelog_hits(removed, _rolelog_groups("infraction"))
    ah = _rolelog_hits(added, _rolelog_groups("promotion"))
    if rh:
        await _rolelog_trigger(guild, member, "infraction", rh)
    if ah:
        await _rolelog_trigger(guild, member, "promotion", ah)


@bot.event
async def on_member_update(before, after):
    """Auto infraction/promotion: a watched role-SET removed -> infraction, added -> promotion."""
    try:
        if before.roles == after.roles:
            return
        # Roblox group-rank sync: any role change may change the mapped rank.
        _schedule_group_sync(after)
        bset, aset = set(before.roles), set(after.roles)
        inf_ids = _rolelog_watched_ids("infraction")
        pro_ids = _rolelog_watched_ids("promotion")
        w_removed = [r for r in (bset - aset) if str(r.id) in inf_ids]
        w_added = [r for r in (aset - bset) if str(r.id) in pro_ids]
        # Diagnostic: show every role change and whether it matched the watch list.
        rem_ids = [str(r.id) for r in (bset - aset)]
        add_ids = [str(r.id) for r in (aset - bset)]
        if rem_ids or add_ids:
            print(f"[RoleLog] member_update {after.id} ({after.display_name}): "
                  f"removed={rem_ids} added={add_ids} | watched={sorted(inf_ids)} | "
                  f"matched_removed={[str(r.id) for r in w_removed]}")
        if not w_removed and not w_added:
            return
        acc = _rolelog_accum.get(after.id)
        if acc is None:
            acc = _rolelog_accum[after.id] = {"removed": {}, "added": {}, "task": None}
        for r in w_removed:
            acc["removed"][str(r.id)] = r
            acc["added"].pop(str(r.id), None)   # a re-add within the window cancels the remove
        for r in w_added:
            acc["added"][str(r.id)] = r
            acc["removed"].pop(str(r.id), None)
        old = acc.get("task")
        if old and not old.done():
            old.cancel()
        acc["task"] = asyncio.create_task(_rolelog_eval_later(after.guild.id, after.id))
    except Exception as e:
        print(f"[RoleLog] on_member_update error: {e}")


# --- Set watched role sets straight from Discord (bypasses the dashboard) ---
async def _bot_config_get(feature):
    if not (SUPABASE_URL and SUPABASE_KEY and BOT_ORDER_ID):
        return {}
    try:
        url = f"{SUPABASE_URL}/rest/v1/bot_config?bot_id=eq.{BOT_ORDER_ID}&feature=eq.{feature}&select=config"
        async with _http() as client:
            r = await client.get(url, headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}, timeout=15)
        if r.status_code == 200 and r.json():
            return r.json()[0].get("config") or {}
    except Exception as e:
        print(f"[RoleLog] config get failed: {e}")
    return {}


async def _durable_config_get(feature, attempts=6):
    """Like _bot_config_get but distinguishes a genuine (possibly empty) result
    from a transient failure, retrying with backoff. Returns (ok, config).
    ok=False means the read failed — callers must NOT enable saving, or they'd
    overwrite stored data with an empty snapshot."""
    if not (SUPABASE_URL and SUPABASE_KEY and BOT_ORDER_ID):
        return True, {}
    url = (f"{SUPABASE_URL}/rest/v1/bot_config?bot_id=eq.{BOT_ORDER_ID}"
           f"&feature=eq.{feature}&select=config")
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    err = ""
    for i in range(attempts):
        try:
            async with _http() as client:
                r = await client.get(url, headers=headers, timeout=20)
            if r.status_code == 200:
                rows = r.json()
                return True, ((rows[0].get("config") if rows else {}) or {})
            err = f"HTTP {r.status_code}"
        except Exception as e:
            err = str(e)[:140]
        if i < attempts - 1:
            await asyncio.sleep(min(30, 1.5 * (i + 1)))
    print(f"[Config] durable get '{feature}' failed after {attempts} tries: {err}")
    return False, {}


async def _bot_config_upsert_via_fn(feature, config):
    """Persist config through the service-role backend (utilities-bot-api
    /save-config). This bypasses RLS entirely, so features the bot writes itself
    (economy-data, ads-data, …) are saved without needing INSERT on the anon key.
    Returns (ok, err). ok=None means the endpoint isn't available (older backend)
    so the caller should fall back to a direct write."""
    if not (BOT_ORDER_ID and WORKER_TOKEN):
        return None, "no worker token"
    try:
        session = await get_poll_session()
        async with session.post(
            f"{SUPABASE_FN_URL}/{BOT_API}/save-config",
            headers=_fn_headers(),
            json={"bot_id": BOT_ORDER_ID, "feature": feature, "config": config},
        ) as r:
            if r.status == 200:
                return True, ""
            body = (await r.text())[:120]
            # 404/405 = endpoint not deployed yet → let the caller fall back.
            if r.status in (404, 405):
                return None, f"HTTP {r.status}"
            return False, f"HTTP {r.status}: {body}"
    except Exception as e:
        return None, str(e)[:120]


async def _bot_config_upsert(feature, config):
    if not (SUPABASE_URL and SUPABASE_KEY and BOT_ORDER_ID):
        return False, "no supabase creds"
    # Prefer the service-role backend (no RLS dependency); fall back to a direct
    # REST upsert if that endpoint isn't available.
    ok, err = await _bot_config_upsert_via_fn(feature, config)
    if ok is True:
        return True, ""
    fn_err = err
    try:
        url = f"{SUPABASE_URL}/rest/v1/bot_config?on_conflict=bot_id,feature"
        payload = {"bot_id": BOT_ORDER_ID, "feature": feature, "config": config,
                   "updated_at": discord.utils.utcnow().isoformat()}
        async with _http() as client:
            r = await client.post(
                url,
                headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
                         "Content-Type": "application/json",
                         "Prefer": "resolution=merge-duplicates,return=minimal"},
                json=payload, timeout=15)
        if r.status_code in (200, 201, 204):
            return True, ""
        # If the service-role write actively failed too, surface both.
        detail = f"HTTP {r.status_code}: {r.text[:100]}"
        if ok is False:
            detail = f"{detail} (fn: {fn_err})"
        return False, detail
    except Exception as e:
        return False, str(e)[:120]


class _RoleWatchSelect(discord.ui.RoleSelect):
    def __init__(self, kind, minv):
        super().__init__(placeholder="Pick the roles to watch…", min_values=1, max_values=25)
        self._kind = kind
        self._minv = int(minv or 0)

    async def callback(self, interaction):
        role_ids = [str(r.id) for r in self.values]
        await interaction.response.defer(ephemeral=True, thinking=True)
        key = self._kind
        feature = "customs-infraction" if key == "infraction" else "customs-promotion"
        cfg = await _bot_config_get(feature)
        cfg["group1_roles"] = role_ids
        cfg["group1_min"] = self._minv
        form_log_configs[key]["groups"] = _parse_role_groups(cfg)  # apply live, now
        ok, err = await _bot_config_upsert(feature, cfg)
        names = ", ".join(f"<@&{rid}>" for rid in role_ids)
        verb = "removed" if key == "infraction" else "added"
        trig = "all of them" if self._minv <= 0 or self._minv > len(role_ids) else f"{self._minv} of them"
        note = "Saved permanently." if ok else f"Applied now, but couldn't write to the dashboard DB ({err}), resets on a full restart."
        ch = await resolve_channel(form_log_configs[key].get("channel_id"))
        chnote = f"\nLogging to {ch.mention}." if ch else "\n⚠️ No log channel set for this yet, set one in the dashboard."
        await interaction.followup.send(
            embed=success_embed(f"{key.title()} roles set",
                                f"Watching: {names}\nFires when **{trig}** are **{verb}**.{chnote}\n{note}"),
            ephemeral=True)
        print(f"[RoleLog] {key} watch set via command -> {role_ids} min={self._minv} persist_ok={ok} {err}")


class _RoleWatchView(discord.ui.View):
    def __init__(self, kind, minv):
        super().__init__(timeout=300)
        self.add_item(_RoleWatchSelect(kind, minv))


@bot.tree.command(name="infractionroles", description="Sets the roles that log an infraction when they're removed.")
@app_commands.describe(min="How many must be removed to trigger. 0 means all of them.")
async def infractionroles_cmd(interaction: discord.Interaction, min: int = 0):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(embed=error_embed("Admins only", "You need Manage Server."), ephemeral=True)
        return
    await interaction.response.send_message(
        embed=info_embed("Infraction roles", "Pick the roles to watch, removing them (that many, within a few seconds) auto-logs an infraction."),
        view=_RoleWatchView("infraction", min), ephemeral=True)


@bot.tree.command(name="promotionroles", description="Sets the roles that log a promotion when they're added.")
@app_commands.describe(min="How many must be added to trigger. 0 means all of them.")
async def promotionroles_cmd(interaction: discord.Interaction, min: int = 0):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(embed=error_embed("Admins only", "You need Manage Server."), ephemeral=True)
        return
    await interaction.response.send_message(
        embed=info_embed("Promotion roles", "Pick the roles to watch, adding them (that many, within a few seconds) auto-logs a promotion."),
        view=_RoleWatchView("promotion", min), ephemeral=True)


@bot.tree.command(name="grouproleupdate", description="Syncs everyone's Roblox group rank to their Discord roles.")
async def grouproleupdate_cmd(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(embed=error_embed("Admins only", "You need Manage Server."), ephemeral=True)
        return
    if not (group_sync_config["enabled"] and group_sync_config["group_id"] and group_sync_config["tiers"]):
        await interaction.response.send_message(
            embed=error_embed("Not set up", "Roblox Group Sync isn't configured yet. In the dashboard, open **Roblox Group Sync**, set the group ID, and map at least one role to a rank number."),
            ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    result, err = await _group_sync_scan(interaction.guild)
    if err:
        await interaction.followup.send(
            embed=error_embed("Sync failed", f"Roblox call failed: {err}"), ephemeral=True)
        return
    if result["checked"] == 0:
        await interaction.followup.send(
            embed=info_embed("Nobody to sync", "No members currently hold a role that's mapped to a Roblox rank."),
            ephemeral=True)
        return
    totals = result["totals"]
    skip_reasons = result["skip_reasons"]
    no_perm = result["no_perm"]
    other_fails = result["other_fails"]

    def _mentions(ids, cap=10):
        picked = [f"<@{i}>" for i in ids if i][:cap]
        extra = len([i for i in ids if i]) - len(picked)
        return ", ".join(picked) + (f" +{extra} more" if extra > 0 else "")

    reason_label = {
        "not_verified": "haven't linked their Roblox (Verify button)",
        "not_in_group": "aren't in the group",
        "no_such_rank": "mapped to a rank number that doesn't exist in the group",
    }
    lines = [
        f"**Checked:** {result['checked']} member(s)",
        f"**Updated:** {totals['changed']}",
        f"**Already correct:** {totals['unchanged']}",
        f"**Skipped:** {totals['skipped']}",
    ]
    for code, n in sorted(skip_reasons.items(), key=lambda kv: -kv[1]):
        lines.append(f"  • {n} — {reason_label.get(code, code)}")
    if totals["failed"]:
        lines.append(f"**Failed:** {totals['failed']}")
        if no_perm:
            lines.append(f"  • Roblox won't let the bot rank these (they're the group owner, or ranked at/above the bot account): {_mentions(no_perm)}")
        for did, err in other_fails:
            who = f"<@{did}>" if did else "someone"
            lines.append(f"  • {who}: {err}")
    embed = success_embed("Group ranks synced", "\n".join(lines)) if not totals["failed"] \
        else error_embed("Group ranks synced (with errors)", "\n".join(lines))
    await interaction.followup.send(embed=embed, ephemeral=True)


async def handle_notify_click(interaction, ids_csv):
    """Notification button — toggle the selected role(s) on the clicker."""
    guild = interaction.guild
    member = getattr(interaction, "user", None)
    if not (guild and isinstance(member, discord.Member)):
        try:
            await interaction.response.send_message(embed=error_embed("Unavailable", "This only works inside a server."), ephemeral=True)
        except Exception:
            pass
        return
    roles = []
    for rid in str(ids_csv).split(","):
        rid = rid.strip()
        if rid.isdigit():
            r = guild.get_role(int(rid))
            if r:
                roles.append(r)
    if not roles:
        try:
            await interaction.response.send_message(embed=error_embed("Not set up", "No roles are attached to this button."), ephemeral=True)
        except Exception:
            pass
        return
    added, removed = [], []
    try:
        for r in roles:
            if r in member.roles:
                await member.remove_roles(r, reason="Notification button")
                removed.append(r.mention)
            else:
                await member.add_roles(r, reason="Notification button")
                added.append(r.mention)
    except discord.Forbidden:
        try:
            await interaction.response.send_message(embed=error_embed("Missing permission", "I can't manage that role, make sure my role is above it."), ephemeral=True)
        except Exception:
            pass
        return
    except Exception as e:
        print(f"[Notify] toggle failed: {e}")
    parts = []
    if added:
        parts.append("Added " + ", ".join(added))
    if removed:
        parts.append("Removed " + ", ".join(removed))
    msg = " · ".join(parts) if parts else "No changes."
    try:
        await interaction.response.send_message(embed=success_embed("Notifications", msg), ephemeral=True)
    except Exception:
        pass


def _parse_gw_cid(raw):
    """Split the Enter button's custom_id payload ('gid' or 'gid|end_ts|winners')."""
    parts = str(raw).split("|")
    gid = parts[0]
    end_ts = int(parts[1]) if len(parts) > 1 and parts[1].lstrip("-").isdigit() else None
    winners = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None
    return gid, end_ts, winners


async def _gw_entries_call(action, gid=None, uid=None, meta=None, entrants=None):
    """Persist/read giveaway state server-side so giveaways + entries survive
    redeploys. Supports get_all (no gid), get, set_state, add, remove, clear."""
    payload = {"action": action}
    if gid is not None:
        payload["gid"] = str(gid)
    if uid is not None:
        payload["uid"] = str(uid)
    if meta is not None:
        payload["meta"] = meta
    if entrants is not None:
        payload["entrants"] = [str(u) for u in entrants]
    try:
        async with _http() as client:
            r = await client.post(
                f"{SUPABASE_FN_URL}/giveaway-entries",
                headers=_fn_headers(), json=payload, timeout=15,
            )
            data = r.json() if r.content else {}
            if r.status_code != 200 or (isinstance(data, dict) and data.get("error")):
                # A 404 here means the 'giveaway-entries' edge function isn't deployed.
                print(f"[Giveaway] entries {action} -> HTTP {r.status_code}: {str(data)[:200]}")
            return data
    except Exception as e:
        print(f"[Giveaway] entries {action} call failed: {e}")
        return {"error": str(e)[:200]}


def _gw_meta(g):
    """JSON-safe snapshot of a giveaway's metadata (everything but the entrants set)."""
    return {k: v for k, v in g.items() if k != "entrants"}


async def _gw_save_state(gid, g):
    """Persist a giveaway's full state (metadata + entrants). Called on create,
    on end, and on shutdown so the whole giveaway is remembered across redeploys."""
    try:
        await _gw_entries_call("set_state", gid=gid, meta=_gw_meta(g),
                               entrants=list(g.get("entrants") or []))
    except Exception as e:
        print(f"[Giveaway] save_state {gid} failed: {e}")


async def _gw_restore_all():
    """On boot, rebuild EVERY saved giveaway into memory with its entrants and
    re-arm its timer — so giveaways come back fully after a redeploy without
    waiting for anyone to click."""
    res = await _gw_entries_call("get_all")
    if not (isinstance(res, dict) and res.get("ok")):
        print(f"[Giveaway] restore skipped (is 'giveaway-entries' deployed?): {(res or {}).get('error')}")
        return
    gws = res.get("giveaways") or {}
    now = int(time.time())
    restored = 0
    for gid, state in gws.items():
        meta = (state or {}).get("meta") or {}
        if not meta.get("channel_id"):
            continue  # incomplete (legacy entrants-only) — handled lazily on click
        g = dict(meta)
        g["entrants"] = set(str(u) for u in ((state or {}).get("entrants") or []))
        g.setdefault("ended", False)
        g.setdefault("winners", 1)
        active_giveaways[gid] = g
        restored += 1
        if not g.get("ended"):
            end_ts = int(g.get("end_ts") or 0)
            if end_ts:
                remaining = end_ts - now
                if remaining <= 0:
                    asyncio.create_task(end_giveaway(gid))
                else:
                    asyncio.create_task(_giveaway_timer(gid, remaining))
                    # Re-draw the message so the Entries count reflects the restored
                    # entrants immediately (not the pre-redeploy render).
                    asyncio.create_task(_giveaway_refresh_count(gid))
    if restored:
        print(f"[Giveaway] restored {restored} giveaway(s) from storage")


async def _giveaway_adopt(interaction, gid, end_ts, winners):
    """Rebuild a running giveaway the process lost on restart, from the button's
    encoded end time/winners + the message it's on, then reschedule its end.
    Entrants are reloaded from storage so nobody's entry is ever dropped."""
    if end_ts is None:
        return None
    msg = getattr(interaction, "message", None)
    g = {
        "message_id": str(msg.id) if msg else None,
        "channel_id": str(interaction.channel.id),
        "guild_id": str(getattr(interaction.guild, "id", "") or ""),
        "prize": "", "winners": max(1, winners or 1), "end_ts": int(end_ts),
        "length": "", "host_id": "", "entrants": set(), "ended": False, "design": None,
    }
    active_giveaways[gid] = g
    # Restore the persisted entrant list from before the restart.
    try:
        res = await _gw_entries_call("get", gid)
        if isinstance(res, dict) and res.get("ok"):
            g["entrants"] = set(str(u) for u in (res.get("entrants") or []))
    except Exception as e:
        print(f"[Giveaway] entrant restore failed for {gid}: {e}")
    remaining = int(end_ts) - int(time.time())
    asyncio.create_task(end_giveaway(gid) if remaining <= 0 else _giveaway_timer(gid, remaining))
    print(f"[Giveaway] re-adopted {gid} after restart ({len(g['entrants'])} entrants, ends in {remaining}s)")
    return g


async def giveaway_enter(interaction, raw):
    gid, end_ts, winners = _parse_gw_cid(raw)
    g = active_giveaways.get(gid)
    if not g:
        # Lost on restart — rebuild from the button so it never breaks.
        g = await _giveaway_adopt(interaction, gid, end_ts, winners)
    if not g:
        await interaction.response.send_message(embed=error_embed("Giveaway unavailable", "This giveaway is no longer active."), ephemeral=True)
        return
    if g.get("ended"):
        await interaction.response.send_message(embed=error_embed("Giveaway ended", "This giveaway has already ended."), ephemeral=True)
        return
    uid = str(interaction.user.id)
    if uid in g["entrants"]:
        g["entrants"].discard(uid)
        msg, entry_action = "Giveaway Left", "remove"
    else:
        g["entrants"].add(uid)
        msg, entry_action = "Giveaway Entered", "add"
    await interaction.response.send_message(msg, ephemeral=True)
    # Persist the entry so it survives a redeploy, then refresh the live count.
    await _gw_entries_call(entry_action, gid, uid)
    await _giveaway_refresh_count(gid)


async def _cmd_reroll(message):
    """'-reroll' — draw a new winner for an ended giveaway. Only giveaway managers
    can use it. Reply to a specific giveaway to reroll that one; otherwise it picks
    the most recently ended giveaway in the channel. Edits the giveaway message in
    place (no new message) and reacts to confirm."""
    async def react(emoji):
        try:
            await message.add_reaction(emoji)
        except Exception:
            pass

    if not _giveaway_can_manage(message.author):
        return await react("⛔")

    chan_id = str(message.channel.id)
    ref = getattr(message, "reference", None)
    ref_mid = str(ref.message_id) if ref and getattr(ref, "message_id", None) else None

    target = None
    if ref_mid:
        for gid, g in active_giveaways.items():
            if str(g.get("channel_id")) == chan_id and str(g.get("message_id")) == ref_mid:
                target = (gid, g)
                break
    if target is None:
        ended = [(gid, g) for gid, g in active_giveaways.items()
                 if str(g.get("channel_id")) == chan_id and g.get("ended")]
        if ended:
            target = max(ended, key=lambda kv: int(kv[1].get("end_ts") or 0))

    if target is None or not target[1].get("ended"):
        return await react("❓")

    gid, g = target
    winners = _pick_winners(g["entrants"], g["winners"])
    if not winners:
        return await react("❌")

    g["last_winners"] = winners
    guild = message.guild
    win_mid = g.get("winner_message_id")
    if win_mid:
        # New model: the winner lives in its own message below the giveaway.
        # Edit that one; leave the giveaway (with its entry count) untouched.
        try:
            route = discord.http.Route(
                "PATCH", "/channels/{channel_id}/messages/{message_id}",
                channel_id=int(g["channel_id"]), message_id=int(win_mid),
            )
            await bot.http.request(route, json=_giveaway_winner_payload(g, gid, guild, winners, for_edit=True))
        except Exception as e:
            print(f"[Giveaway] reroll winner-message edit failed: {e}")
            return await react("❌")
    else:
        # Legacy giveaways that ended before this change: edit the main message.
        await _giveaway_patch(g, _giveaway_payload(g, gid, guild, ended=True, winner_ids=winners, for_edit=True))
    await _gw_save_state(gid, g)
    await react("✅")


@bot.event
async def on_raw_message_delete(payload):
    """The giveaway message lives forever on its own — the bot only ever edits it.
    The one thing that ends a giveaway's tracking is the message being MANUALLY
    deleted: when that happens, drop the giveaway so nothing tries to edit a gone
    message and its end timer becomes a no-op."""
    mid = str(getattr(payload, "message_id", ""))
    if not mid:
        return
    for gid, g in list(active_giveaways.items()):
        if str(g.get("message_id")) == mid:
            active_giveaways.pop(gid, None)
            print(f"[Giveaway] {gid} message {mid} was deleted — dropped from tracking")


@bot.event
async def on_interaction(interaction: discord.Interaction):
    # Form submits arrive as modal_submit interactions (not component). Handle
    # ours here; leave every other modal (Close Order, etc.) to discord.py's own
    # Modal dispatch by returning. This fires regardless of restarts, so forms
    # keep working across redeploys.
    if interaction.type == discord.InteractionType.modal_submit:
        cid = (interaction.data or {}).get("custom_id", "")
        if cid.startswith("pf:"):
            rest = cid.split(":", 1)[1]
            # "feature" or "feature:formnum" (feature names contain hyphens, not colons)
            feat, _, fnum = rest.rpartition(":")
            if feat and fnum.isdigit():
                await _pf_submit(interaction, feat, int(fnum))
            else:
                await _pf_submit(interaction, rest, 1)
        elif cid.startswith("ticketform:"):
            payload = cid.split(":", 1)[1]
            if "|" in payload:
                fkey, pg = payload.rsplit("|", 1)
                try:
                    pg = int(pg)
                except Exception:
                    pg = 0
            else:
                fkey, pg = payload, 0
            await handle_ticket_form_submit(interaction, fkey, pg)
        elif cid == "giveawayform":
            await handle_giveaway_form_submit(interaction)
        return
    if interaction.type != discord.InteractionType.component:
        return
    cid = (interaction.data or {}).get("custom_id", "")
    if cid.startswith("npv2_"):
        await handle_npv2_button(interaction, cid)
        return
    if cid.startswith(("ticket_msg:", "ticket_form:", "eph:", "ticket_cat:")) or cid in ("ticket_select", "ticket_open"):
        print(f"[Tickets] interaction cid={cid!r} values={(interaction.data or {}).get('values')}")
    if cid == "ticket_select":
        values = (interaction.data or {}).get("values") or []
        if values:
            v = values[0]
            if v.startswith("ticket_msg:"):
                await _dispatch_ticket_open(interaction, v)
            elif v.startswith("ticket_form:"):
                await open_ticket_form(interaction, v.split(":", 1)[1])
            elif v.startswith("eph:"):
                await show_ephemeral(interaction, v.split(":", 1)[1])
            elif v.startswith("ch:") or v.startswith("url:"):
                try:
                    await interaction.response.defer(ephemeral=True)
                except Exception:
                    pass
            else:
                await _dispatch_ticket_open(interaction, v)
    elif cid.startswith("ticket_msg:"):
        await _dispatch_ticket_open(interaction, cid)
    elif cid.startswith("ticket_form:"):
        await open_ticket_form(interaction, cid.split(":", 1)[1])
    elif cid.startswith("formcont:"):
        payload = cid.split(":", 1)[1]
        fkey, pg = (payload.rsplit("|", 1) + ["0"])[:2] if "|" in payload else (payload, "0")
        try:
            pg = int(pg)
        except Exception:
            pg = 0
        await _open_form_page(interaction, fkey, pg)
    elif cid.startswith("eph:"):
        await show_ephemeral(interaction, cid.split(":", 1)[1])
    elif cid.startswith("ticket_cat:"):
        await _dispatch_ticket_open(interaction, cid)
    elif cid == "ticket_open":
        await _dispatch_ticket_open(interaction, "ticket_open")
    elif cid == "session_action":
        await _session_select(interaction)
    elif cid == "session_vote":
        await _session_vote_click(interaction)
    elif cid.startswith("shift_"):
        await _shift_button(interaction, cid)
    elif cid == "ticket_claim":
        await ticket_claim_toggle(interaction, True)
    elif cid == "ticket_unclaim":
        await ticket_claim_toggle(interaction, False)
    elif cid == "ticket_close":
        await ticket_close_prompt(interaction)
    elif cid == "ticket_closetype":
        values = (interaction.data or {}).get("values") or []
        mode = values[0] if values else "instant"
        await interaction.response.send_modal(CloseReasonModal(mode))
    elif cid == "ticket_close_confirm":
        await close_ticket(interaction)
    elif cid == "roblox_verify":
        await start_roblox_verify(interaction)
    elif cid.startswith("gw:"):
        await giveaway_enter(interaction, cid.split(":", 1)[1])
    elif cid.startswith("notifyrole:"):
        await handle_notify_click(interaction, cid.split(":", 1)[1])
    elif cid.startswith("infrreason:"):
        await _rolelog_open_reason(interaction, cid)
    elif cid == "ad_claim":
        await _ads_open_claim(interaction)
    elif cid == "ad_queue":
        await _ads_open_queue(interaction)
    elif cid == "adsel_use":
        v = ((interaction.data or {}).get("values") or [None])[0]
        await _ads_handle_use(interaction, v)
    elif cid.startswith("ad_ok:"):
        await _ads_decide(interaction, cid.split(":", 1)[1], True)
    elif cid.startswith("ad_no:"):
        await _ads_decide(interaction, cid.split(":", 1)[1], False)
    elif cid.startswith("ad_delay:"):
        await _ads_open_delay(interaction, cid.split(":", 1)[1])
    elif cid.startswith("adinv:"):
        parts = cid.split(":")
        await interaction.response.send_modal(
            AdUpdateInviteModal(parts[1] if len(parts) > 1 else "", parts[2] if len(parts) > 2 else ""))


def _ticket_topic(opener_id, category, base=""):
    return f"ticket|{opener_id}|{category}|{base}"


def _san_name(x):
    x = re.sub(r"<[^>]+>", "", str(x or ""))
    x = x.lower().replace(" ", "-")
    x = re.sub(r"[^a-z0-9\-]", "", x)
    x = re.sub(r"-+", "-", x).strip("-")
    return x[:40] or "ticket"


def _ticket_first_word(open_comps):
    def find_text(items):
        for c in (items or []):
            if not isinstance(c, dict):
                continue
            t = c.get("type")
            if t in ("text", "text_display", "section"):
                txt = c.get("text") or c.get("content") or c.get("title") or ""
                if str(txt).strip():
                    return str(txt)
            if t == "container":
                r = find_text(c.get("children") or c.get("components") or [])
                if r:
                    return r
        return ""
    txt = re.sub(r"<[^>]+>", "", find_text(open_comps))
    txt = re.sub(r"[*_`~>#|:\-]", " ", txt)
    words = [w for w in txt.split() if w]
    return words[0] if words else ""


_QUESTION_RE = re.compile(r"\{Question:\s*(.*?)\}", re.IGNORECASE)
# A form field is either a {Question: Label} (text input) or a {File: Label}
# (file upload — Discord modals support file components now).
_FIELD_RE = re.compile(r"\{(LQuestion|Question|SFile|File):\s*(.*?)\}", re.IGNORECASE)


def _existing_ticket_for(guild, user_id):
    for ch in guild.text_channels:
        topic = ch.topic or ""
        if topic.startswith("ticket|") and topic.split("|")[1] == str(user_id):
            return ch
    return None


# How many open tickets a member may have per section (category) at once.
MAX_TICKETS_PER_SECTION = 2


def _user_ticket_count_for(guild, user_id, cat_name, fallback_cat_channel):
    """Count a member's open tickets in one section.
    - cat_name given → match channels whose Discord category name == cat_name
      (so all Ticket/Form types that share a category name count together).
    - no cat_name → match channels in the fallback (global) category / uncategorized.
    """
    uid = str(user_id)
    target_name = (cat_name or "").strip().lower()
    fb_id = fallback_cat_channel.id if fallback_cat_channel else None
    count = 0
    for ch in guild.text_channels:
        topic = ch.topic or ""
        if not (topic.startswith("ticket|") and topic.split("|")[1] == uid):
            continue
        if target_name:
            ch_name = ch.category.name.strip().lower() if ch.category else ""
            if ch_name == target_name:
                count += 1
        else:
            ch_id = ch.category.id if ch.category else None
            if ch_id == fb_id:
                count += 1
    return count


def _open_ticket_count_for_category(guild, cat_name):
    """Count ALL open order tickets in one Discord category (any opener). Used by
    the Order Status embed to decide open/limited/closed per service."""
    target = (cat_name or "").strip().lower()
    if not (guild and target):
        return 0
    count = 0
    for ch in guild.text_channels:
        topic = ch.topic or ""
        if not topic.startswith("ticket|"):
            continue
        ch_cat = ch.category.name.strip().lower() if ch.category else ""
        if ch_cat == target:
            count += 1
    return count


def _clean_label(s):
    """Strip markdown emphasis so a {Question: **Server Name:**} token shows a
    clean 'Server Name:' label in the modal instead of literal asterisks."""
    return re.sub(r"[*_`~]", "", s or "").strip()


def _parse_questions(open_comps, limit=5):
    """Ordered, de-duplicated list of {Question: LABEL} labels in a design.
    Discord modals hold 5 fields each; ticket forms page across two modals so
    they allow up to 10 (limit=10). Other callers keep the single-modal 5."""
    raw = json.dumps(open_comps or [])
    seen = []
    for m in _QUESTION_RE.finditer(raw):
        lbl = (m.group(1) or "").strip()
        if lbl and lbl not in seen:
            seen.append(lbl)
    return seen[:limit]


def _parse_form_fields(open_comps, limit=10):
    """Ordered, de-duplicated form fields in a design — both {Question: LABEL}
    (text) and {File: LABEL} (upload) — in the order they appear. Each is
    {"kind": "q"|"file", "label": ...}."""
    raw = json.dumps(open_comps or [])
    seen = set()
    fields = []
    for m in _FIELD_RE.finditer(raw):
        g = m.group(1).lower()
        kind = "file" if g in ("file", "sfile") else "q"
        label = (m.group(2) or "").strip()
        sig = (kind, label.lower())
        if label and sig not in seen:
            seen.add(sig)
            # long = paragraph text input; before = file rendered above the message.
            fields.append({"kind": kind, "label": label, "long": g == "lquestion", "before": g == "sfile"})
    return fields[:limit]


# In-progress ticket-form answers + uploaded files between paged modals,
# keyed by (user_id, key).
_pending_form_answers = {}
_pending_form_files = {}
FORM_PAGE_SIZE = 5
FORM_MAX_QUESTIONS = 10


async def _post_form_files(channel, files, label=True):
    """Upload collected form files into a channel. With label=True each file gets
    a bold header naming its field; with label=False just the file is posted (no
    words), e.g. a suggestion's image sitting bare in its thread."""
    for f in files or []:
        try:
            async with _http() as client:
                r = await client.get(f["url"], timeout=90, follow_redirects=True)
                if r.status_code != 200:
                    continue
                blob = r.content
            content = f"**{_clean_label(f.get('label') or 'File')}**" if label else None
            await channel.send(content=content,
                               file=discord.File(io.BytesIO(blob), filename=f.get("filename") or "file"))
        except Exception as e:
            print(f"[Form] file post failed: {e}")


def _is_image_name(filename):
    return str(filename or "").lower().rsplit(".", 1)[-1] in ("png", "jpg", "jpeg", "gif", "webp")


def _san_filename(name, fallback="file"):
    n = re.sub(r"[^A-Za-z0-9._-]", "_", str(name or "").strip()) or fallback
    return n[:80]


async def _send_v2_with_files(channel, components_v2, files, allowed_mentions=None, buttons=None):
    """Send a Components-V2 message with uploaded files embedded INSIDE it — each
    as a labelled File component (type 13) or, for images, a Media Gallery (type
    12) — sent as multipart with the real attachments. `files` = [{label, url,
    filename}]. Returns the message id, or False so the caller can fall back."""
    guild = getattr(channel, "guild", None)
    built = [b for b in (_build_v2(c, guild) for c in components_v2) if b]
    attachments = []
    dfiles = []
    extra = []
    for i, f in enumerate(files or []):
        try:
            async with _http() as client:
                r = await client.get(f["url"], timeout=90, follow_redirects=True)
                if r.status_code != 200:
                    print(f"[Form] file fetch HTTP {r.status_code}")
                    continue
                blob = r.content
        except Exception as e:
            print(f"[Form] file fetch failed: {e}")
            continue
        fname = _san_filename(f.get("filename"), f"file{i}")
        label = _clean_label(f.get("label") or "File")
        extra.append({"type": 10, "content": f"**{label}**"})
        if _is_image_name(fname):
            extra.append({"type": 12, "items": [{"media": {"url": f"attachment://{fname}"}}]})
        else:
            extra.append({"type": 13, "file": {"url": f"attachment://{fname}"}})
        attachments.append({"id": i, "filename": fname})
        dfiles.append(discord.File(io.BytesIO(blob), filename=fname))
    if not dfiles:
        return False
    built = built + extra
    ALLOWED_TOP = {1, 9, 10, 12, 13, 14, 17}
    if not {c.get("type") for c in built}.issubset(ALLOWED_TOP):
        built = [{"type": 17, "components": built}]
    if buttons:
        built.append({"type": 1, "components": list(buttons)})
    payload = {"components": built, "flags": 1 << 15, "attachments": attachments}
    if allowed_mentions is not None:
        payload["allowed_mentions"] = allowed_mentions
    form = [{"name": "payload_json", "value": json.dumps(payload)}]
    for index, fobj in enumerate(dfiles):
        form.append({"name": f"files[{index}]", "value": fobj.fp,
                     "filename": fobj.filename, "content_type": "application/octet-stream"})
    route = discord.http.Route("POST", "/channels/{channel_id}/messages", channel_id=channel.id)
    try:
        resp = await bot.http.request(route, form=form, files=dfiles)
        return str(resp["id"]) if isinstance(resp, dict) and resp.get("id") else True
    except Exception as e:
        print(f"[Form] V2+files send failed: {e}")
        return False


async def _post_form_files_thread(channel, opening_message_id, files, thread_name="References", label=True):
    """Post the uploaded form files into a THREAD off the ticket's opening
    message (falls back to a standalone thread, then to the channel itself).
    label=False posts the files bare (no field header)."""
    name = (thread_name or "References")[:100]
    thread = None
    if opening_message_id:
        try:
            msg = await channel.fetch_message(int(opening_message_id))
            thread = await msg.create_thread(name=name, auto_archive_duration=10080)
        except Exception as e:
            print(f"[Form] thread-from-message failed: {e}")
    if thread is None:
        try:
            thread = await channel.create_thread(name=name, type=discord.ChannelType.public_thread, auto_archive_duration=10080)
        except Exception as e:
            print(f"[Form] standalone thread failed: {e}")
    if thread is None:
        await _post_form_files(channel, files, label=label)  # last resort: post in the channel
        return None
    await _post_form_files(thread, files, label=label)
    return thread


async def _form_fields_for(key):
    """The form design's fields (text + file), source depending on the form kind."""
    open_comps = (form_log_configs[key]["components"] if key in form_log_configs else form_msgs.get(key)) or []
    return _parse_form_fields(open_comps, limit=FORM_MAX_QUESTIONS)


async def _open_form_page(interaction, key, page):
    """Open the modal for one page (up to 5 fields) of a form. Fields may be text
    inputs ({Question:}) or file uploads ({File:}). Called as the response to the
    Form button (page 0) or a 'Continue' button (later pages)."""
    fields = await _form_fields_for(key)
    start = page * FORM_PAGE_SIZE
    page_fields = fields[start:start + FORM_PAGE_SIZE]
    if not page_fields:
        return
    total_pages = (len(fields) + FORM_PAGE_SIZE - 1) // FORM_PAGE_SIZE
    components = []
    for j, f in enumerate(page_fields):
        idx = start + j
        label = (_clean_label(f["label"]) or f["label"])[:45]
        if f["kind"] == "file":
            components.append({
                "type": 18, "label": label,
                "component": {"type": 19, "custom_id": f"f{idx}", "min_values": 1, "max_values": 10},
            })
        else:
            style = 2 if f.get("long") else _form_input_style(f["label"])
            components.append({
                "type": 18, "label": label,
                "component": {"type": 4, "custom_id": f"q{idx}", "style": style,
                              "required": True, "max_length": 1000},
            })
    title = (form_titles.get(key) or "Application")
    if total_pages > 1:
        title = f"{title} ({page + 1}/{total_pages})"
    data = {"title": title[:45], "custom_id": f"ticketform:{key}|{page}", "components": components}
    route = discord.http.Route(
        "POST", "/interactions/{interaction_id}/{interaction_token}/callback",
        interaction_id=interaction.id, interaction_token=interaction.token,
    )
    await bot.http.request(route, json={"type": 9, "data": data})


def _form_input_style(label):
    l = (label or "").lower()
    if any(k in l for k in ("descri", "about", "why", "reason", "detail", "explain", "tell", "message")):
        return 2  # paragraph
    return 1  # short


def _modal_values(components):
    """Flatten a modal_submit tree into {custom_id: value}, handling both text
    inputs (value) and selects (values[0])."""
    out = {}
    for row in components or []:
        if not isinstance(row, dict):
            continue
        inner = row.get("component")
        cands = [inner] if isinstance(inner, dict) else []
        cands += [c for c in (row.get("components") or []) if isinstance(c, dict)]
        for c in cands:
            cid = c.get("custom_id")
            if not cid:
                continue
            if "values" in c:
                vals = c.get("values") or []
                out[cid] = vals[0] if vals else ""
            else:
                out[cid] = c.get("value", "") or ""
    return out


def _collect_modal_values(components):
    """Flatten a modal_submit component tree into {custom_id: value}. Handles
    Label-wrapped inputs (type 18 -> component), action rows (type 1), and bare
    text inputs (type 4)."""
    vals = {}
    for row in components or []:
        if not isinstance(row, dict):
            continue
        inner = row.get("component")
        if isinstance(inner, dict) and inner.get("custom_id"):
            # A select inside a Label reports its picks under `values`.
            vals[inner["custom_id"]] = inner.get("value") or ", ".join(str(v) for v in (inner.get("values") or [])) or ""
        for c in (row.get("components") or []):
            if isinstance(c, dict) and c.get("custom_id"):
                vals[c["custom_id"]] = c.get("value") or ", ".join(str(v) for v in (c.get("values") or [])) or ""
        if row.get("type") == 4 and row.get("custom_id"):
            vals[row["custom_id"]] = row.get("value", "") or ""
    return vals


def _modal_uploaded_files(interaction, custom_id):
    """Return [{url, filename}] for each file uploaded in the modal's file-upload
    component (type 19) with this custom_id. Discord lists the attachment ids in
    the component's `values`; the file objects live under data.resolved.attachments."""
    data = interaction.data or {}
    resolved = ((data.get("resolved") or {}).get("attachments")) or {}
    files = []

    def collect(c):
        if isinstance(c, dict) and c.get("custom_id") == custom_id:
            for aid in (c.get("values") or []):
                att = resolved.get(str(aid)) or {}
                if att.get("url"):
                    files.append({"url": att["url"], "filename": att.get("filename")})

    for row in (data.get("components") or []):
        if not isinstance(row, dict):
            continue
        collect(row)
        collect(row.get("component"))
        for c in (row.get("components") or []):
            collect(c)
    return files


def _apply_answers(open_comps, mapping):
    """Replace each {Question: LABEL} token with '**LABEL** answer', and each
    {File: LABEL} token with '**LABEL**' (the file itself is posted separately)."""
    raw = json.dumps(open_comps or [])

    def repl(m):
        kind = m.group(1).lower()
        label = (m.group(2) or "").strip()
        clean = _clean_label(label)
        if kind in ("file", "sfile"):
            out = f"**{clean}**"
        else:
            answer = mapping.get(label, "")
            out = f"**{clean}** {answer}".strip() if answer else f"**{clean}**"
        return json.dumps(out)[1:-1]  # JSON-escape (we're inside a string literal)

    return json.loads(_FIELD_RE.sub(repl, raw))


# ===================== Prompt forms (suggestions / feedback / report-bug) =====================
# A generic "run a slash command -> fill a form -> post the designed message to a
# configured channel" engine. The admin designs ONE Components-V2 message in the
# dashboard whose text embeds tokens; the tokens define both the modal inputs and
# where the answers land in the posted message. Supported tokens:
#   {user}                             -> the submitter's mention (no input)
#   {member: Label}                    -> pick one server member (renders their mention)
#   {question: Label}                  -> short text input
#   {long question: Label} / {LQuestion: Label} -> paragraph text input
#   {drop down: Name Opt1 Opt2 ...}    -> select menu (first word = name;
#                                          options may be space- or comma-separated)
#   {file: Name}                       -> file upload (attached to the posted message)
# custom_id namespace for the modal is "pf:<feature>".
prompt_forms_config = {}  # feature -> {"design": [...], "channel_id": "...", "title": "..."}
# Answers collected so far for a multi-form submission, keyed by (feature, uid):
# {"design", "title", "channel_id", "answers": {gidx: str}, "files": {gidx: [file]}}
_pf_pending = {}

# A trailing number groups a token into a form "page": {Question:} / {File:} are
# form 1; {Question2:} / {File2:} are form 2, and so on. Discord caps a modal at 5
# inputs, so >5 questions are split across forms shown one after another.
_PF_TOKEN_RE = re.compile(
    r"\{(user|members?|member\s*select|long\s*question|lquestion|drop\s*down|dropdown|select|question|file)\s*(\d*)\s*(?::\s*(.*?))?\}",
    re.IGNORECASE,
)


def _pf_norm_kind(raw_kind):
    k = (raw_kind or "").lower().replace(" ", "")
    if k in ("longquestion", "lquestion"):
        return "lquestion"
    if k in ("dropdown", "select"):
        return "dropdown"
    if k in ("member", "members", "memberselect"):
        return "member"
    return k  # user | question | file


def _pf_tokens(design):
    """Every token in a design, in document order: [{kind, content, form}].
    `form` is the group number (1 unless the token has a trailing number)."""
    raw = json.dumps(design or [])
    toks = []
    for m in _PF_TOKEN_RE.finditer(raw):
        num = m.group(2)
        toks.append({
            "kind": _pf_norm_kind(m.group(1)),
            "content": (m.group(3) or "").strip(),
            "form": int(num) if num else 1,
        })
    return toks


def _pf_inputs(design):
    """Every token that needs a modal input (all except {user}), across all
    forms — used to tell whether the design collects anything at all."""
    return [t for t in _pf_tokens(design) if t["kind"] != "user"]


def _pf_forms(design):
    """Ordered {form_number: [(global_input_index, token), ...]}. The global
    index is the token's position among ALL inputs in document order, so the
    collected answers line up with _pf_render's in-order substitution. Each form
    is capped at Discord's 5-input limit."""
    forms = {}
    for gidx, t in enumerate(_pf_inputs(design)):
        forms.setdefault(int(t.get("form") or 1), []).append((gidx, t))
    return {n: forms[n][:5] for n in sorted(forms)}


def _pf_form_numbers(design):
    return list(_pf_forms(design).keys())


def _pf_dropdown_parts(content):
    """'Priority Low Medium Urgent' -> ('Priority', ['Low','Medium','Urgent']).
    The first whitespace-separated token is the name; the rest are options.
    Options may be separated by spaces AND/OR commas, so 'Rating: 1, 2, 3, 4, 5'
    yields clean 1..5 (no trailing commas). De-duplicated (Discord rejects a
    select with repeated values)."""
    toks = [p for p in re.split(r"\s+", (content or "").strip()) if p]
    if not toks:
        return ("Choose", ["Yes", "No"])
    name = toks[0].strip(":,").strip()
    opts = []
    for p in toks[1:]:
        for o in re.split(r",+", p):
            o = o.strip(":,").strip()
            if o:
                opts.append(o)
    opts = list(dict.fromkeys(opts))  # de-dupe, keep order
    return (name or "Choose", opts or ["Yes", "No"])


# A dropdown named "Rating" with numeric options renders the picked number as
# this custom emoji repeated N times plus "(N/max)" — e.g. ✔✔✔✔✔ (5/5).
RATING_EMOJI = "<:rating:1457205320056049665>"


def _pf_render_dropdown(content, val):
    """How a dropdown answer shows in the posted message. A "Rating" dropdown
    with numeric options becomes the rating emoji repeated + (N/max); anything
    else shows the picked value verbatim."""
    name, opts = _pf_dropdown_parts(content or "")
    if _clean_label(name).strip().lower() == "rating" and str(val).strip().isdigit():
        n = int(str(val).strip())
        nums = [int(o) for o in opts if str(o).strip().isdigit()]
        mx = max(nums) if nums else n
        n = max(0, min(n, mx))
        return (RATING_EMOJI * n) + f" ({n}/{mx})"
    return val


def _pf_render(design, uid, answers):
    """Substitute each token with its collected answer (or the submitter mention
    for {user}) and return the resulting Components-V2 tree."""
    raw = json.dumps(design or [])
    state = {"i": 0}

    def repl(m):
        kind = _pf_norm_kind(m.group(1))
        if kind == "user":
            out = f"<@{uid}>"
        else:
            val = answers[state["i"]] if state["i"] < len(answers) else ""
            state["i"] += 1
            if kind == "member":
                # A member pick collects the chosen user's id — render as a mention.
                out = f"<@{val}>" if val else ""
            elif kind == "dropdown":
                out = _pf_render_dropdown(m.group(3), val)
            else:
                out = val
        return json.dumps(str(out))[1:-1]

    return json.loads(_PF_TOKEN_RE.sub(repl, raw))


def _pf_prefill_member(design, text):
    """Replace every {member:...} token in a design with literal `text`, so that
    field is no longer collected in the modal. Used when we already know the
    subject — e.g. a receipt's "Leave a Review" fills the Designer line with the
    package they bought instead of asking them to pick a member."""
    raw = json.dumps(design or [])
    esc = json.dumps(str(text))[1:-1]

    def repl(m):
        if _pf_norm_kind(m.group(1)) == "member":
            return esc
        return m.group(0)

    return json.loads(_PF_TOKEN_RE.sub(repl, raw))


def _pf_strip_file_lines(design):
    """Remove any "**Label:** {File:…}" segment (with its leading newline) from a
    design, so an uploaded file leaves no text on the posted message — the upload
    lives in a thread instead."""
    raw = json.dumps(design or [])
    raw = re.sub(r'(?:\\n)?[^"\\{}]*\{\s*file\s*\d*\s*(?::\s*[^{}]*?)?\s*\}', "", raw, flags=re.IGNORECASE)
    return json.loads(raw)


async def _pf_open_modal(interaction, feature, design, title, form_num=None):
    forms = _pf_forms(design)
    if form_num is None:
        form_num = next(iter(forms), 1)
    entries = forms.get(form_num, [])
    total_forms = len(forms)
    # Show "(1/2)" etc. in the title when there's more than one page.
    ttl = title or "Submit"
    if total_forms > 1:
        page = list(forms).index(form_num) + 1 if form_num in forms else 1
        ttl = f"{ttl} ({page}/{total_forms})"
    components = []
    for gidx, t in entries:
        cid = f"p{gidx}"
        if t["kind"] == "file":
            label = _clean_label(t["content"]) or "File"
            # Optional: a submitter may not have a file to attach. Discord defaults
            # `required` to True, and required + min_values:0 is rejected — so make
            # it explicitly not required to match min_values:0.
            components.append({"type": 18, "label": label[:45],
                               "component": {"type": 19, "custom_id": cid, "required": False,
                                             "min_values": 0, "max_values": 5}})
        elif t["kind"] == "member":
            # A user-select inside the modal — the submitter picks one member;
            # it renders as that member's mention in the posted message.
            components.append({"type": 18, "label": (_clean_label(t["content"]) or "Member")[:45],
                               "component": {"type": 5, "custom_id": cid, "min_values": 1,
                                             "max_values": 1}})
        elif t["kind"] == "dropdown":
            name, opts = _pf_dropdown_parts(t["content"])
            seen, options = set(), []
            for o in opts:
                v = o[:100]
                if v and v not in seen:
                    seen.add(v)
                    options.append({"label": v, "value": v})
                if len(options) >= 25:
                    break
            components.append({"type": 18, "label": (_clean_label(name) or "Choose")[:45],
                               "component": {"type": 3, "custom_id": cid, "min_values": 1,
                                             "max_values": 1, "required": True, "options": options}})
        else:
            style = 2 if t["kind"] == "lquestion" else _form_input_style(t["content"])
            components.append({"type": 18, "label": (_clean_label(t["content"]) or "Answer")[:45],
                               "component": {"type": 4, "custom_id": cid, "style": style,
                                             "required": True, "max_length": 1000}})
    data = {"title": ttl[:45], "custom_id": f"pf:{feature}:{form_num}", "components": components}
    route = discord.http.Route(
        "POST", "/interactions/{interaction_id}/{interaction_token}/callback",
        interaction_id=interaction.id, interaction_token=interaction.token,
    )
    try:
        await bot.http.request(route, json={"type": 9, "data": data})
    except Exception as e:
        print(f"[PromptForm] modal open failed for {feature}: {e}")
        try:
            await interaction.response.send_message(
                "Couldn't open the form right now — please try again in a moment. "
                "If it keeps happening, an admin should re-check the form design.",
                ephemeral=True)
        except Exception:
            pass


_PF_TITLES = {
    "customs-suggestions": "Suggestion",
    "customs-feedback": "Feedback",
    "customs-reportbug": "Report a Bug",
}


def _pf_config_for(feature):
    return prompt_forms_config.get(feature) or {}


# /suggestion is its own feature (the Suggestions block), NOT linked to the
# website "Custom feature" form — those are separate. No prompt-form command
# falls back to a global Extras setting anymore.
_PF_PLATFORM_KEY = {}
_PF_BUILTIN_DESIGN = {
    "customs-suggestions": [{"type": "text", "text": (
        f"## {BRAND} | Custom Feature\n**User:** {{user}}\n"
        "{Question: **Feature Title:**}\n{Question: **Description:**}\n{File: **Example:**}")}],
    "customs-reportbug": [{"type": "text", "text": (
        f"## {BRAND} | Bug Report\n**User:** {{user}}\n"
        "{Question: **What happened:**}\n{Question: **Steps to reproduce:**}\n{File: **Screenshot:**}")}],
}
# The dashboard's Custom Feature / Report a Bug editor uses value tokens
# ({title}, {description}, {example}, …). Map each to a prompt-form token so the
# same engine asks that question and renders the OWNER'S design with the answer
# filled in where the token was. {user} is left as-is (rendered as the mention).
_EXTRAS_TOKEN_MAP = {
    "customs-suggestions": {
        "title": "{question: Feature Title}",
        "description": "{long question: Description}",
        "example": "{file: Example}",
    },
    "customs-reportbug": {
        "title": "{question: Bug Title}",
        "description": "{long question: What happened}",
        "steps": "{long question: Steps to reproduce}",
        "priority": "{question: Priority}",
        "proof": "{file: Proof}",
        "screenshot": "{file: Screenshot}",
    },
}


def _extras_design_to_pf(feature, design):
    """Convert an Extras design's value tokens into prompt-form tokens so the
    form engine can drive it. Returns the converted design (owner's layout)."""
    mapping = _EXTRAS_TOKEN_MAP.get(feature) or {}
    raw = json.dumps(design or [])
    for tok, pf in mapping.items():
        raw = re.sub(r"\{\s*" + re.escape(tok) + r"\s*\}", lambda _m, _pf=pf: _pf,
                     raw, flags=re.IGNORECASE)
    try:
        return json.loads(raw)
    except Exception:
        return design or []


async def _platform_setting_get(key):
    """Read one global platform_settings row (its `value` jsonb) via REST.
    Returns {} on any failure. Lets the bot pick up the owner-only Extras config
    (Custom Feature / Report a Bug channel) the dashboard stores globally."""
    if not (SUPABASE_URL and SUPABASE_KEY):
        return {}
    try:
        url = f"{SUPABASE_URL}/rest/v1/platform_settings?key=eq.{key}&select=value"
        headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
        async with _http() as client:
            r = await client.get(url, headers=headers, timeout=15)
        if r.status_code == 200:
            rows = r.json()
            if rows and isinstance(rows[0].get("value"), dict):
                return rows[0]["value"]
        else:
            print(f"[PromptForm] platform setting '{key}' — HTTP {r.status_code}")
    except Exception as e:
        print(f"[PromptForm] platform setting '{key}' read failed: {e}")
    return {}


async def _pf_platform_fallback(feature):
    """When a prompt form has no per-bot dashboard config, use the global Extras
    setting: the owner-picked channel + a built-in form design. Populates
    prompt_forms_config so the rest of the flow is unchanged. Returns True if a
    usable channel was found."""
    key = _PF_PLATFORM_KEY.get(feature)
    if not key:
        return False
    val = await _platform_setting_get(key)
    ch = str(val.get("channel_id") or "")
    if not ch:
        return False
    # Use the OWNER'S designed message (converted to form tokens); only fall back
    # to the built-in template if they haven't designed one / it has no fields.
    design = None
    components = val.get("components")
    if isinstance(components, list) and components:
        converted = _extras_design_to_pf(feature, components)
        if _pf_inputs(converted):
            design = converted
    if design is None:
        design = [dict(c) for c in (_PF_BUILTIN_DESIGN.get(feature) or [])]
    prompt_forms_config[feature] = {
        "design": design,
        "channel_id": ch,
        "title": _PF_TITLES.get(feature) or "Submit",
    }
    return True


async def _pf_command(interaction, feature):
    """Slash-command entry point: open the form, or (if the design has no input
    tokens) post the designed message straight away."""
    cfg = _pf_config_for(feature)
    deferred = False
    # If it isn't in memory (the bot may not be applying config live), ACK first
    # so we don't blow Discord's 3s window, then pull the saved config on demand.
    if not cfg.get("channel_id") or not cfg.get("design"):
        try:
            await interaction.response.defer(ephemeral=True)
            deferred = True
        except Exception:
            pass
        try:
            fresh = await fetch_config(feature)
            if fresh:
                await apply_config(feature, fresh)
                cfg = _pf_config_for(feature)
        except Exception as e:
            print(f"[PromptForm] on-demand config refresh failed for {feature}: {e}")
        # Still nothing? Fall back to the global Extras channel (Custom Feature /
        # Report a Bug) so the command works without a per-bot dashboard save.
        if not cfg.get("channel_id") or not cfg.get("design"):
            try:
                if await _pf_platform_fallback(feature):
                    cfg = _pf_config_for(feature)
            except Exception as e:
                print(f"[PromptForm] platform fallback failed for {feature}: {e}")

    channel_id = cfg.get("channel_id")
    design = cfg.get("design") or []

    # Specific diagnostics so it's clear which piece is missing.
    msg = None
    if not channel_id and not design:
        msg = "This isn't set up yet — add a message and pick a channel in the dashboard, then Save."
    elif not channel_id:
        msg = "Almost there — no destination **channel** is set. Pick one in the dashboard and Save."
    elif not design:
        msg = ("Almost there — the **message is empty**. Add it (with the `{Question:}` / `{File:}` "
               "tokens) in the dashboard and Save.")
    if msg:
        if deferred:
            return await interaction.followup.send(msg, ephemeral=True)
        return await interaction.response.send_message(msg, ephemeral=True)

    title = cfg.get("title") or _PF_TITLES.get(feature) or "Submit"
    inputs = _pf_inputs(design)

    # We can only open a modal as the FIRST response — if we already deferred to
    # load the config, ask the user to run it once more (now it's in memory).
    if deferred and inputs:
        return await interaction.followup.send(
            "Loaded your latest setup — run the command once more to open the form.", ephemeral=True)

    if not inputs:
        ch = await resolve_channel(channel_id)
        if ch:
            await send_v2_message(ch, _pf_render(design, interaction.user.id, []),
                                  allowed_mentions={"parse": []})
        text = "Submitted. Our team will look into it!"
        return await (interaction.followup.send(text, ephemeral=True) if deferred
                      else interaction.response.send_message(text, ephemeral=True))

    # Start a fresh multi-form session and open the first page.
    _pf_pending[(feature, interaction.user.id)] = {
        "design": design, "title": title, "channel_id": channel_id,
        "answers": {}, "files": {},
    }
    first = next(iter(_pf_forms(design)), 1)
    await _pf_open_modal(interaction, feature, design, title, first)


class _PFContinueView(discord.ui.View):
    """The button shown between form pages — Discord can't open a modal straight
    from a modal submit, so the user clicks Continue to get the next page."""
    def __init__(self, feature, next_form):
        super().__init__(timeout=900)
        self.feature = feature
        self.next_form = next_form

    @discord.ui.button(label="Continue", style=discord.ButtonStyle.primary)
    async def _cont(self, interaction: discord.Interaction, _button: discord.ui.Button):
        pend = _pf_pending.get((self.feature, interaction.user.id))
        if not pend:
            return await interaction.response.send_message(
                "This form expired — run the command again.", ephemeral=True)
        await _pf_open_modal(interaction, self.feature, pend["design"], pend["title"], self.next_form)


async def _pf_submit(interaction, feature, form_num=1):
    key = (feature, interaction.user.id)
    pend = _pf_pending.get(key)
    cfg = _pf_config_for(feature)
    # Recover a session lost to a restart from the live config.
    if not pend:
        design = cfg.get("design") or []
        pend = {"design": design, "title": cfg.get("title") or _PF_TITLES.get(feature) or "Submit",
                "channel_id": cfg.get("channel_id"), "answers": {}, "files": {}}
        _pf_pending[key] = pend
    design = pend["design"]
    forms = _pf_forms(design)
    vals = _modal_values(interaction.data.get("components"))
    # Record this page's answers against their global indices.
    for gidx, t in forms.get(form_num, []):
        cid = f"p{gidx}"
        if t["kind"] == "file":
            fs = _modal_uploaded_files(interaction, cid)
            pend["files"][gidx] = fs
            pend["answers"][gidx] = ", ".join((f.get("filename") or "file") for f in fs) if fs else ""
        else:
            pend["answers"][gidx] = vals.get(cid, "")

    form_nums = list(forms)
    remaining = [n for n in form_nums if form_nums.index(n) > form_nums.index(form_num)]
    if remaining:
        nxt = remaining[0]
        page = form_nums.index(nxt) + 1
        return await interaction.response.send_message(
            f"Saved. Click **Continue** for the rest ({page}/{len(form_nums)}).",
            view=_PFContinueView(feature, nxt), ephemeral=True)

    # Last page — assemble every answer in document order and post.
    _pf_pending.pop(key, None)
    total = len(_pf_inputs(design))
    answers = [pend["answers"].get(i, "") for i in range(total)]
    files = []
    for i in range(total):
        files.extend(pend["files"].get(i, []))
    ch = await resolve_channel(pend.get("channel_id"))

    # Blacklist: fill the auto tokens (nickname / @ / Roblox link) from the
    # chosen member. The Roblox lookup hits the network, so defer first.
    member_id = pend.get("blacklist_member_id")
    if member_id:
        try:
            await interaction.response.defer(ephemeral=True)
        except Exception:
            pass
        member = interaction.guild.get_member(int(member_id)) if interaction.guild else None
        rid = await _bl_roblox_id(member) if member else ""
        roblox_url = _bl_roblox_url_from_id(rid)
        if rid and member:
            _bl_mark_roblox(member.guild.id, rid, member.id)
            await _bl_save_saved()
        design = _bl_auto_fill(design, member, roblox_url)
        if not ch:
            return await interaction.followup.send("Couldn't find the blacklist channel.", ephemeral=True)
        await send_v2_message(ch, _pf_render(design, interaction.user.id, answers),
                              allowed_mentions={"parse": []})
        if files:
            try:
                await _post_form_files(ch, files)
            except Exception as e:
                print(f"[PromptForm] file post failed: {e}")
        extra = await _bl_apply_punishment(member)
        return await interaction.followup.send(f"Blacklist entry logged{extra}.", ephemeral=True)

    if not ch:
        return await interaction.response.send_message("Couldn't find the destination channel.", ephemeral=True)
    await interaction.response.send_message("Submitted. Our team will look into it!", ephemeral=True)

    # An uploaded file goes into a THREAD off the message with no text on the
    # message itself: strip the {File:} line and drop its answer slot, so the
    # remaining answers still line up, then post the file bare in the thread.
    toks = _pf_inputs(design)
    answers_nofile, file_label = [], "Attachment"
    for i, t in enumerate(toks):
        if t["kind"] == "file":
            if t.get("content"):
                file_label = _clean_label(t["content"]) or file_label
        else:
            answers_nofile.append(pend["answers"].get(i, ""))
    out = _pf_render(_pf_strip_file_lines(design), interaction.user.id, answers_nofile)
    mid = await send_v2_message(ch, out, allowed_mentions={"parse": []})
    if files:
        try:
            await _post_form_files_thread(
                ch, mid if isinstance(mid, str) else None, files, file_label, label=False,
            )
        except Exception as e:
            print(f"[PromptForm] file thread post failed: {e}")


@bot.tree.command(name="suggestion", description="Sends a suggestion to the team.")
async def suggestion_cmd(interaction: discord.Interaction):
    await _pf_command(interaction, "customs-suggestions")


# ===================== Shifts (staff activity) =====================
# /shift manage opens a personal panel (start, end, break), /shift leaderboard
# ranks time on shift, /shift online lists who is on now. Every message comes
# from the dashboard Shifts block as a template with tokens. Shifts are
# persisted (shift-data) so a redeploy never loses an open shift.
SHIFT_DEFAULT_MANAGE = ("## Shift panel\n{status}\n\n**Total shift time:** {total_time}\n"
                        "**Total break time:** {break_time}\n**Activity quota:** {quota}\n"
                        "**Shifts this week:** {shifts}\n\n**Recent shifts**\n{recent}")
SHIFT_DEFAULT_LEADERBOARD = "## Shift leaderboard, {period}\n{leaderboard}"
SHIFT_DEFAULT_ONLINE = "## Staff on shift, {count}\n{online}"
shift_config = {
    "staff_role_ids": [], "onshift_role_ids": [], "quota_hours": 0.0, "log_channel_id": "",
    "manage_message": SHIFT_DEFAULT_MANAGE, "leaderboard_message": SHIFT_DEFAULT_LEADERBOARD,
    "online_message": SHIFT_DEFAULT_ONLINE,
}
shift_data = {}   # guild_id -> {"active": {uid: {"start", "break_start", "break_total"}}, "history": {uid: [...]}}
_shift_loaded = False
SHIFT_MAX_SECONDS = 12 * 3600      # a forgotten shift ends itself after this long
SHIFT_HISTORY_KEEP = 300           # shifts kept per person


async def _shift_load():
    global _shift_loaded
    ok, cfg = await _durable_config_get("shift-data")
    if not ok:
        print("[Shift] load failed, shifts disabled this session.")
        return
    g = (cfg or {}).get("guilds")
    if isinstance(g, dict):
        for gid, d in g.items():
            if isinstance(d, dict):
                shift_data[str(gid)] = {
                    "active": {str(u): v for u, v in (d.get("active") or {}).items() if isinstance(v, dict)},
                    "history": {str(u): [x for x in (lst or []) if isinstance(x, dict)]
                                for u, lst in (d.get("history") or {}).items()},
                }
    _shift_loaded = True
    print(f"[Shift] loaded, {sum(len(d['active']) for d in shift_data.values())} shift(s) open")


async def _shift_save():
    if not _shift_loaded:
        return
    try:
        await _bot_config_upsert("shift-data", {"guilds": shift_data})
    except Exception as e:
        print(f"[Shift] save failed: {e}")


def _shift_guild(gid):
    return shift_data.setdefault(str(gid), {"active": {}, "history": {}})


def _shift_week_start():
    """Monday 00:00 Central, as a unix timestamp. The quota and the weekly
    leaderboard both count from here."""
    central = datetime.timezone(datetime.timedelta(hours=-5))
    now = datetime.datetime.now(central)
    start = (now - datetime.timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return start.timestamp()


def _fmt_dur(secs):
    secs = int(max(0, secs))
    h, m = secs // 3600, (secs % 3600) // 60
    if h and m:
        return f"{h} hour{'s' if h != 1 else ''} {m} minute{'s' if m != 1 else ''}"
    if h:
        return f"{h} hour{'s' if h != 1 else ''}"
    if m:
        return f"{m} minute{'s' if m != 1 else ''}"
    return f"{secs} second{'s' if secs != 1 else ''}"


def _shift_active_elapsed(a, now=None):
    """(worked_seconds, break_seconds) for an open shift."""
    now = now or time.time()
    brk = float(a.get("break_total") or 0)
    if a.get("break_start"):
        brk += now - float(a["break_start"])
    return max(0.0, now - float(a["start"]) - brk), brk


def _shift_totals(gid, uid, since_ts=0):
    """(worked, break, count) for one person since a time, open shift included."""
    g = _shift_guild(gid)
    worked = brk = 0.0
    count = 0
    for h in g["history"].get(str(uid), []):
        if float(h.get("end") or 0) >= since_ts:
            worked += float(h.get("duration") or 0)
            brk += float(h.get("break") or 0)
            count += 1
    a = g["active"].get(str(uid))
    if a:
        w, b = _shift_active_elapsed(a)
        worked += w
        brk += b
        count += 1
    return worked, brk, count


def _shift_can_use(member):
    roles = shift_config.get("staff_role_ids") or []
    if not roles:
        return True
    return has_any_role(member, roles)


def _shift_status_text(a):
    if not a:
        return "You're offline."
    if a.get("break_start"):
        return f"You're on break, on shift since <t:{int(float(a['start']))}:t>."
    return f"You're on shift since <t:{int(float(a['start']))}:t>."


def _shift_manage_mapping(member):
    g = _shift_guild(member.guild.id)
    a = g["active"].get(str(member.id))
    week = _shift_week_start()
    worked, brk, count = _shift_totals(member.guild.id, member.id, week)
    quota_h = float(shift_config.get("quota_hours") or 0)
    if quota_h > 0:
        pct = int(round(worked / (quota_h * 3600) * 100))
        quota = f"{pct}% of {_fmt_dur(quota_h * 3600)}, " + ("met" if pct >= 100 else "not met yet")
    else:
        quota = "No quota set"
    recent = []
    hist = sorted(g["history"].get(str(member.id), []), key=lambda h: float(h.get("end") or 0), reverse=True)[:3]
    for h in hist:
        recent.append(f"<t:{int(float(h.get('start') or 0))}:t> to <t:{int(float(h.get('end') or 0))}:t>, {_fmt_dur(h.get('duration') or 0)}")
    return {
        "user": member.mention, "status": _shift_status_text(a), "total_time": _fmt_dur(worked),
        "break_time": _fmt_dur(brk), "quota": quota, "shifts": str(count),
        "recent": "\n".join(recent) if recent else "No shifts yet this week.",
        "week_start": f"<t:{int(week)}:D>",
    }


def _shift_fill(template, mapping, guild):
    out = str(template or "")
    for k, v in mapping.items():
        out = out.replace("{" + k + "}", str(v))
    return _render_guild_text(out, guild)


def _shift_buttons(a):
    if not a:
        return [{"type": 2, "style": 3, "custom_id": "shift_start", "label": "Start shift"}]
    if a.get("break_start"):
        return [{"type": 2, "style": 1, "custom_id": "shift_unbreak", "label": "End break"},
                {"type": 2, "style": 4, "custom_id": "shift_end", "label": "End shift"}]
    return [{"type": 2, "style": 2, "custom_id": "shift_break", "label": "Start break"},
            {"type": 2, "style": 4, "custom_id": "shift_end", "label": "End shift"},
            {"type": 2, "style": 2, "custom_id": "shift_refresh", "label": "Refresh"}]


async def _shift_respond(interaction, text, buttons=None, update=False, ephemeral=True):
    """Answer a slash command (type 4) or update the clicked panel (type 7)
    with one Components V2 container."""
    built = [b for b in (_build_v2(c, interaction.guild) for c in [{"type": "container", "children": [{"type": "text", "text": text}]}]) if b]
    if buttons:
        built.append({"type": 1, "components": buttons})
    flags = 1 << 15
    if ephemeral:
        flags |= 1 << 6
    data = {"flags": flags, "components": built, "allowed_mentions": {"parse": []}}
    route = discord.http.Route("POST", "/interactions/{interaction_id}/{interaction_token}/callback",
                               interaction_id=interaction.id, interaction_token=interaction.token)
    await bot.http.request(route, json={"type": 7 if update else 4, "data": data})


async def _shift_panel(interaction, update=False):
    member = interaction.user
    a = _shift_guild(member.guild.id)["active"].get(str(member.id))
    text = _shift_fill(shift_config.get("manage_message") or SHIFT_DEFAULT_MANAGE, _shift_manage_mapping(member), member.guild)
    await _shift_respond(interaction, text, buttons=_shift_buttons(a), update=update)


async def _shift_roles(member, give):
    ids = [int(r) for r in (shift_config.get("onshift_role_ids") or []) if str(r).isdigit()]
    roles = [member.guild.get_role(i) for i in ids]
    roles = [r for r in roles if r]
    if not roles:
        return
    try:
        if give:
            await member.add_roles(*roles, reason="On shift")
        else:
            await member.remove_roles(*roles, reason="Shift ended")
    except Exception as e:
        print(f"[Shift] role change failed for {member.id}: {e}")


async def _shift_log(guild, text):
    ch = await resolve_channel(shift_config.get("log_channel_id"))
    if not ch:
        return
    try:
        await send_v2_message(ch, [{"type": "container", "children": [{"type": "text", "text": text}]}],
                              allowed_mentions={"parse": []})
    except Exception as e:
        print(f"[Shift] log failed: {e}")


async def _shift_end_for(guild, uid, reason=""):
    """Close a shift, record it, drop the roles. Returns the record or None."""
    g = _shift_guild(guild.id)
    a = g["active"].pop(str(uid), None)
    if not a:
        return None
    now = time.time()
    worked, brk = _shift_active_elapsed(a, now)
    rec = {"start": float(a["start"]), "end": now, "duration": worked, "break": brk}
    hist = g["history"].setdefault(str(uid), [])
    hist.append(rec)
    if len(hist) > SHIFT_HISTORY_KEEP:
        del hist[:-SHIFT_HISTORY_KEEP]
    member = guild.get_member(int(uid))
    if member:
        await _shift_roles(member, False)
    await _shift_save()
    who = member.mention if member else f"<@{uid}>"
    await _shift_log(guild, f"{who} ended a shift, {_fmt_dur(worked)}" + (f", {reason}" if reason else "") + ".")
    return rec


async def _shift_button(interaction, cid):
    member = interaction.user
    if not isinstance(member, discord.Member) or not _shift_can_use(member):
        return await interaction.response.send_message(embed=error_embed("Staff only", "You don't have a role that can use shifts."), ephemeral=True)
    g = _shift_guild(member.guild.id)
    a = g["active"].get(str(member.id))
    now = time.time()
    if cid == "shift_start":
        if not a:
            g["active"][str(member.id)] = {"start": now, "break_start": 0, "break_total": 0}
            await _shift_roles(member, True)
            await _shift_save()
            await _shift_log(member.guild, f"{member.mention} started a shift.")
    elif cid == "shift_end":
        if a:
            await _shift_end_for(member.guild, member.id)
    elif cid == "shift_break":
        if a and not a.get("break_start"):
            a["break_start"] = now
            await _shift_save()
    elif cid == "shift_unbreak":
        if a and a.get("break_start"):
            a["break_total"] = float(a.get("break_total") or 0) + (now - float(a["break_start"]))
            a["break_start"] = 0
            await _shift_save()
    await _shift_panel(interaction, update=True)


shift_group = app_commands.Group(name="shift", description="Staff shifts.")


@shift_group.command(name="manage", description="Starts, pauses, or ends your shift and shows your time.")
async def shift_manage_cmd(interaction: discord.Interaction):
    if not _shift_can_use(interaction.user):
        return await interaction.response.send_message(embed=error_embed("Staff only", "You don't have a role that can use shifts."), ephemeral=True)
    if not _shift_loaded:
        return await interaction.response.send_message(embed=error_embed("Try again", "Shifts aren't ready yet, give it a minute."), ephemeral=True)
    await _shift_panel(interaction)


@shift_group.command(name="leaderboard", description="Shows who has been on shift the most.")
@app_commands.describe(period="This week or all time. This week if you leave it.")
@app_commands.choices(period=[app_commands.Choice(name="This week", value="week"), app_commands.Choice(name="All time", value="all")])
async def shift_leaderboard_cmd(interaction: discord.Interaction, period: app_commands.Choice[str] = None):
    guild = interaction.guild
    p = period.value if period else "week"
    since = _shift_week_start() if p == "week" else 0
    g = _shift_guild(guild.id)
    uids = set(g["history"].keys()) | set(g["active"].keys())
    rows = []
    for uid in uids:
        worked, _brk, count = _shift_totals(guild.id, uid, since)
        if worked > 0:
            rows.append((worked, count, uid))
    rows.sort(reverse=True)
    lines = [f"{i + 1}. <@{uid}>, {_fmt_dur(w)}, {c} shift{'s' if c != 1 else ''}" for i, (w, c, uid) in enumerate(rows[:15])]
    mapping = {"leaderboard": "\n".join(lines) if lines else "Nobody has been on shift yet.",
               "period": "this week" if p == "week" else "all time", "week_start": f"<t:{int(_shift_week_start())}:D>"}
    text = _shift_fill(shift_config.get("leaderboard_message") or SHIFT_DEFAULT_LEADERBOARD, mapping, guild)
    await _shift_respond(interaction, text, ephemeral=False)


@shift_group.command(name="online", description="Shows who is on shift right now.")
async def shift_online_cmd(interaction: discord.Interaction):
    guild = interaction.guild
    g = _shift_guild(guild.id)
    lines = []
    for uid, a in sorted(g["active"].items(), key=lambda kv: float(kv[1].get("start") or 0)):
        worked, _brk = _shift_active_elapsed(a)
        state = ", on break" if a.get("break_start") else ""
        lines.append(f"<@{uid}>, on shift for {_fmt_dur(worked)}{state}")
    mapping = {"online": "\n".join(lines) if lines else "Nobody is on shift right now.", "count": str(len(lines))}
    text = _shift_fill(shift_config.get("online_message") or SHIFT_DEFAULT_ONLINE, mapping, guild)
    await _shift_respond(interaction, text, ephemeral=False)


bot.tree.add_command(shift_group)


@tasks.loop(minutes=10)
async def shift_tick():
    """End shifts that have run past the cap, so a forgotten shift doesn't count forever."""
    if not _shift_loaded:
        return
    now = time.time()
    for gid, d in list(shift_data.items()):
        guild = bot.get_guild(int(gid)) if str(gid).isdigit() else None
        if not guild:
            continue
        for uid, a in list(d["active"].items()):
            if now - float(a.get("start") or now) >= SHIFT_MAX_SECONDS:
                await _shift_end_for(guild, uid, reason="ended automatically after 12 hours")


@shift_tick.before_loop
async def _shift_before():
    await bot.wait_until_ready()


# ===================== Sessions (ER:LC server sessions) =====================
# /session manage opens a staff panel whose menu is fixed (Start a vote, Start
# session, Boost, End session) while every message it posts comes from the
# dashboard Sessions block. A vote message carries a fixed Vote button that
# counts unique voters until the needed number is reached.
def _v2_text(text):
    return [{"type": "container", "children": [{"type": "text", "text": text}]}]


SESSION_DEFAULTS = {
    "panel": _v2_text("## Session manager\nPick what to do from the menu below."),
    "vote": _v2_text("## Session vote\n{ping} A session vote has started. Press Vote below if you can play.\n{votes} of {needed} votes so far."),
    "start": _v2_text("## Session started\n{ping} The server is up, join in game now.\nStarted by {user}."),
    "boost": _v2_text("## Session boost\n{ping} We need more players in the server right now. Come join."),
    "end": _v2_text("## Session ended\n{ping} The server has shut down. Thanks to everyone who joined.\nEnded by {user}."),
}
session_config = {"manager_role_ids": [], "channel_id": "", "ping_role_id": "", "vote_needed": 5,
                  "designs": {k: [] for k in SESSION_DEFAULTS}}
session_data = {}   # guild_id -> {"active", "start_ts", "started_by", "vote": {"message_id","channel_id","voters","needed","by","passed"}}
_session_loaded = False
SESSION_ACTIONS = [
    ("vote", "Start a session vote", "Post a vote and let players press Vote"),
    ("start", "Start the session", "Announce the server is up"),
    ("boost", "Boost the session", "Ask for more players"),
    ("end", "End the session", "Announce the shutdown"),
]


async def _session_load():
    global _session_loaded
    ok, cfg = await _durable_config_get("session-data")
    if not ok:
        print("[Session] load failed, sessions disabled this session.")
        return
    g = (cfg or {}).get("guilds")
    if isinstance(g, dict):
        for gid, d in g.items():
            if isinstance(d, dict):
                session_data[str(gid)] = d
    _session_loaded = True
    print(f"[Session] loaded, {sum(1 for d in session_data.values() if d.get('active'))} active")


async def _session_save():
    if not _session_loaded:
        return
    try:
        await _bot_config_upsert("session-data", {"guilds": session_data})
    except Exception as e:
        print(f"[Session] save failed: {e}")


def _session_guild(gid):
    return session_data.setdefault(str(gid), {"active": False, "start_ts": 0, "started_by": "", "vote": None})


def _session_design(key):
    d = (session_config.get("designs") or {}).get(key)
    return d if isinstance(d, list) and d else SESSION_DEFAULTS[key]


def _session_can_manage(member):
    try:
        if member.guild_permissions.manage_guild:
            return True
    except Exception:
        pass
    return has_any_role(member, session_config.get("manager_role_ids") or [])


def _session_mapping(guild, user=None, **extra):
    d = _session_guild(guild.id)
    rid = str(session_config.get("ping_role_id") or "")
    vote = d.get("vote") or {}
    m = {
        "user": user.mention if user else "", "ping": f"<@&{rid}>" if rid.isdigit() else "",
        "votes": str(len(vote.get("voters") or [])), "needed": str(vote.get("needed") or session_config.get("vote_needed") or 0),
        "started_at": f"<t:{int(d.get('start_ts') or 0)}:t>" if d.get("start_ts") else "",
        "started_by": f"<@{d['started_by']}>" if d.get("started_by") else "",
    }
    m.update(extra)
    return m


def _session_allowed_mentions():
    rid = str(session_config.get("ping_role_id") or "")
    return {"parse": ["users"], "roles": [rid]} if rid.isdigit() else {"parse": ["users"]}


async def _v2_respond(interaction, comps, rows=None, note=None, update=False, ephemeral=True):
    """Answer a slash command (type 4) or update the clicked message (type 7)
    with a designed Components V2 message plus fixed rows underneath."""
    built = [b for b in (_build_v2(c, interaction.guild) for c in comps) if b]
    if note:
        built.append({"type": 10, "content": str(note)})
    for row in (rows or []):
        built.append(row)
    flags = 1 << 15
    if ephemeral:
        flags |= 1 << 6
    data = {"flags": flags, "components": built, "allowed_mentions": {"parse": []}}
    route = discord.http.Route("POST", "/interactions/{interaction_id}/{interaction_token}/callback",
                               interaction_id=interaction.id, interaction_token=interaction.token)
    await bot.http.request(route, json={"type": 7 if update else 4, "data": data})


def _session_menu_row():
    return {"type": 1, "components": [{"type": 3, "custom_id": "session_action", "placeholder": "What do you want to do?",
                                       "options": [{"label": lbl, "value": v, "description": desc} for v, lbl, desc in SESSION_ACTIONS]}]}


async def _session_panel(interaction, note=None, update=False):
    design = _ui_render(_session_design("panel"), _session_mapping(interaction.guild, interaction.user))
    await _v2_respond(interaction, design, rows=[_session_menu_row()], note=note, update=update)


def _session_vote_button(vote, disabled=False):
    n, needed = len(vote.get("voters") or []), int(vote.get("needed") or 0)
    label = f"Vote, {n} of {needed}" if not vote.get("passed") else f"Vote passed, {n} of {needed}"
    return {"type": 2, "style": 3 if vote.get("passed") else 1, "custom_id": "session_vote", "label": label[:80], "disabled": disabled}


async def _session_edit_vote(guild, disabled=False):
    d = _session_guild(guild.id)
    vote = d.get("vote")
    if not vote or not vote.get("message_id"):
        return
    ch = guild.get_channel(int(vote["channel_id"])) if str(vote.get("channel_id")).isdigit() else None
    if not ch:
        return
    comps = _ui_render(_session_design("vote"), _session_mapping(guild, guild.get_member(int(vote["by"])) if str(vote.get("by")).isdigit() else None))
    built = [b for b in (_build_v2(c, guild) for c in comps) if b]
    built.append({"type": 1, "components": [_session_vote_button(vote, disabled)]})
    route = discord.http.Route("PATCH", "/channels/{channel_id}/messages/{message_id}", channel_id=ch.id, message_id=int(vote["message_id"]))
    try:
        await bot.http.request(route, json={"components": built, "flags": 1 << 15, "allowed_mentions": {"parse": []}})
    except Exception as e:
        print(f"[Session] vote edit failed: {e}")


async def _session_channel(interaction):
    ch = await resolve_channel(session_config.get("channel_id"))
    return ch or interaction.channel


async def _session_do(interaction, action):
    guild, user = interaction.guild, interaction.user
    d = _session_guild(guild.id)
    ch = await _session_channel(interaction)
    if action == "vote":
        needed = max(1, int(session_config.get("vote_needed") or 5))
        vote = {"message_id": "", "channel_id": str(ch.id), "voters": [], "needed": needed, "by": str(user.id), "passed": False}
        d["vote"] = vote
        comps = _ui_render(_session_design("vote"), _session_mapping(guild, user))
        mid = await send_v2_message(ch, comps, buttons=[_session_vote_button(vote)], allowed_mentions=_session_allowed_mentions())
        vote["message_id"] = str(mid) if isinstance(mid, str) else ""
        await _session_save()
        return f"Vote posted in {ch.mention}. It passes at {needed} votes."
    if action == "start":
        d["active"], d["start_ts"], d["started_by"] = True, int(time.time()), str(user.id)
        if d.get("vote"):
            d["vote"]["passed"] = True
            await _session_edit_vote(guild, disabled=True)
            d["vote"] = None
        comps = _ui_render(_session_design("start"), _session_mapping(guild, user))
        await send_v2_message(ch, comps, allowed_mentions=_session_allowed_mentions())
        await _session_save()
        return f"Session started, announced in {ch.mention}."
    if action == "boost":
        if not d.get("active"):
            return "There's no session running. Start one first."
        comps = _ui_render(_session_design("boost"), _session_mapping(guild, user))
        await send_v2_message(ch, comps, allowed_mentions=_session_allowed_mentions())
        return f"Boost posted in {ch.mention}."
    if action == "end":
        if not d.get("active") and not d.get("vote"):
            return "There's no session running."
        d["active"], d["start_ts"], d["started_by"] = False, 0, ""
        if d.get("vote"):
            await _session_edit_vote(guild, disabled=True)
            d["vote"] = None
        comps = _ui_render(_session_design("end"), _session_mapping(guild, user))
        await send_v2_message(ch, comps, allowed_mentions=_session_allowed_mentions())
        await _session_save()
        return f"Session ended, announced in {ch.mention}."
    return "Unknown action."


async def _session_select(interaction):
    member = interaction.user
    if not isinstance(member, discord.Member) or not _session_can_manage(member):
        return await interaction.response.send_message(embed=error_embed("Staff only", "You don't have a role that can manage sessions."), ephemeral=True)
    vals = (interaction.data or {}).get("values") or []
    action = vals[0] if vals else ""
    try:
        note = await _session_do(interaction, action)
    except Exception as e:
        print(f"[Session] {action} failed: {e}")
        note = f"That didn't go through: {str(e)[:120]}"
    await _session_panel(interaction, note=note, update=True)


async def _session_vote_click(interaction):
    guild, user = interaction.guild, interaction.user
    d = _session_guild(guild.id)
    vote = d.get("vote")
    if not vote or str(interaction.message.id if interaction.message else "") != str(vote.get("message_id")):
        return await interaction.response.send_message(embed=info_embed("Vote closed", "This vote isn't open anymore."), ephemeral=True)
    if vote.get("passed"):
        return await interaction.response.send_message(embed=info_embed("Vote passed", "This vote already passed. Staff can start the session."), ephemeral=True)
    voters = list(vote.get("voters") or [])
    uid = str(user.id)
    if uid in voters:
        voters.remove(uid)
        msg = "Your vote was removed."
    else:
        voters.append(uid)
        msg = "Your vote was counted."
    vote["voters"] = voters
    try:
        await interaction.response.send_message(embed=success_embed("Session vote", msg), ephemeral=True)
    except Exception:
        pass
    if len(voters) >= int(vote.get("needed") or 0):
        vote["passed"] = True
        by = f"<@{vote['by']}>" if str(vote.get("by")).isdigit() else "Staff"
        ch = guild.get_channel(int(vote["channel_id"])) if str(vote.get("channel_id")).isdigit() else None
        if ch:
            try:
                await send_v2_message(ch, _v2_text(f"{by} The vote passed with {len(voters)} votes. You can start the session from /session manage."),
                                      allowed_mentions={"parse": ["users"]})
            except Exception as e:
                print(f"[Session] vote passed notice failed: {e}")
    await _session_edit_vote(guild)
    await _session_save()


session_group = app_commands.Group(name="session", description="Server sessions.")


@session_group.command(name="manage", description="Starts a vote, starts, boosts, or ends the session.")
async def session_manage_cmd(interaction: discord.Interaction):
    if not _session_can_manage(interaction.user):
        return await interaction.response.send_message(embed=error_embed("Staff only", "You don't have a role that can manage sessions."), ephemeral=True)
    if not _session_loaded:
        return await interaction.response.send_message(embed=error_embed("Try again", "Sessions aren't ready yet, give it a minute."), ephemeral=True)
    await _session_panel(interaction)


bot.tree.add_command(session_group)


# ===================== Blacklist logs =====================
# `/blacklist` posts the log message designed in the dashboard, filling tokens
# from the command's user + reason arguments.
blacklist_config = {"design": [], "channel_id": "", "apply_role": False, "role_id": "", "strip_roles": True}

# Roles removed when a member was blacklisted, so /unblacklist can restore them.
# guild_id(str) -> { user_id(str): [role_id(str), ...] }. Persisted to bot_config.
blacklist_saved = {}
# Blacklisted Roblox accounts, so a ban-evader who links the same Roblox to a
# new Discord gets the blacklist role at verify time.
# guild_id(str) -> { roblox_id(str): discord_id(str) }.
blacklist_roblox = {}
_bl_saved_loaded = False


def _bl_mark_roblox(guild_id, roblox_id, discord_id):
    if roblox_id:
        blacklist_roblox.setdefault(str(guild_id), {})[str(roblox_id)] = str(discord_id)


def _bl_is_roblox_blacklisted(guild_id, roblox_id):
    return bool(roblox_id) and str(roblox_id) in (blacklist_roblox.get(str(guild_id)) or {})


async def _bl_load_saved():
    global _bl_saved_loaded
    ok, cfg = await _durable_config_get("blacklist-data")
    if not ok:
        print("[Blacklist] saved-roles load failed — role restore disabled this session.")
        return
    g = (cfg or {}).get("guilds")
    if isinstance(g, dict):
        for gid, users in g.items():
            if isinstance(users, dict):
                blacklist_saved[str(gid)] = {str(u): [str(x) for x in (v or [])] for u, v in users.items()}
    rb = (cfg or {}).get("roblox")
    if isinstance(rb, dict):
        for gid, ids in rb.items():
            if isinstance(ids, dict):
                blacklist_roblox[str(gid)] = {str(k): str(v) for k, v in ids.items()}
    _bl_saved_loaded = True
    n = sum(len(u) for u in blacklist_saved.values())
    nr = sum(len(u) for u in blacklist_roblox.values())
    print(f"[Blacklist] restored {n} saved role set(s), {nr} blacklisted Roblox account(s)")


async def _bl_save_saved():
    if not _bl_saved_loaded:
        return
    try:
        await _bot_config_upsert("blacklist-data",
                                 {"guilds": blacklist_saved, "roblox": blacklist_roblox})
    except Exception as e:
        print(f"[Blacklist] saved-roles save failed: {e}")

# Auto tokens the moderator does NOT type — the bot fills them from the chosen
# member: {username} -> server nickname, {discord} -> @mention,
# {roblox} / {roblox profile} / {roblox group} -> a View Profile link built from
# their verified Roblox id. Everything else uses the {Question:} form tokens.
_BL_USERNAME_RE = re.compile(r"\{\s*username\s*\}", re.IGNORECASE)
_BL_DISCORD_RE = re.compile(r"\{\s*discord\s*\}", re.IGNORECASE)
_BL_ROBLOX_RE = re.compile(r"\{\s*roblox(?:\s+profile|\s+group)?\s*\}", re.IGNORECASE)


def _bl_auto_fill(design, member, roblox_url):
    raw = json.dumps(design or [])
    nickname = member.display_name if member else ""
    mention = member.mention if member else ""
    profile = f"[View Profile]({roblox_url})" if roblox_url else "Not verified"

    def esc(v):
        return json.dumps(str(v))[1:-1]

    raw = _BL_USERNAME_RE.sub(lambda m: esc(nickname), raw)
    raw = _BL_DISCORD_RE.sub(lambda m: esc(mention), raw)
    raw = _BL_ROBLOX_RE.sub(lambda m: esc(profile), raw)
    try:
        return json.loads(raw)
    except Exception:
        return design or []


async def _bl_apply_punishment(member):
    """If enabled in the dashboard, strip the member's roles and give them the
    chosen blacklist role — a soft alternative to banning. Returns a short status
    string for the confirmation, or ''. Best-effort: never blocks the log post."""
    if not member or not blacklist_config.get("apply_role"):
        return ""
    guild = member.guild
    role_id = blacklist_config.get("role_id")
    role = guild.get_role(int(role_id)) if role_id and str(role_id).isdigit() else None
    me = guild.me
    top = me.top_role if me else None
    try:
        if blacklist_config.get("strip_roles", True):
            removable = [r for r in member.roles
                         if r != guild.default_role and not r.managed and (top and r < top) and r != role]
            if removable:
                await member.remove_roles(*removable, reason="Blacklisted")
                # Remember what we took so /unblacklist can give it all back.
                blacklist_saved.setdefault(str(guild.id), {})[str(member.id)] = [str(r.id) for r in removable]
                await _bl_save_saved()
        if role:
            if top and role >= top:
                return " (couldn't assign the blacklist role — it's above my highest role)"
            if role not in member.roles:
                await member.add_roles(role, reason="Blacklisted")
            return f" and applied {role.mention}"
        return " and removed their roles"
    except discord.Forbidden:
        return " (I'm missing the Manage Roles permission)"
    except Exception as e:
        print(f"[Blacklist] role change failed: {e}")
        return " (couldn't change their roles)"


async def _bl_roblox_id(member):
    """The target's linked Roblox user id from their verification, or '' if none."""
    if not member:
        return ""
    try:
        res = await _robux_locker_call("roblox_by_discord", discord_user_id=str(member.id))
        rid = (res or {}).get("roblox_id")
        return str(rid) if rid else ""
    except Exception as e:
        print(f"[Blacklist] roblox lookup failed: {e}")
    return ""


def _bl_roblox_url_from_id(rid):
    return f"https://www.roblox.com/users/{rid}/profile" if rid else ""


async def _bl_roblox_url(member):
    """The target's Roblox profile URL from their verification, or '' if none."""
    return _bl_roblox_url_from_id(await _bl_roblox_id(member))


@bot.tree.command(name="blacklist", description="Logs a blacklist entry.")
@app_commands.describe(user="The member to blacklist")
async def blacklist_cmd(interaction: discord.Interaction, user: discord.Member):
    if not interaction.guild:
        return await interaction.response.send_message("Use this in a server.", ephemeral=True)
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("You need Manage Server to blacklist.", ephemeral=True)
    design = blacklist_config.get("design") or []
    channel_id = blacklist_config.get("channel_id")
    if not channel_id or not design:
        return await interaction.response.send_message(
            "Blacklist logging isn't set up in the dashboard yet.", ephemeral=True)
    inputs = _pf_inputs(design)
    if not inputs:
        # No questions to ask — resolve auto tokens and post immediately.
        await interaction.response.defer(ephemeral=True)
        rid = await _bl_roblox_id(user)
        roblox_url = _bl_roblox_url_from_id(rid)
        if rid:
            _bl_mark_roblox(interaction.guild.id, rid, user.id)
            await _bl_save_saved()
        ch = await resolve_channel(channel_id)
        if not ch:
            return await interaction.followup.send("The blacklist log channel wasn't found.", ephemeral=True)
        out = _pf_render(_bl_auto_fill(design, user, roblox_url), interaction.user.id, [])
        await send_v2_message(ch, out, allowed_mentions={"parse": []})
        extra = await _bl_apply_punishment(user)
        return await interaction.followup.send(f"Logged a blacklist entry for {user.mention}{extra}.", ephemeral=True)
    # Ask the form questions; the auto tokens resolve from `user` at submit time.
    _pf_pending[("customs-blacklist", interaction.user.id)] = {
        "design": design, "title": "Blacklist", "channel_id": channel_id,
        "answers": {}, "files": {}, "blacklist_member_id": user.id,
    }
    first = next(iter(_pf_forms(design)), 1)
    await _pf_open_modal(interaction, "customs-blacklist", design, "Blacklist", first)


@bot.tree.command(name="unblacklist", description="Removes a blacklist and gives the member their roles back.")
@app_commands.describe(user="The member to unblacklist")
async def unblacklist_cmd(interaction: discord.Interaction, user: discord.Member):
    if not interaction.guild:
        return await interaction.response.send_message("Use this in a server.", ephemeral=True)
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("You need Manage Server to unblacklist.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    me = guild.me
    top = me.top_role if me else None
    notes = []

    # 1) Take the blacklist role off.
    role_id = blacklist_config.get("role_id")
    role = guild.get_role(int(role_id)) if role_id and str(role_id).isdigit() else None
    if role and role in user.roles:
        try:
            await user.remove_roles(role, reason="Unblacklisted")
            notes.append(f"removed {role.mention}")
        except Exception as e:
            print(f"[Blacklist] unblacklist remove-role failed: {e}")

    # 2) Give back whatever roles were stripped when they were blacklisted.
    saved = (blacklist_saved.get(str(guild.id)) or {}).pop(str(user.id), None)
    if saved:
        to_add = []
        for rid in saved:
            r = guild.get_role(int(rid)) if str(rid).isdigit() else None
            if (r and r != guild.default_role and not r.managed
                    and (top and r < top) and r not in user.roles):
                to_add.append(r)
        if to_add:
            try:
                await user.add_roles(*to_add, reason="Unblacklisted — restored roles")
                notes.append(f"restored {len(to_add)} role(s)")
            except discord.Forbidden:
                notes.append("couldn't restore roles (missing Manage Roles)")
            except Exception as e:
                print(f"[Blacklist] restore roles failed: {e}")
                notes.append("couldn't restore some roles")
    else:
        notes.append("no saved roles on file to restore")

    # 3) Clear their Roblox account from the blacklist so re-verifying is clean.
    rid = await _bl_roblox_id(user)
    rmap = blacklist_roblox.get(str(guild.id)) or {}
    removed_rb = False
    if rid and rid in rmap:
        rmap.pop(rid, None)
        removed_rb = True
    else:
        # Fall back to clearing by the linked discord id if the roblox lookup failed.
        for k, v in list(rmap.items()):
            if str(v) == str(user.id):
                rmap.pop(k, None)
                removed_rb = True
    await _bl_save_saved()
    if removed_rb:
        notes.append("cleared Roblox blacklist")

    tail = (" — " + ", ".join(notes)) if notes else ""
    await interaction.followup.send(f"Unblacklisted {user.mention}{tail}.", ephemeral=True)


# ===================== Small system-message designs + ticket auto-close =====================
# small_ui_config maps a UI key (e.g. "ticket_inactivity_warn") to a designed
# Components-V2 message. The dashboard "System Messages" block edits these; the
# bot substitutes it wherever that system message would otherwise be hardcoded.
small_ui_config = {}
ticket_autoclose_config = {"enabled": True, "warn_hours": 24, "close_hours": 24}
# channel_id -> unix ts we posted the inactivity warning. Persisted to bot_config
# so a redeploy doesn't forget (and re-warn) tickets it already warned.
_ticket_warned = {}
# Tickets exempted from the inactivity warn/close (-inactive hold). Persisted.
_ticket_ac_hold = set()
_ticket_ac_loaded = False
# Staff-side reminders on CLAIMED tickets: channel_id -> {"stage": 0|1|2,
# "since": unix ts of the customer's oldest unanswered message}. Stage 1 pings
# the claimer after 12h with no staff reply; stage 2 pings the ticket's staff
# roles plus the claimer at 24h. Cleared as soon as staff reply. Persisted.
_ticket_staff_nudge = {}
STAFF_NUDGE_HOURS = 12
STAFF_ESCALATE_HOURS = 24
# Queue position updates: channel_id -> last position we told the customer
# (1 = next up). Only unclaimed order tickets are "in line". Persisted so a
# redeploy doesn't re-announce positions it already reported.
_ticket_queue_pos = {}
_ticket_queue_last_notice = {}  # channel_id -> unix ts of the last update we posted


async def _load_ticket_autoclose():
    """Load the persisted 'already warned' map so a redeploy doesn't re-warn
    tickets or restart their 24h clock."""
    global _ticket_ac_loaded
    ok, cfg = await _durable_config_get("ticket-autoclose-state")
    if not ok:
        print("[Ticket] autoclose state load failed — inactivity checks paused "
              "this session to avoid false warnings.")
        return
    warned = cfg.get("warned") if isinstance(cfg, dict) else None
    if isinstance(warned, dict):
        for k, v in warned.items():
            try:
                _ticket_warned[str(k)] = float(v)
            except Exception:
                pass
    held = cfg.get("held") if isinstance(cfg, dict) else None
    if isinstance(held, list):
        _ticket_ac_hold.update(str(x) for x in held)
    nudged = cfg.get("staff_nudge") if isinstance(cfg, dict) else None
    if isinstance(nudged, dict):
        for k, v in nudged.items():
            if isinstance(v, dict):
                _ticket_staff_nudge[str(k)] = {"stage": int(v.get("stage") or 0), "since": float(v.get("since") or 0)}
    qpos = cfg.get("queue_pos") if isinstance(cfg, dict) else None
    if isinstance(qpos, dict):
        for k, v in qpos.items():
            try:
                _ticket_queue_pos[str(k)] = int(v)
            except Exception:
                pass
    _ticket_ac_loaded = True
    print(f"[Ticket] autoclose state loaded — {len(_ticket_warned)} warned, {len(_ticket_ac_hold)} held, "
          f"{len(_ticket_staff_nudge)} nudged, {len(_ticket_queue_pos)} queued ticket(s)")


async def _save_ticket_autoclose():
    if not _ticket_ac_loaded:
        return
    try:
        await _bot_config_upsert("ticket-autoclose-state",
                                 {"warned": _ticket_warned, "held": sorted(_ticket_ac_hold),
                                  "staff_nudge": _ticket_staff_nudge, "queue_pos": _ticket_queue_pos})
    except Exception as e:
        print(f"[Ticket] autoclose state save failed: {e}")


def _small_ui(key):
    d = small_ui_config.get(key)
    return d if isinstance(d, list) and d else None


def _ui_render(design, mapping):
    """Substitute simple {token} placeholders in a designed message."""
    raw = json.dumps(design or [])
    for k, v in (mapping or {}).items():
        raw = raw.replace("{" + k + "}", json.dumps(str(v))[1:-1])
    return json.loads(raw)


async def _delete_message_later(channel, message_id, delay):
    """Delete a posted channel message after `delay` seconds (best-effort)."""
    try:
        await asyncio.sleep(delay)
        msg = await channel.fetch_message(int(message_id))
        await msg.delete()
    except Exception:
        pass


async def _delete_response_later(interaction, delay):
    """Delete an interaction's original response after `delay` seconds."""
    try:
        await asyncio.sleep(delay)
        await interaction.delete_original_response()
    except Exception:
        pass


async def _ui_channel_or_embed(interaction, key, mapping, title, desc,
                               buttons=None, content=None, fallback_view=None,
                               delete_after=None):
    """Respond to an interaction with the admin's designed system message for
    `key` (posted in-channel, keeping any required buttons), or fall back to the
    built-in embed. delete_after removes the message after that many seconds."""
    design = _small_ui(key)
    if design:
        try:
            await interaction.response.defer()
        except Exception:
            pass
        mid = await send_v2_message(interaction.channel, _ui_render(design, mapping),
                                    content=content, buttons=buttons,
                                    allowed_mentions={"parse": ["users"]})
        if delete_after and isinstance(mid, str):
            asyncio.create_task(_delete_message_later(interaction.channel, mid, delete_after))
    else:
        # discord.py distinguishes MISSING from None — passing view=None crashes
        # send_message, so only include it when there actually is one.
        kwargs = {"embed": info_embed(title, desc)}
        if content:
            kwargs["content"] = content
        if fallback_view is not None:
            kwargs["view"] = fallback_view
        await interaction.response.send_message(**kwargs)
        if delete_after:
            asyncio.create_task(_delete_response_later(interaction, delete_after))


async def _ticket_last_activity(ch):
    """Timestamp of the ticket's most recent message that ISN'T one of the bot's
    own automated messages (inactivity warnings, panels, system notices). The
    bot's warning must never count as activity — otherwise it looks like someone
    replied right after the warning, the warn flag is cleared, and the ticket
    re-warns every 24 hours and never actually auto-closes.

    Returns None if history can't be read right now — we must never guess an old
    time (e.g. the channel's creation date), or a long-open ticket would get a
    false inactivity warning on the very next tick, especially right after a
    redeploy when the cache is cold."""
    try:
        me_id = getattr(bot.user, "id", None)
        async for msg in ch.history(limit=50):
            if me_id is not None and getattr(msg.author, "id", None) == me_id:
                continue  # skip the bot's own messages (warnings/panels)
            return msg.created_at.timestamp()
        # Only the bot has ever spoken here (or an empty channel): fall back to
        # the channel's age so a real human message is still required to reset.
        return ch.created_at.timestamp()
    except Exception:
        return None


async def _ticket_warn_msg(ch, opener):
    design = _small_ui("ticket_inactivity_warn")
    if design:
        await send_v2_message(ch, _ui_render(design, {"user": opener.mention if opener else "there"}),
                              allowed_mentions={"parse": ["users"]})
    else:
        # Clean Components V2 container (no accent side color, no emoji). A V2
        # message can't use a top-level content field, so the opener ping lives
        # inside the text.
        ping = f"{opener.mention} " if opener else ""
        await send_v2_message(
            ch,
            [{"type": "container", "children": [{"type": "text", "text": (
                f"{ping}**Inactivity warning**\nThis ticket has been quiet for 24 hours. "
                "If there's no reply in the next 24 hours it will be closed automatically."
            )}]}],
            allowed_mentions={"parse": ["users"]})


@tasks.loop(minutes=30)
async def ticket_inactivity_tick():
    if not ticket_autoclose_config.get("enabled", True):
        return
    # Never act on incomplete state — if the persisted 'already warned' map didn't
    # load, we could re-warn tickets we already warned. Wait for a good load.
    if not _ticket_ac_loaded:
        return
    warn_after = int(ticket_autoclose_config.get("warn_hours") or 24) * 3600
    close_after = int(ticket_autoclose_config.get("close_hours") or 24) * 3600
    now = time.time()
    dirty = False
    live_ids = set()
    for guild in list(bot.guilds):
        for ch in list(getattr(guild, "text_channels", [])):
            topic = getattr(ch, "topic", "") or ""
            if not topic.startswith("ticket|"):
                continue
            wid = str(ch.id)
            live_ids.add(wid)
            if wid in _ticket_ac_hold:
                continue  # -inactive hold — never warn/close this ticket
            last_ts = await _ticket_last_activity(ch)
            if last_ts is None:
                continue  # couldn't read history this tick — retry next time
            warned_at = _ticket_warned.get(wid)
            if warned_at:
                if last_ts > warned_at + 2:  # someone replied after the warning
                    _ticket_warned.pop(wid, None)
                    dirty = True
                    continue
                if now - warned_at >= close_after:
                    _ticket_warned.pop(wid, None)
                    dirty = True
                    try:
                        await _do_close(ch, guild, bot.user, reason="Auto-closed for inactivity")
                    except Exception as e:
                        print(f"[Ticket] auto-close failed: {e}")
            elif now - last_ts >= warn_after:
                _ticket_warned[wid] = now
                dirty = True
                parts = topic.split("|")
                opener_id = parts[1] if len(parts) > 1 else ""
                opener = guild.get_member(int(opener_id)) if opener_id.isdigit() else None
                try:
                    await _ticket_warn_msg(ch, opener)
                except Exception as e:
                    print(f"[Ticket] warn failed: {e}")
    # Forget warned/held entries for tickets that no longer exist (closed/deleted).
    for gone in [w for w in _ticket_warned if w not in live_ids]:
        _ticket_warned.pop(gone, None)
        dirty = True
    for gone in [w for w in _ticket_ac_hold if w not in live_ids]:
        _ticket_ac_hold.discard(gone)
        dirty = True
    if dirty:
        await _save_ticket_autoclose()


@ticket_inactivity_tick.before_loop
async def _ticket_inactivity_before():
    await bot.wait_until_ready()


def _ticket_topic_info(ch):
    """Parse a ticket channel's topic: ticket|opener|cat|base|claim_ts|claimer."""
    parts = (getattr(ch, "topic", "") or "").split("|")
    if not parts or parts[0] != "ticket":
        return None
    claim_ts = parts[4].strip() if len(parts) > 4 else ""
    claimer = parts[5].strip() if len(parts) > 5 else ""
    return {
        "opener_id": parts[1] if len(parts) > 1 else "",
        "cat": parts[2] if len(parts) > 2 else "",
        "base": parts[3] if len(parts) > 3 else "",
        "claimed": claim_ts.isdigit(),
        "claim_ts": int(claim_ts) if claim_ts.isdigit() else 0,
        "claimer_id": claimer if claimer.isdigit() else "",
        "status": parts[6].strip() if len(parts) > 6 else "",  # /progress stage
    }


async def _ticket_waiting_since(ch, opener_id):
    """If the customer is waiting on staff: the unix ts of their OLDEST message
    since the last staff reply. None when a staff member spoke last (or history
    can't be read). The bot's own messages never count either way."""
    try:
        me_id = getattr(bot.user, "id", None)
        oldest = None
        async for msg in ch.history(limit=50):
            a = msg.author
            if me_id is not None and getattr(a, "id", None) == me_id:
                continue
            is_customer = str(getattr(a, "id", "")) == str(opener_id)
            if not is_customer and isinstance(a, discord.Member) and _is_ticket_staff(a, ch):
                break  # staff replied — the customer isn't waiting past this point
            if is_customer:
                oldest = msg.created_at.timestamp()
        return oldest
    except Exception:
        return None


async def _staff_nudge_msg(ch, stage, claimer, roles):
    hours = STAFF_ESCALATE_HOURS if stage >= 2 else STAFF_NUDGE_HOURS
    who = ""
    allowed = {"parse": ["users"]}
    if stage >= 2 and roles:
        who = " ".join(r.mention for r in roles) + " "
        allowed = {"parse": ["users"], "roles": [str(r.id) for r in roles]}
    if claimer:
        who += f"{claimer.mention} "
    design = _small_ui("ticket_staff_reminder")
    if design:
        await send_v2_message(ch, _ui_render(design, {
            "user": claimer.mention if claimer else "", "hours": hours,
            "roles": " ".join(r.mention for r in roles) if (stage >= 2 and roles) else "",
        }), allowed_mentions=allowed)
        return
    text = (f"{who}**Waiting on staff**\nThe customer has been waiting {hours} hours for a reply on this order."
            if stage < 2 else
            f"{who}**Still waiting on staff**\nNo staff reply on this order for {hours} hours. "
            + (f"{claimer.mention} has it claimed." if claimer else "It's claimed but nobody has answered."))
    await send_v2_message(ch, [{"type": "container", "children": [{"type": "text", "text": text}]}],
                          allowed_mentions=allowed)


@tasks.loop(minutes=30)
async def ticket_staff_reply_tick():
    """Nudge the claimer when a claimed ticket's customer has waited 12h with no
    staff reply; at 24h ping the ticket's staff roles too (with the claimer's @)."""
    if not _ticket_ac_loaded:
        return
    now = time.time()
    dirty = False
    live = set()
    for guild in list(bot.guilds):
        for ch in list(getattr(guild, "text_channels", [])):
            info = _ticket_topic_info(ch)
            if not info:
                continue
            wid = str(ch.id)
            live.add(wid)
            if not info["claimed"] or wid in _ticket_ac_hold:
                if wid in _ticket_staff_nudge:
                    _ticket_staff_nudge.pop(wid, None)
                    dirty = True
                continue
            since = await _ticket_waiting_since(ch, info["opener_id"])
            state = _ticket_staff_nudge.get(wid)
            if since is None:
                if state:
                    _ticket_staff_nudge.pop(wid, None)  # staff replied
                    dirty = True
                continue
            if state and abs(state.get("since", 0) - since) > 2:
                state = None  # a new unanswered run started — restart the clock
            stage = int(state.get("stage") or 0) if state else 0
            waited = now - since
            want = 2 if waited >= STAFF_ESCALATE_HOURS * 3600 else (1 if waited >= STAFF_NUDGE_HOURS * 3600 else 0)
            if want <= stage:
                if state is None and wid in _ticket_staff_nudge:
                    _ticket_staff_nudge.pop(wid, None)
                    dirty = True
                continue
            claimer = guild.get_member(int(info["claimer_id"])) if info["claimer_id"] else None
            roles = _ticket_reping_roles(ch) if want >= 2 else []
            if want == 1 and claimer is None:
                continue  # old-format claim with no claimer id: only the 24h staff ping applies
            try:
                await _staff_nudge_msg(ch, want, claimer, roles)
            except Exception as e:
                print(f"[Ticket] staff reminder failed: {e}")
            _ticket_staff_nudge[wid] = {"stage": want, "since": since}
            dirty = True
    for gone in [w for w in _ticket_staff_nudge if w not in live]:
        _ticket_staff_nudge.pop(gone, None)
        dirty = True
    if dirty:
        await _save_ticket_autoclose()


@ticket_staff_reply_tick.before_loop
async def _ticket_staff_reply_before():
    await bot.wait_until_ready()


async def _dispatch_ticket_open(interaction, action, agreed=False):
    """Open a ticket for a panel action string exactly as the button would."""
    if action.startswith("ticket_msg:"):
        mk = action.split(":", 1)[1]
        await open_ticket(interaction, action, open_comps_override=ticket_msgs.get(mk),
                          category_name_override=ticket_categories.get(mk), access_names_override=ticket_access.get(mk), agreed=agreed)
    elif action.startswith("ticket_form:"):
        key = action.split(":", 1)[1]
        await open_ticket(interaction, action, open_comps_override=form_msgs.get(key) or [], agreed=agreed)
    elif action.startswith("ticket_cat:"):
        await open_ticket(interaction, action.split(":", 1)[1], agreed=agreed)
    elif action == "ticket_open":
        await open_ticket(interaction, "support", agreed=agreed)
    else:
        await open_ticket(interaction, action, agreed=agreed)


async def open_ticket_form(interaction, key):
    """A Form button/option: pop a modal to collect {Question:} answers, then
    open the ticket with those answers filled into the designed message."""
    open_comps = form_msgs.get(key) or []
    fields = _parse_form_fields(open_comps, limit=FORM_MAX_QUESTIONS)
    if not fields:
        # No questions/files defined — behave exactly like a Ticket button.
        await _dispatch_ticket_open(interaction, f"ticket_form:{key}")
        return

    guild = interaction.guild
    st = _source_settings.get(_key_source.get(key, "tickets"), ticket_config)
    if guild and st.get("one_per_user", True):
        cat_name = ticket_categories.get(key)
        fb = None
        if not cat_name:
            cid = st.get("category_id") or ""
            if cid:
                fb = guild.get_channel(int(cid))
        if _user_ticket_count_for(guild, interaction.user.id, cat_name, fb) >= MAX_TICKETS_PER_SECTION:
            try:
                await interaction.response.send_message(
                    embed=error_embed("Limit reached", f"You already have {MAX_TICKETS_PER_SECTION} open tickets in this section. Please close one before opening another."),
                    ephemeral=True,
                )
            except Exception:
                pass
            return

    # Start fresh, then open page 1 of the form (up to 5 fields per page,
    # continued with a button if there are more — Discord caps a modal at 5).
    _pending_form_answers.pop((interaction.user.id, key), None)
    _pending_form_files.pop((interaction.user.id, key), None)
    try:
        await _open_form_page(interaction, key, 0)
    except Exception as e:
        print(f"[Ticket] form modal failed: {e}")
        try:
            await interaction.response.send_message(embed=error_embed("Couldn't open form", "Please try again."), ephemeral=True)
        except Exception:
            pass


async def handle_ticket_form_submit(interaction, key, page=0):
    # Form-log forms (/orderlog, /infraction, /promote) read their design from
    # their own config (robust even if the shared registry was rebuilt mid-form)
    # and post to a channel instead of opening a ticket.
    open_comps = (form_log_configs[key]["components"] if key in form_log_configs else form_msgs.get(key)) or []
    fields = _parse_form_fields(open_comps, limit=FORM_MAX_QUESTIONS)
    total_pages = (len(fields) + FORM_PAGE_SIZE - 1) // FORM_PAGE_SIZE

    # Stash this page's answers + files (keyed to the member so pages accumulate).
    vals = _collect_modal_values((interaction.data or {}).get("components"))
    pend = _pending_form_answers.setdefault((interaction.user.id, key), {})
    pend_files = _pending_form_files.setdefault((interaction.user.id, key), [])
    start = page * FORM_PAGE_SIZE
    for j, f in enumerate(fields[start:start + FORM_PAGE_SIZE]):
        idx = start + j
        if f["kind"] == "file":
            for up in _modal_uploaded_files(interaction, f"f{idx}"):
                pend_files.append({"label": f["label"], "url": up["url"], "filename": up.get("filename"), "before": bool(f.get("before"))})
        else:
            pend[f["label"]] = (vals.get(f"q{idx}") or "").strip()

    # More fields to go — offer a Continue button that opens the next modal
    # (button -> modal is always allowed, unlike modal -> modal).
    if page + 1 < total_pages:
        remaining = len(fields) - (page + 1) * FORM_PAGE_SIZE
        row = {"type": 1, "components": [{
            "type": 2, "style": 1, "custom_id": f"formcont:{key}|{page + 1}", "label": "Continue",
        }]}
        data = {"flags": 1 << 6,
                "content": f"Saved, **{remaining}** more field{'s' if remaining != 1 else ''} to go. Tap **Continue**.",
                "components": [row]}
        try:
            route = discord.http.Route(
                "POST", "/interactions/{interaction_id}/{interaction_token}/callback",
                interaction_id=interaction.id, interaction_token=interaction.token)
            await bot.http.request(route, json={"type": 4, "data": data})
        except Exception as e:
            print(f"[Ticket] form continue prompt failed: {e}")
        return

    await _finish_ticket_form(interaction, key, open_comps)


async def _finish_ticket_form(interaction, key, open_comps, agreed=False):
    # Last page — acknowledge, then build the ticket with ALL collected answers.
    try:
        await interaction.response.defer(ephemeral=True, thinking=True)
    except Exception as e:
        print(f"[Ticket] form submit defer failed: {e}")
    try:
        mapping = dict(_pending_form_answers.pop((interaction.user.id, key), {}))
        files = list(_pending_form_files.pop((interaction.user.id, key), []))
        substituted = _apply_answers(open_comps, mapping)
        if key in form_log_configs:
            await _post_form_log(interaction, key, substituted, files=files)
            return
        await open_ticket(interaction, f"ticket_form:{key}", open_comps_override=substituted,
                          category_name_override=ticket_categories.get(key), access_names_override=ticket_access.get(key),
                          already_responded=True, attachments=files, agreed=agreed)
    except Exception as e:
        import traceback
        print(f"[Ticket] form submit failed: {e}\n{traceback.format_exc()}")
        try:
            await interaction.followup.send(embed=error_embed("Couldn't open ticket", "Something went wrong creating your ticket. Please try again."), ephemeral=True)
        except Exception:
            pass


_category_locks = {}


async def _get_or_create_category(guild, name):
    """Find a category by name (case-insensitive), creating it only if none
    exists. A per-name lock + re-check makes concurrent ticket opens reuse the
    same category instead of racing to create duplicate 'ELS' categories."""
    name = (name or "").strip()
    if not name:
        return None
    target = name.lower()

    def _find():
        for cat in guild.categories:
            if cat.name.strip().lower() == target:
                return cat
        return None

    existing = _find()
    if existing:
        return existing

    key = (guild.id, target)
    lock = _category_locks.get(key)
    if lock is None:
        lock = _category_locks[key] = asyncio.Lock()
    async with lock:
        # Another open may have created it while we waited for the lock.
        existing = _find()
        if existing:
            return existing
        try:
            return await guild.create_category(name=name[:100], reason="Ticket category")
        except Exception as e:
            print(f"[Tickets] category create failed for {name!r}: {e}")
            return None


def _resolve_role_names(guild, names_csv):
    """Turn a comma-separated list of role names into role objects (case-insensitive)."""
    if not names_csv or not guild:
        return []
    wanted = [n.strip().lower() for n in str(names_csv).split(",") if n.strip()]
    if not wanted:
        return []
    out = []
    for role in guild.roles:
        if role.is_default():
            continue
        if role.name.strip().lower() in wanted and role not in out:
            out.append(role)
    return out


async def open_ticket(interaction, category, open_comps_override=None, category_name_override=None, access_names_override=None, already_responded=False, attachments=None, agreed=False):
    guild = interaction.guild
    if not guild:
        return
    if not already_responded:
        await interaction.response.defer(ephemeral=True)
    # Tickets vs Marketplace: use the settings block for whichever panel this
    # button came from.
    st = _settings_for_category(category)

    # Per-Ticket/Form category (by name, created on demand) wins; otherwise fall
    # back to the configured category id for this source.
    category_channel = None
    if category_name_override:
        category_channel = await _get_or_create_category(guild, category_name_override)
    if category_channel is None:
        cat_id = st.get("category_id") or ""
        if cat_id:
            category_channel = guild.get_channel(int(cat_id))

    # Limit: up to MAX_TICKETS_PER_SECTION open tickets per section (category).
    if st.get("one_per_user", True):
        open_count = _user_ticket_count_for(guild, interaction.user.id, category_name_override, category_channel)
        if open_count >= MAX_TICKETS_PER_SECTION:
            await interaction.followup.send(embed=error_embed("Limit reached", f"You already have {MAX_TICKETS_PER_SECTION} open tickets in this section. Please close one before opening another."), ephemeral=True)
            return

    support_roles = []
    for rid in st.get("support_role_ids", []):
        role = guild.get_role(int(rid))
        if role:
            support_roles.append(role)
    # Per-Ticket/Form access roles (by name) — who can SEE this ticket. Kept
    # separate from support_roles so they grant visibility without being pinged.
    access_roles = _resolve_role_names(guild, access_names_override)

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, embed_links=True),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True, manage_messages=True),
    }
    for role in support_roles:
        overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
    for role in access_roles:
        overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

    tdef = next((t for t in ticket_config.get("types", []) if t.get("id") == category), None)
    open_comps = open_comps_override if open_comps_override is not None else ((tdef.get("open_components") if tdef else None) or [])
    type_name = (tdef.get("name") if tdef else None) or str(category).replace("_", " ").title()
    first_word = _ticket_first_word(open_comps) or (type_name.split()[0] if type_name.split() else "ticket")
    ticket_base = f"{_san_name(interaction.user.name)}-{_san_name(first_word)}".strip("-") or _san_name(interaction.user.name)
    base_name = f"\U0001F534\u30FB{ticket_base}"[:90]
    try:
        channel = await guild.create_text_channel(
            name=base_name,
            category=category_channel if isinstance(category_channel, discord.CategoryChannel) else None,
            overwrites=overwrites,
            topic=_ticket_topic(interaction.user.id, category, ticket_base),
            reason=f"Ticket opened by {interaction.user}",
        )
    except discord.Forbidden:
        await interaction.followup.send(embed=error_embed("Couldn't open ticket", "I'm missing the Manage Channels permission."), ephemeral=True)
        return
    except Exception as e:
        await interaction.followup.send(embed=error_embed("Couldn't open ticket", str(e)), ephemeral=True)
        return

    # No auto-pings. Support roles get channel access (above) but are never
    # pinged automatically — the ticket shows ONLY the designed message. To ping
    # someone, write @role directly into the ticket/form design.
    content = None

    # (ticket type + opening components resolved above for the channel name)
    sent_rich = False
    if open_comps:
        try:
            def _js(s):
                return json.dumps(str(s))[1:-1]
            raw = json.dumps(open_comps)
            raw = raw.replace("{user}", _js(interaction.user.mention)).replace("{username}", _js(interaction.user.display_name))
            comps = json.loads(raw)
            close_row = {"type": "buttonRow", "buttons": [{"label": "Claim", "style": "success", "__ticket_claim": True}, {"label": "Close", "style": "danger", "__ticket_close": True}]}
            panel = [dict(c) for c in comps]
            tail = [close_row]
            container_idxs = [i for i, c in enumerate(panel) if c.get("type") == "container"]
            if container_idxs:
                i = container_idxs[-1]
                panel[i] = dict(panel[i])
                panel[i]["children"] = list(panel[i].get("children") or []) + tail
            else:
                panel.extend(tail)
            # Ping first (plain message) so the opener + support actually get notified,
            # then the rich Components V2 message (which can't carry a pinging content).
            if content:
                try:
                    await channel.send(content=content)
                except Exception:
                    pass
            # Allow role + user mentions inside the ticket message to actually
            # ping (e.g. a @Livery Designer role written into the design).
            mid = await send_v2_message(channel, panel, allowed_mentions={"parse": ["users", "roles"]})
            sent_rich = bool(mid)
            # Uploaded form files go into a THREAD off the opening message (named
            # after the file field, e.g. "References"), not on the main message.
            if sent_rich and attachments:
                thread_name = _clean_label(attachments[0].get("label") or "References") or "References"
                await _post_form_files_thread(channel, mid if isinstance(mid, str) else None, attachments, thread_name)
                attachments = None  # handled in the thread
        except Exception as e:
            print(f"[Tickets] rich open message failed: {e}")
            sent_rich = False

    if not sent_rich:
        open_msg = st.get("open_message") or f"Thanks {interaction.user.mention}, a member of the team will be with you shortly."
        open_msg = open_msg.replace("{user}", interaction.user.mention)
        embed = info_embed(f"{type_name} ticket", open_msg)
        embed.set_footer(text=f"Opened by {interaction.user}")

        close_view = discord.ui.View(timeout=None)
        close_view.add_item(discord.ui.Button(label="Claim", style=discord.ButtonStyle.success, custom_id="ticket_claim"))
        close_view.add_item(discord.ui.Button(label="Close", style=discord.ButtonStyle.danger, custom_id="ticket_close", emoji="🔒"))

        await channel.send(content=content, embed=embed, view=close_view)
    # Post any uploaded form files into the ticket (each labelled by its field).
    if attachments:
        await _post_form_files(channel, attachments)
    await record_ticket(guild.id, channel.id, interaction.user.id, category, "open")
    await interaction.followup.send(embed=success_embed("Ticket opened", f"Your ticket is ready: {channel.mention}"), ephemeral=True)


async def show_ephemeral(interaction, key):
    comps = eph_msgs.get(key)
    print(f"[Tickets] show_ephemeral key={key!r} registered={key in eph_msgs} len={len(comps) if comps else 0} "
          f"all_eph={{{', '.join(f'{k}:{len(v or [])}' for k, v in eph_msgs.items())}}}")
    if not comps:
        # Unknown key — this posted message predates a design edit and its
        # content isn't in the registry. Say so instead of silently doing
        # nothing, so it's obvious the panel needs a re-save/re-post.
        try:
            await interaction.response.send_message(
                embed=info_embed(
                    "This option needs a refresh",
                    "This message is from an older version of its design. "
                    "An admin can fix it by re-saving that block in the dashboard "
                    "(or re-posting this message).",
                ),
                ephemeral=True,
            )
        except Exception:
            pass
        return
    try:
        await interaction.response.defer(ephemeral=True)
    except Exception:
        pass

    def _js(x):
        return json.dumps(str(x))[1:-1]
    try:
        raw = json.dumps(comps)
        raw = raw.replace("{user}", _js(interaction.user.mention)).replace("{username}", _js(interaction.user.display_name))
        comps2 = json.loads(raw)
    except Exception:
        comps2 = comps
    ok = await send_v2_message(interaction.channel, comps2, interaction=interaction, ephemeral=True)
    if not ok:
        try:
            await interaction.followup.send(embed=info_embed("Note", "Couldn't render this message."), ephemeral=True)
        except Exception:
            pass


async def _do_close(channel, guild, closer, reason=""):
    topic = getattr(channel, "topic", "") or ""
    parts = topic.split("|")
    opener_id = parts[1] if len(parts) > 1 else ""
    category = parts[2] if len(parts) > 2 else "support"
    transcript = await build_transcript(channel)
    log_id = _settings_for_category(category).get("log_channel_id") or ""
    opener = guild.get_member(int(opener_id)) if opener_id.isdigit() else None
    if log_id:
        log_channel = guild.get_channel(int(log_id))
        if log_channel:
            desc = f"**Category:** {category}\n**Opened by:** {opener.mention if opener else opener_id}\n**Closed by:** {closer.mention}"
            if reason:
                desc += f"\n**Reason:** {reason}"
            try:
                await log_channel.send(embed=info_embed("Ticket closed", desc), file=discord.File(io.BytesIO(transcript.encode("utf-8")), filename=f"{channel.name}.txt"))
            except Exception as e:
                print(f"[Ticket] log failed: {e}")
    await record_ticket(guild.id, channel.id, opener_id, category, "closed")
    # The customer's copy: a short summary and the transcript.
    try:
        await _ticket_close_dm(channel, guild, opener, closer, reason, transcript)
    except Exception as e:
        print(f"[Ticket] close DM failed: {e}")
    await asyncio.sleep(2)
    try:
        await channel.delete(reason=f"Ticket closed by {closer}")
    except Exception as e:
        print(f"[Ticket] delete failed: {e}")


async def _ticket_close_dm(channel, guild, opener, closer, reason, transcript):
    """DM the opener when their ticket closes: what it was, who handled it, and
    the transcript file."""
    if opener is None or getattr(opener, "bot", False):
        return
    info = _ticket_topic_info(channel) or {}
    claimer_id = info.get("claimer_id") or ""
    handler = guild.get_member(int(claimer_id)) if claimer_id else None
    what = channel.category.name if channel.category else (info.get("cat") or "your ticket")
    e = discord.Embed(title="Your ticket was closed", color=0x2b2d31, timestamp=discord.utils.utcnow())
    lines = [f"**Server:** {guild.name}", f"**Ticket:** {what}"]
    if handler:
        lines.append(f"**Handled by:** {handler.mention}")
    lines.append(f"**Closed by:** {closer.mention if hasattr(closer, 'mention') else closer}")
    if reason:
        lines.append(f"**Reason:** {reason}")
    e.description = "\n".join(lines)
    e.set_footer(text="Your transcript is attached.")
    file = discord.File(io.BytesIO((transcript or "").encode("utf-8")), filename=f"{channel.name}.txt")
    await opener.send(embed=e, file=file)


async def close_ticket(interaction):
    channel = interaction.channel
    topic = getattr(channel, "topic", "") or ""
    if not topic.startswith("ticket|"):
        await interaction.response.send_message(embed=error_embed("Not a ticket", "This channel isn't a ticket."), ephemeral=True)
        return
    opener_id = topic.split("|")[1] if len(topic.split("|")) > 1 else ""
    is_opener = str(interaction.user.id) == opener_id
    if not (_is_ticket_staff(interaction.user, channel) or is_opener):
        await interaction.response.send_message(embed=error_embed("No permission", "Only staff or the opener can close this."), ephemeral=True)
        return
    await interaction.response.send_message(embed=info_embed("Closing order", "Saving transcript and closing\u2026"))
    await _do_close(channel, interaction.guild, interaction.user)


def _is_ticket_staff(member, channel=None):
    try:
        if member.guild_permissions.manage_channels:
            return True
    except Exception:
        pass
    # Global support roles (see & manage ALL tickets), if any are configured.
    if has_any_role(member, ticket_config.get("support_role_ids", [])):
        return True
    # Per-ticket: any role granted view access to THIS channel is staff for it,
    # so a section's Access roles can claim/close their own tickets.
    if channel is not None:
        member_role_ids = {r.id for r in getattr(member, "roles", [])}
        try:
            for target, ow in channel.overwrites.items():
                if isinstance(target, discord.Role) and not target.is_default() and ow.view_channel and target.id in member_role_ids:
                    return True
        except Exception:
            pass
    return False


def _ticket_guard(interaction):
    """Return (channel, ok, err_embed) — channel must be a ticket and the caller
    must be staff or the opener."""
    channel = interaction.channel
    topic = getattr(channel, "topic", "") or ""
    if not topic.startswith("ticket|"):
        return channel, False, error_embed("Not a ticket", "Run this inside a ticket channel.")
    parts = topic.split("|")
    opener_id = parts[1] if len(parts) > 1 else ""
    if not (_is_ticket_staff(interaction.user, channel) or str(interaction.user.id) == opener_id):
        return channel, False, error_embed("No permission", "Only staff or the ticket opener can do that.")
    return channel, True, None


@bot.tree.command(name="ticketadd", description="Adds someone to this ticket.")
@app_commands.describe(user="The member to add to this ticket")
async def ticketadd_cmd(interaction: discord.Interaction, user: discord.Member):
    channel, ok, err = _ticket_guard(interaction)
    if not ok:
        await interaction.response.send_message(embed=err, ephemeral=True)
        return
    try:
        await channel.set_permissions(
            user, view_channel=True, send_messages=True, attach_files=True,
            embed_links=True, read_message_history=True, reason=f"Ticket add by {interaction.user}")
    except discord.Forbidden:
        await interaction.response.send_message(embed=error_embed("Missing permission", "I need **Manage Channels** in this ticket."), ephemeral=True)
        return
    except Exception as e:
        await interaction.response.send_message(embed=error_embed("Couldn't add", str(e)[:200]), ephemeral=True)
        return
    await interaction.response.send_message(embed=success_embed("Added", f"{user.mention} was added to this ticket."))


@bot.tree.command(name="ticketremove", description="Removes someone from this ticket.")
@app_commands.describe(user="The member to remove from this ticket")
async def ticketremove_cmd(interaction: discord.Interaction, user: discord.Member):
    channel, ok, err = _ticket_guard(interaction)
    if not ok:
        await interaction.response.send_message(embed=err, ephemeral=True)
        return
    try:
        await channel.set_permissions(user, overwrite=None, reason=f"Ticket remove by {interaction.user}")
    except discord.Forbidden:
        await interaction.response.send_message(embed=error_embed("Missing permission", "I need **Manage Channels** in this ticket."), ephemeral=True)
        return
    except Exception as e:
        await interaction.response.send_message(embed=error_embed("Couldn't remove", str(e)[:200]), ephemeral=True)
        return
    await interaction.response.send_message(embed=success_embed("Removed", f"{user.mention} was removed from this ticket."))


def _toggle_claim_in_components(comps, claimed):
    for c in (comps or []):
        if not isinstance(c, dict):
            continue
        if isinstance(c.get("components"), list):
            _toggle_claim_in_components(c["components"], claimed)
        if c.get("type") == 2 and c.get("custom_id") in ("ticket_claim", "ticket_unclaim"):
            c.pop("emoji", None)
            if claimed:
                c["custom_id"], c["label"], c["style"] = "ticket_unclaim", "Unclaim", 2
            else:
                c["custom_id"], c["label"], c["style"] = "ticket_claim", "Claim", 3


def _has_claim_button(components):
    for c in (components or []):
        if not isinstance(c, dict):
            continue
        if c.get("type") == 2 and c.get("custom_id") in ("ticket_claim", "ticket_unclaim"):
            return True
        if isinstance(c.get("components"), list) and _has_claim_button(c["components"]):
            return True
    return False


async def _find_claim_message(channel):
    """Find the ticket's opening message (the one carrying the Claim button) so a
    -claim/-unclaim text command can toggle it just like the button does."""
    try:
        async for m in channel.history(limit=15, oldest_first=True):
            if not (m.author and m.author.id == bot.user.id):
                continue
            try:
                raw = await bot.http.get_message(channel.id, m.id)
            except Exception:
                continue
            if _has_claim_button(raw.get("components", [])):
                return m
    except Exception:
        pass
    return None


async def ticket_claim_toggle(interaction, claimed):
    member = interaction.user
    if not _is_ticket_staff(member, interaction.channel):
        await interaction.response.send_message(embed=error_embed("No permission", "Only staff can claim orders."), ephemeral=True)
        return
    channel, msg = interaction.channel, interaction.message
    key = "ticket_claimed" if claimed else "ticket_unclaimed"
    title = "Order claimed" if claimed else "Order unclaimed"
    verb = "claimed" if claimed else "unclaimed"
    await _ui_channel_or_embed(interaction, key, {"user": member.mention},
                               title, f"{member.mention} {verb} this order.",
                               delete_after=6)
    await _do_claim_toggle(channel, member, claimed, msg)


# Only re-ping the ticket's roles on unclaim if it was actually held for at
# least this long — a quick claim-then-unclaim shouldn't ping anyone.
CLAIM_REPING_AFTER = 60  # seconds


def _ticket_reping_roles(channel):
    """The roles to notify when a ticket becomes available again: the roles
    selected for THIS ticket (they hold a view overwrite on the channel),
    preferring the per-ticket access roles over the global 'see all' support
    roles, and falling back to the global support roles if that's all there is."""
    guild = channel.guild
    global_support = set(str(x) for x in (ticket_config.get("support_role_ids") or []))
    access, support = [], []
    for target, ow in channel.overwrites.items():
        if isinstance(target, discord.Role) and target != guild.default_role and ow.view_channel is True:
            (support if str(target.id) in global_support else access).append(target)
    return access or support


async def _ticket_reping(channel):
    roles = _ticket_reping_roles(channel)
    if not roles:
        return
    mention = " ".join(r.mention for r in roles)
    # Clean V2 container (no accent side line, no emoji) — exactly the text asked.
    await send_v2_message(
        channel,
        [{"type": "container", "children": [
            {"type": "text", "text": f"{mention} This commission is back available."}]}],
        allowed_mentions={"roles": [str(r.id) for r in roles]},
    )


async def _do_claim_toggle(channel, member, claimed, msg):
    # Toggle the Claim/Unclaim button on the ticket message (if we have it).
    if msg is not None:
        try:
            raw = await bot.http.get_message(channel.id, msg.id)
            comps = raw.get("components", []) or []
            _toggle_claim_in_components(comps, claimed)
            route = discord.http.Route("PATCH", "/channels/{channel_id}/messages/{message_id}", channel_id=channel.id, message_id=msg.id)
            await bot.http.request(route, json={"components": comps, "flags": raw.get("flags", 0)})
        except Exception as e:
            print(f"[Tickets] claim toggle failed: {e}")
    # Rename + reorder: on claim, go green + claimer and jump to the TOP of the
    # category (saving the old slot in the topic). On unclaim, go back to red +
    # opener-firstword and drop back to where it was.
    try:
        parts = (getattr(channel, "topic", "") or "").split("|")
        opener_id = parts[1] if len(parts) > 1 else ""
        cat = parts[2] if len(parts) > 2 else "support"
        base = parts[3] if len(parts) > 3 and parts[3] else _san_name(getattr(channel, "name", "ticket"))
        if claimed:
            # Stamp the claim time in the topic (slot 5) so a later unclaim \u2014 even
            # after a redeploy \u2014 can tell a quick claim/unclaim from a real hold.
            new_name = f"\U0001F7E2\u30FB{_san_name(member.name)}"[:90]
            # Slot 6 = who claimed it, so the staff-reply reminders can @ them.
            new_topic = f"ticket|{opener_id}|{cat}|{base}|{int(time.time())}|{member.id}"
            await channel.edit(name=new_name, topic=new_topic, reason=f"Ticket claimed by {member}")
            try:
                await channel.move(beginning=True, category=channel.category, sync_permissions=False, reason="Claimed ticket to top")
            except Exception as e:
                print(f"[Tickets] move-to-top failed: {e}")
        else:
            claim_ts = 0
            if len(parts) > 4 and parts[4].strip().isdigit():
                claim_ts = int(parts[4])
            new_name = f"\U0001F534\u30FB{base}"[:90]
            new_topic = f"ticket|{opener_id}|{cat}|{base}"
            await channel.edit(name=new_name, topic=new_topic, reason=f"Ticket unclaimed by {member}")
            # Drop the ticket to the very bottom of its category.
            try:
                await channel.move(end=True, category=channel.category, sync_permissions=False, reason="Unclaimed ticket to bottom")
            except Exception as e:
                print(f"[Tickets] move-to-bottom failed: {e}")
            # Only re-ping if it was genuinely held for a while (not an instant
            # claim -> unclaim).
            held = int(time.time()) - claim_ts if claim_ts else 0
            if claim_ts and held >= CLAIM_REPING_AFTER:
                try:
                    await _ticket_reping(channel)
                except Exception as e:
                    print(f"[Tickets] reping failed: {e}")
    except Exception as e:
        print(f"[Tickets] rename/reorder failed: {e}")


# ---- Text commands: -claim / -unclaim / -close (mirror the buttons) ----
async def _cmd_claim(message, claimed):
    channel = message.channel
    member = message.author
    if not _is_ticket_staff(member, channel):
        await channel.send(embed=error_embed("No permission", "Only staff can claim orders."), delete_after=10)
        return
    msg = await _find_claim_message(channel)
    verb = "claimed" if claimed else "unclaimed"
    await channel.send(embed=info_embed(f"Order {verb}", f"{member.mention} {verb} this order."),
                       delete_after=6)
    await _do_claim_toggle(channel, member, claimed, msg)


async def _cmd_close(message, reason=""):
    channel = message.channel
    topic = getattr(channel, "topic", "") or ""
    opener_id = topic.split("|")[1] if len(topic.split("|")) > 1 else ""
    member = message.author
    if not (_is_ticket_staff(member, channel) or str(member.id) == opener_id):
        await channel.send(embed=error_embed("No permission", "Only staff or the opener can close this."), delete_after=10)
        return
    await channel.send(embed=info_embed("Closing order", "Saving transcript and closing…"))
    await _do_close(channel, message.guild, member, (reason or "").strip())


import random as _rnd


class _EconLayout(discord.ui.LayoutView):
    """Render an economy message as a clean Components V2 container — a boxed
    card with NO colored sidebar line (accent_colour is left unset), which the
    owner wanted for a sleeker look than a classic embed."""

    def __init__(self, title, body):
        super().__init__(timeout=None)
        c = discord.ui.Container()  # no accent_colour -> no sidebar accent line
        text = f"## {title}\n{body}" if title else (body or "​")
        c.add_item(discord.ui.TextDisplay(text[:3900]))
        self.add_item(c)


async def _econ_send(channel, embed, view=None):
    """Send an economy embed as a sleek V2 container (no sidebar). Falls back to
    a normal embed send if V2 delivery fails so a message is never dropped."""
    if view is not None:
        return await channel.send(view=view)
    title = getattr(embed, "title", "") or ""
    body = getattr(embed, "description", "") or ""
    try:
        return await channel.send(view=_EconLayout(title, body))
    except Exception as e:
        print(f"[Econ] V2 send failed, using embed: {e}")
        return await channel.send(embed=embed)


_ECON_DEFAULT_REPLIES = {
    "work": ["You worked a shift and earned {amt}.", "Hard work paid off — {amt}.",
             "You clocked in and made {amt}."],
    "slut": ["You worked the corner and made {amt}.", "Easy money — {amt}."],
    "crime": ["You pulled off the heist and got {amt}.", "Crime paid — {amt} this time."],
    "slut_fail": ["You got caught and paid a fine of {fine}.", "Bad night — fined {fine}."],
    "crime_fail": ["The cops caught you — fined {fine}.", "The heist flopped — fined {fine}."],
}


def _econ_cd_left(u, cmd):
    cd = int(gambling_config.get("cooldowns", {}).get(cmd, 0) or 0)
    last = int(u.get("cd", {}).get(cmd, 0) or 0)
    return max(0, cd - int(time.time() - last))


def _econ_cd_set(u, cmd):
    u.setdefault("cd", {})[cmd] = int(time.time())


def _fmt_dur(secs):
    secs = int(secs)
    h, m, s = secs // 3600, (secs % 3600) // 60, secs % 60
    return " ".join(x for x in (f"{h}h" if h else "", f"{m}m" if m else "", f"{s}s" if s and not h else "") if x) or "0s"


def _econ_payout(cmd):
    lo, hi = (gambling_config.get("payouts", {}).get(cmd) or [0, 0])[:2]
    return _rnd.randint(int(lo), max(int(lo), int(hi)))


def _econ_fine(cmd, cash):
    lo, hi = (gambling_config.get("fines", {}).get(cmd) or [0, 0])[:2]
    amt = _rnd.randint(int(lo), max(int(lo), int(hi)))
    if gambling_config.get("fine_type") == "percent":
        amt = int(cash * (amt / 100.0))
    return max(0, min(amt, cash))


async def _econ_earn(message, cmd):
    gid, uid = message.guild.id, message.author.id
    u = _econ_u(gid, uid)
    left = _econ_cd_left(u, cmd)
    if left > 0:
        await _econ_send(message.channel, error_embed("Slow down", f"You can `{gambling_config['prefix']}{cmd}` again in **{_fmt_dur(left)}**."))
        return
    _econ_cd_set(u, cmd)
    fail_rate = float(gambling_config.get("fail_rate", {}).get(cmd, 0) or 0)
    if cmd in ("slut", "crime") and _rnd.random() < fail_rate:
        fine = _econ_fine(cmd, int(u.get("cash", 0)))
        _econ_add(gid, uid, -fine)
        reply = _rnd.choice(_ECON_DEFAULT_REPLIES.get(f"{cmd}_fail", ["You failed and were fined {fine}."]))
        await _econ_send(message.channel, error_embed("Failed", reply.replace("{fine}", _econ_fmt(fine))))
        return
    amt = _econ_payout(cmd)
    _econ_add(gid, uid, amt)
    reply = _rnd.choice(_ECON_DEFAULT_REPLIES.get(cmd, ["You earned {amt}."]))
    await _econ_send(message.channel, success_embed(cmd.capitalize(), reply.replace("{amt}", _econ_fmt(amt))))


async def _econ_rob(message, args):
    if not message.mentions:
        await _econ_send(message.channel, error_embed("Rob who?", f"Use `{gambling_config['prefix']}rob @user`."))
        return
    target = message.mentions[0]
    if target.id == message.author.id or target.bot:
        await _econ_send(message.channel, error_embed("Nope", "Pick another member to rob."))
        return
    gid, uid = message.guild.id, message.author.id
    u = _econ_u(gid, uid)
    left = _econ_cd_left(u, "rob")
    if left > 0:
        await _econ_send(message.channel, error_embed("Slow down", f"You can rob again in **{_fmt_dur(left)}**."))
        return
    _econ_cd_set(u, "rob")
    tu = _econ_u(gid, target.id)
    if int(tu.get("cash", 0)) < 1:
        await _econ_send(message.channel, error_embed("Empty pockets", f"{target.display_name} has no cash on hand to rob."))
        return
    if _rnd.random() < float(gambling_config.get("rob_success_rate", 0.5)):
        stolen = _rnd.randint(1, int(tu["cash"]))
        _econ_add(gid, target.id, -stolen)
        _econ_add(gid, uid, stolen)
        await _econ_send(message.channel, success_embed("Robbery!", f"You robbed **{_econ_fmt(stolen)}** from {target.mention}."))
        await _econ_audit(message.guild, f"{message.author.mention} robbed {_econ_fmt(stolen)} from {target.mention}")
    else:
        fine = _econ_fine("crime", int(u.get("cash", 0))) or _rnd.randint(1, max(1, int(u.get("cash", 0)) or 1))
        _econ_add(gid, uid, -fine)
        await _econ_send(message.channel, error_embed("Caught!", f"You got caught and paid **{_econ_fmt(fine)}**."))


async def _econ_collect(message):
    gid, uid = message.guild.id, message.author.id
    u = _econ_u(gid, uid)
    now = int(time.time())
    total = 0
    # Role income.
    for ri in (gambling_config.get("role_income") or []):
        rid = str(ri.get("role_id") or "")
        if rid and any(str(r.id) == rid for r in message.author.roles):
            interval = max(60, int(ri.get("interval_minutes") or 60) * 60)
            periods = (now - int(u.get("inc_last", 0))) // interval if u.get("inc_last") else 1
            if periods > 0:
                total += int(ri.get("amount") or 0) * min(periods, 24)
    # Property income.
    props = {p["id"]: p for p in _econ_properties()}
    for pid, held in (u.get("props") or {}).items():
        p = props.get(pid)
        if not p:
            continue
        interval = max(60, int(p.get("interval_minutes") or 60) * 60)
        last = int(held.get("last", 0) or 0)
        periods = (now - last) // interval if last else 1
        if periods > 0:
            total += int(p.get("income") or 0) * int(held.get("n", 0)) * min(periods, 24)
            held["last"] = now
    if total <= 0:
        await _econ_send(message.channel, info_embed("Nothing to collect", "No role or property income is ready yet."))
        return
    u["inc_last"] = now
    _econ_add(gid, uid, total)
    await _econ_send(message.channel, success_embed("Income collected", f"You collected **{_econ_fmt(total)}**."))


async def _econ_leaderboard(message):
    gid = message.guild.id
    users = _econ_users(gid)
    # Auto-clean members who left the server.
    ranked = []
    for uid, u in list(users.items()):
        m = message.guild.get_member(int(uid)) if str(uid).isdigit() else None
        if m is None:
            users.pop(uid, None)
            continue
        ranked.append((uid, _econ_total(u)))
    _save_econ_soon()
    ranked.sort(key=lambda x: x[1], reverse=True)
    if not ranked:
        await _econ_send(message.channel, info_embed("Leaderboard", "No balances yet."))
        return
    lines = [f"**{i+1}.** <@{uid}> — {_econ_fmt(tot)}" for i, (uid, tot) in enumerate(ranked[:10])]
    await _econ_send(message.channel, info_embed(f"{message.guild.name} Leaderboard", "\n".join(lines)))


def _econ_require_bet(message, args):
    """Parse + validate a bet from the first arg. Returns the int amount, or None
    (after sending an error) if it's missing/too big/invalid."""
    gid, uid = message.guild.id, message.author.id
    cash = int(_econ_u(gid, uid)["cash"])
    if not args:
        return None
    amt = _econ_parse_amount(args[0], cash)
    if amt <= 0:
        return None
    if amt > cash:
        return "over"
    return amt


async def _econ_game_result(message, bet, won, payout, detail):
    gid, uid = message.guild.id, message.author.id
    if won:
        _econ_add(gid, uid, payout - bet)  # net gain
        e = success_embed("You won!", f"{detail}\n\nYou won **{_econ_fmt(payout - bet)}**.\nBalance: {_econ_fmt(_econ_u(gid, uid)['cash'])}")
    else:
        _econ_add(gid, uid, -bet)
        e = error_embed("You lost", f"{detail}\n\nYou lost **{_econ_fmt(bet)}**.\nBalance: {_econ_fmt(_econ_u(gid, uid)['cash'])}")
    await _econ_send(message.channel, e)


async def _econ_coinflip(message, args):
    bet = _econ_require_bet(message, args)
    p = gambling_config["prefix"]
    if bet is None:
        return await _econ_send(message.channel, error_embed("Usage", f"`{p}coinflip <amount> [heads|tails]`"))
    if bet == "over":
        return await _econ_send(message.channel, error_embed("Not enough cash", "You don't have that much."))
    call = (args[1].lower() if len(args) > 1 else "heads")
    call = "heads" if call.startswith("h") else "tails"
    flip = _rnd.choice(["heads", "tails"])
    await _econ_game_result(message, bet, flip == call, bet * 2, f"🪙 It landed **{flip}** (you called **{call}**).")


async def _econ_dice(message, args):
    bet = _econ_require_bet(message, args)
    p = gambling_config["prefix"]
    if bet is None:
        return await _econ_send(message.channel, error_embed("Usage", f"`{p}dice <amount>`"))
    if bet == "over":
        return await _econ_send(message.channel, error_embed("Not enough cash", "You don't have that much."))
    you, dealer = _rnd.randint(1, 6) + _rnd.randint(1, 6), _rnd.randint(1, 6) + _rnd.randint(1, 6)
    await _econ_game_result(message, bet, you > dealer, bet * 2, f"🎲 You rolled **{you}**, dealer rolled **{dealer}**.")


async def _econ_slots(message, args):
    bet = _econ_require_bet(message, args)
    p = gambling_config["prefix"]
    if bet is None:
        return await _econ_send(message.channel, error_embed("Usage", f"`{p}slots <amount>`"))
    if bet == "over":
        return await _econ_send(message.channel, error_embed("Not enough cash", "You don't have that much."))
    reel = ["🍒", "🍋", "🍊", "🍉", "⭐", "💎", "7️⃣"]
    spin = [_rnd.choice(reel) for _ in range(3)]
    line = " ".join(spin)
    if spin[0] == spin[1] == spin[2]:
        mult = 10 if spin[0] == "7️⃣" else (7 if spin[0] == "💎" else 5)
        await _econ_game_result(message, bet, True, bet * mult, f"**[ {line} ]** — three of a kind! ×{mult}")
    elif spin[0] == spin[1] or spin[1] == spin[2]:
        await _econ_game_result(message, bet, True, int(bet * 1.5), f"**[ {line} ]** — a pair! ×1.5")
    else:
        await _econ_game_result(message, bet, False, 0, f"**[ {line} ]** — no match.")


async def _econ_roulette(message, args):
    bet = _econ_require_bet(message, args)
    p = gambling_config["prefix"]
    if bet is None or len(args) < 2:
        return await _econ_send(message.channel, error_embed("Usage", f"`{p}roulette <amount> <red|black|even|odd|1-36>`"))
    if bet == "over":
        return await _econ_send(message.channel, error_embed("Not enough cash", "You don't have that much."))
    pick = args[1].lower()
    n = _rnd.randint(0, 36)
    reds = {1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36}
    colour = "green" if n == 0 else ("red" if n in reds else "black")
    won, payout = False, 0
    if pick.isdigit() and int(pick) == n:
        won, payout = True, bet * 36
    elif pick in ("red", "black") and pick == colour:
        won, payout = True, bet * 2
    elif pick in ("even", "odd") and n != 0 and (n % 2 == 0) == (pick == "even"):
        won, payout = True, bet * 2
    await _econ_game_result(message, bet, won, payout, f"🎡 The ball landed on **{n} ({colour})**.")


# ---- Blackjack (interactive) ----
def _bj_deal():
    return _rnd.randint(1, 13)


def _bj_val(cards):
    total, aces = 0, 0
    for c in cards:
        if c == 1:
            aces += 1; total += 11
        else:
            total += min(c, 10)
    while total > 21 and aces:
        total -= 10; aces -= 1
    return total


def _bj_show(cards, hide=False):
    names = {1: "A", 11: "J", 12: "Q", 13: "K"}
    faces = [names.get(c, str(c)) for c in cards]
    if hide:
        faces[-1] = "?"
    return " ".join(f"`{f}`" for f in faces)


# The house plays generously here: pushes go to the player, and on a straight
# loss (player didn't bust) there's a chance the dealer is pushed into a bust.
# Together with ties-to-player this lands the player win rate around ~60%.
BJ_LUCK = 0.30


class _BJRow(discord.ui.ActionRow):
    def __init__(self, view):
        super().__init__()
        self._bjv = view

    @discord.ui.button(label="Hit", style=discord.ButtonStyle.primary)
    async def hit(self, interaction, button):
        await self._bjv.on_hit(interaction)

    @discord.ui.button(label="Stand", style=discord.ButtonStyle.secondary)
    async def stand(self, interaction, button):
        await self._bjv.on_stand(interaction)


class BlackjackView(discord.ui.LayoutView):
    def __init__(self, gid, uid, bet):
        super().__init__(timeout=120)
        self.gid, self.uid, self.bet = gid, uid, bet
        self.player = [_bj_deal(), _bj_deal()]
        self.dealer = [_bj_deal(), _bj_deal()]
        self.done = False
        self._container = discord.ui.Container()  # no accent -> no sidebar line
        self._text = discord.ui.TextDisplay(self._body())
        self._row = _BJRow(self)
        self._container.add_item(self._text)
        self._container.add_item(self._row)
        self.add_item(self._container)

    def _body(self, reveal=False, result=None):
        d = _bj_show(self.dealer, hide=not reveal)
        p = _bj_show(self.player)
        dv = _bj_val(self.dealer) if reveal else "?"
        desc = (f"## Blackjack\n**Your hand** ({_bj_val(self.player)}): {p}\n"
                f"**Dealer** ({dv}): {d}\n\n**Bet:** {_econ_fmt(self.bet)}")
        if result:
            desc += f"\n\n{result}"
        return desc

    def _refresh(self, reveal=False, result=None):
        self._text.content = self._body(reveal=reveal, result=result)

    async def interaction_check(self, interaction):
        if interaction.user.id != self.uid:
            await interaction.response.send_message("Not your game.", ephemeral=True)
            return False
        return True

    async def on_hit(self, interaction):
        self.player.append(_bj_deal())
        if _bj_val(self.player) >= 21:
            return await self._finish(interaction)
        self._refresh()
        await interaction.response.edit_message(view=self)

    async def on_stand(self, interaction):
        await self._finish(interaction)

    async def _finish(self, interaction):
        self.done = True
        while _bj_val(self.dealer) < 17:
            self.dealer.append(_bj_deal())
        pv = _bj_val(self.player)
        if pv > 21:
            won, msg = False, "You busted."
        else:
            dv = _bj_val(self.dealer)
            if dv > 21 or pv > dv:
                won, msg = True, "You won! 🎉"
            elif pv == dv:
                won, msg = True, "Push goes to you — you win! 🎉"
            elif _rnd.random() < BJ_LUCK:
                # Luck of the table: nudge the dealer into a bust.
                while _bj_val(self.dealer) <= 21:
                    self.dealer.append(_bj_deal())
                won, msg = True, "The dealer overdrew and busted! 🎉"
            else:
                won, msg = False, "Dealer wins."
        _econ_add(self.gid, self.uid, self.bet if won else -self.bet)
        self._row.hit.disabled = True
        self._row.stand.disabled = True
        bal = _econ_u(self.gid, self.uid)["cash"]
        self._refresh(reveal=True, result=f"{msg}\nBalance: {_econ_fmt(bal)}")
        await interaction.response.edit_message(view=self)
        self.stop()


async def _econ_blackjack(message, args):
    bet = _econ_require_bet(message, args)
    p = gambling_config["prefix"]
    if bet is None:
        return await _econ_send(message.channel, error_embed("Usage", f"`{p}blackjack <amount>`"))
    if bet == "over":
        return await _econ_send(message.channel, error_embed("Not enough cash", "You don't have that much."))
    view = BlackjackView(message.guild.id, message.author.id, bet)
    # Natural blackjack pays 2.5x immediately.
    if _bj_val(view.player) == 21:
        _econ_add(message.guild.id, message.author.id, int(bet * 1.5))
        return await _econ_send(message.channel, success_embed("Blackjack!", f"Natural 21! You won **{_econ_fmt(int(bet*1.5))}**."))
    await message.channel.send(view=view)


# ---- Property shop (buy businesses that pay passive income) ----
def _econ_find_property(args):
    """Match a property by id or (partial) name from the command args."""
    props = _econ_properties()
    if not args:
        return None
    key = args[0].lower()
    for p in props:
        if p["id"] == key:
            return p
    q = " ".join(args).lower()
    return next((p for p in props if q == p["id"] or q in p["name"].lower()), None)


def _econ_cycle_text(p):
    return _fmt_dur(int(p.get("interval_minutes") or 60) * 60)


async def _econ_shop(message):
    p = gambling_config["prefix"]
    lines = []
    for prop in _econ_properties():
        lines.append(
            f"{prop['name']} — `{p}buy {prop['id']}`\n"
            f"　Price **{_econ_fmt(prop['price'])}** • Earns **{_econ_fmt(prop['income'])}**/{_econ_cycle_text(prop)}"
            + (f" • Max {prop['max']}" if prop.get('max') else "")
        )
    body = "\n\n".join(lines) + (
        f"\n\nBuy with `{p}buy <name>`, view yours with `{p}properties`, "
        f"and cash in earnings with `{p}collect`."
    )
    await _econ_send(message.channel, info_embed("🏪 Property Shop", body))


async def _econ_buy(message, args):
    p = gambling_config["prefix"]
    if not args:
        return await _econ_send(message.channel, error_embed("Usage", f"`{p}buy <name>` — browse `{p}shop`."))
    prop = _econ_find_property(args)
    if not prop:
        return await _econ_send(message.channel, error_embed("Not found", f"No property like that. See `{p}shop`."))
    gid, uid = message.guild.id, message.author.id
    u = _econ_u(gid, uid)
    held = u["props"].get(prop["id"]) or {"n": 0, "last": int(time.time())}
    mx = int(prop.get("max") or 0)
    if mx and int(held.get("n", 0)) >= mx:
        return await _econ_send(message.channel, error_embed("Maxed out", f"You already own the most ({mx}) of {prop['name']} you can."))
    price = int(prop["price"])
    if int(u["cash"]) < price:
        return await _econ_send(message.channel, error_embed("Not enough cash", f"{prop['name']} costs **{_econ_fmt(price)}** — you have {_econ_fmt(u['cash'])} on hand."))
    _econ_add(gid, uid, -price)
    held["n"] = int(held.get("n", 0)) + 1
    held.setdefault("last", int(time.time()))
    u["props"][prop["id"]] = held
    _save_econ_soon()
    await _econ_send(message.channel, success_embed(
        "Purchased",
        f"You bought **{prop['name']}** (now own ×{held['n']}). It earns "
        f"**{_econ_fmt(prop['income'])}** every {_econ_cycle_text(prop)} — cash in with `{p}collect`."))
    await _econ_audit(message.guild, f"{message.author.mention} bought {prop['name']} for {_econ_fmt(price)}")


async def _econ_sell(message, args):
    p = gambling_config["prefix"]
    if not args:
        return await _econ_send(message.channel, error_embed("Usage", f"`{p}sell <name>` — sells one back for half price."))
    prop = _econ_find_property(args)
    gid, uid = message.guild.id, message.author.id
    u = _econ_u(gid, uid)
    held = u["props"].get(prop["id"]) if prop else None
    if not prop or not held or int(held.get("n", 0)) <= 0:
        return await _econ_send(message.channel, error_embed("You don't own that", f"See what you own with `{p}properties`."))
    refund = int(int(prop["price"]) * 0.5)
    held["n"] = int(held.get("n", 0)) - 1
    if held["n"] <= 0:
        u["props"].pop(prop["id"], None)
    _econ_add(gid, uid, refund)
    _save_econ_soon()
    await _econ_send(message.channel, success_embed("Sold", f"You sold one **{prop['name']}** for **{_econ_fmt(refund)}**."))


async def _econ_portfolio(message):
    p = gambling_config["prefix"]
    gid, uid = message.guild.id, message.author.id
    u = _econ_u(gid, uid)
    owned = {pid: h for pid, h in (u.get("props") or {}).items() if int(h.get("n", 0)) > 0}
    if not owned:
        return await _econ_send(message.channel, info_embed("Your properties", f"You don't own any yet — browse the `{p}shop`."))
    props = {pr["id"]: pr for pr in _econ_properties()}
    lines, per_cycle = [], 0
    for pid, h in owned.items():
        prop = props.get(pid)
        if not prop:
            continue
        n = int(h.get("n", 0))
        rate = int(prop["income"]) * n
        per_cycle += rate
        lines.append(f"{prop['name']} ×{n} — **{_econ_fmt(rate)}**/{_econ_cycle_text(prop)}")
    body = "\n".join(lines) + f"\n\n**Total income:** {_econ_fmt(per_cycle)} per cycle\nCash in with `{p}collect`."
    await _econ_send(message.channel, info_embed("🏢 Your Properties", body))


# ---- More casino games: poker, baccarat, higher-lower, wheel ----
_CARD_SUITS = ["♠", "♥", "♦", "♣"]
_CARD_RANKS = {1: "A", 11: "J", 12: "Q", 13: "K"}


def _card_name(card):
    r, s = card
    return f"{_CARD_RANKS.get(r, str(r))}{_CARD_SUITS[s]}"


def _fresh_deck():
    deck = [(r, s) for s in range(4) for r in range(1, 14)]
    _rnd.shuffle(deck)
    return deck


def _poker_eval(cards):
    """Evaluate a 5-card hand (Jacks-or-Better paytable). Returns (name, mult)."""
    ranks = sorted(c[0] for c in cards)
    suits = [c[1] for c in cards]
    counts = {}
    for r in ranks:
        counts[r] = counts.get(r, 0) + 1
    by_count = sorted(counts.values(), reverse=True)
    flush = len(set(suits)) == 1
    uniq = sorted(set(ranks))
    straight = len(uniq) == 5 and (uniq[-1] - uniq[0] == 4)
    # Ace-low straight (A,2,3,4,5) and Ace-high (10,J,Q,K,A).
    if set(ranks) == {1, 10, 11, 12, 13}:
        straight = True
    if flush and set(ranks) == {1, 10, 11, 12, 13}:
        return ("Royal Flush", 250)
    if flush and straight:
        return ("Straight Flush", 50)
    if by_count[0] == 4:
        return ("Four of a Kind", 25)
    if by_count[0] == 3 and by_count[1] == 2:
        return ("Full House", 9)
    if flush:
        return ("Flush", 6)
    if straight:
        return ("Straight", 4)
    if by_count[0] == 3:
        return ("Three of a Kind", 3)
    if by_count[0] == 2 and by_count[1] == 2:
        return ("Two Pair", 2)
    if by_count[0] == 2:
        pair_rank = next(r for r, n in counts.items() if n == 2)
        if pair_rank == 1 or pair_rank >= 11:  # Jacks or better (A,J,Q,K)
            return ("Jacks or Better", 1)
    return ("No win", 0)


class _PokerHoldRow(discord.ui.ActionRow):
    def __init__(self, view):
        super().__init__()
        self._pv = view

    async def _toggle(self, interaction, idx):
        await self._pv.toggle_hold(interaction, idx)

    @discord.ui.button(label="1")
    async def h0(self, i, b): await self._toggle(i, 0)
    @discord.ui.button(label="2")
    async def h1(self, i, b): await self._toggle(i, 1)
    @discord.ui.button(label="3")
    async def h2(self, i, b): await self._toggle(i, 2)
    @discord.ui.button(label="4")
    async def h3(self, i, b): await self._toggle(i, 3)
    @discord.ui.button(label="5")
    async def h4(self, i, b): await self._toggle(i, 4)


class _PokerDrawRow(discord.ui.ActionRow):
    def __init__(self, view):
        super().__init__()
        self._pv = view

    @discord.ui.button(label="Draw", style=discord.ButtonStyle.success)
    async def draw(self, i, b): await self._pv.do_draw(i)


class PokerView(discord.ui.LayoutView):
    def __init__(self, gid, uid, bet):
        super().__init__(timeout=120)
        self.gid, self.uid, self.bet = gid, uid, bet
        self.deck = _fresh_deck()
        self.cards = [self.deck.pop() for _ in range(5)]
        self.held = [False] * 5
        self.done = False
        self._container = discord.ui.Container()
        self._text = discord.ui.TextDisplay(self._body())
        self._hold_row = _PokerHoldRow(self)
        self._draw_row = _PokerDrawRow(self)
        self._container.add_item(self._text)
        self._container.add_item(self._hold_row)
        self._container.add_item(self._draw_row)
        self.add_item(self._container)

    def _hand_str(self):
        return "  ".join(
            (f"**[{_card_name(c)}]**" if self.held[i] else f"`{_card_name(c)}`")
            for i, c in enumerate(self.cards)
        )

    def _body(self, result=None):
        desc = (f"## 🃏 Video Poker\n{self._hand_str()}\n\n"
                f"**Bet:** {_econ_fmt(self.bet)}")
        if result is None:
            desc += "\n\nTap **1–5** to hold cards, then **Draw**. (Jacks or better pays.)"
        else:
            desc += f"\n\n{result}"
        return desc

    def _refresh(self, result=None):
        self._text.content = self._body(result=result)

    async def interaction_check(self, interaction):
        if interaction.user.id != self.uid:
            await interaction.response.send_message("Not your game.", ephemeral=True)
            return False
        return True

    async def toggle_hold(self, interaction, idx):
        if self.done:
            return await interaction.response.defer()
        self.held[idx] = not self.held[idx]
        btn = self._hold_row.children[idx]
        btn.style = discord.ButtonStyle.primary if self.held[idx] else discord.ButtonStyle.secondary
        self._refresh()
        await interaction.response.edit_message(view=self)

    async def do_draw(self, interaction):
        if self.done:
            return await interaction.response.defer()
        self.done = True
        for i in range(5):
            if not self.held[i]:
                self.cards[i] = self.deck.pop()
        name, mult = _poker_eval(self.cards)
        net = self.bet * mult - self.bet
        _econ_add(self.gid, self.uid, net)
        if mult >= 2:
            res = f"**{name}!** You won **{_econ_fmt(self.bet * mult - self.bet)}** (×{mult})."
        elif mult == 1:
            res = f"**{name}** — your bet is returned."
        else:
            res = f"**{name}.** You lost **{_econ_fmt(self.bet)}**."
        res += f"\nBalance: {_econ_fmt(_econ_u(self.gid, self.uid)['cash'])}"
        for row in (self._hold_row, self._draw_row):
            for c in row.children:
                c.disabled = True
        self._refresh(result=res)
        await interaction.response.edit_message(view=self)
        self.stop()


async def _econ_poker(message, args):
    bet = _econ_require_bet(message, args)
    p = gambling_config["prefix"]
    if bet is None:
        return await _econ_send(message.channel, error_embed("Usage", f"`{p}poker <amount>`"))
    if bet == "over":
        return await _econ_send(message.channel, error_embed("Not enough cash", "You don't have that much."))
    view = PokerView(message.guild.id, message.author.id, bet)
    await message.channel.send(view=view)


def _bacc_val(cards):
    return sum(min(c[0], 10) % 10 if c[0] != 1 else 1 for c in cards) % 10


async def _econ_baccarat(message, args):
    p = gambling_config["prefix"]
    bet = _econ_require_bet(message, args)
    if bet is None or len(args) < 2:
        return await _econ_send(message.channel, error_embed("Usage", f"`{p}baccarat <amount> <player|banker|tie>`"))
    if bet == "over":
        return await _econ_send(message.channel, error_embed("Not enough cash", "You don't have that much."))
    pick = args[1].lower()
    pick = "player" if pick.startswith("p") else ("banker" if pick.startswith("b") else ("tie" if pick.startswith("t") else pick))
    if pick not in ("player", "banker", "tie"):
        return await _econ_send(message.channel, error_embed("Usage", f"Bet on **player**, **banker**, or **tie**."))
    deck = _fresh_deck()
    ph = [deck.pop(), deck.pop()]
    bh = [deck.pop(), deck.pop()]
    pv, bv = _bacc_val(ph), _bacc_val(bh)
    # Simplified natural rules: a third card to whoever is under 6 and no natural.
    if max(pv, bv) < 8:
        if pv <= 5:
            ph.append(deck.pop()); pv = _bacc_val(ph)
        if bv <= 5:
            bh.append(deck.pop()); bv = _bacc_val(bh)
    outcome = "player" if pv > bv else ("banker" if bv > pv else "tie")
    detail = (f"🎴 Player: {' '.join('`'+_card_name(c)+'`' for c in ph)} = **{pv}**\n"
              f"🎴 Banker: {' '.join('`'+_card_name(c)+'`' for c in bh)} = **{bv}**\n"
              f"Result: **{outcome.capitalize()}**")
    if pick == outcome:
        mult = 8 if outcome == "tie" else 2
        await _econ_game_result(message, bet, True, bet * mult, detail)
    else:
        await _econ_game_result(message, bet, False, 0, detail)


async def _econ_highlow(message, args):
    p = gambling_config["prefix"]
    bet = _econ_require_bet(message, args)
    if bet is None or len(args) < 2:
        return await _econ_send(message.channel, error_embed("Usage", f"`{p}highlow <amount> <high|low>` — will the next card beat the first?"))
    if bet == "over":
        return await _econ_send(message.channel, error_embed("Not enough cash", "You don't have that much."))
    call = "high" if args[1].lower().startswith("h") else "low"
    deck = _fresh_deck()
    first, second = deck.pop(), deck.pop()
    a, b = (14 if first[0] == 1 else first[0]), (14 if second[0] == 1 else second[0])
    detail = f"🃏 First card **`{_card_name(first)}`**, next card **`{_card_name(second)}`** (you called **{call}**)."
    if b == a:
        # Tie returns the bet.
        await _econ_send(message.channel, info_embed("Push", f"{detail}\n\nA tie — your bet is returned."))
        return
    won = (b > a) if call == "high" else (b < a)
    await _econ_game_result(message, bet, won, bet * 2, detail)


async def _econ_wheel(message, args):
    p = gambling_config["prefix"]
    bet = _econ_require_bet(message, args)
    if bet is None:
        return await _econ_send(message.channel, error_embed("Usage", f"`{p}wheel <amount>`"))
    if bet == "over":
        return await _econ_send(message.channel, error_embed("Not enough cash", "You don't have that much."))
    # Weighted wheel — mostly small outcomes, rare big multipliers. Weights are
    # tuned to ~0.98 expected return (a small house edge) so it stays sustainable.
    segments = [(0, 38), (0.5, 24), (1.5, 18), (2, 12), (3, 5), (5, 2), (10, 1)]
    pool = [m for m, w in segments for _ in range(w)]
    mult = _rnd.choice(pool)
    detail = f"🎡 The wheel landed on **×{mult}**."
    if mult >= 1:
        await _econ_game_result(message, bet, True, int(bet * mult), detail)
    else:
        await _econ_game_result(message, bet, False, 0, detail)


async def _econ_crash(message, args):
    p = gambling_config["prefix"]
    bet = _econ_require_bet(message, args)
    if bet is None or len(args) < 2:
        return await _econ_send(message.channel, error_embed("Usage", f"`{p}crash <amount> <cashout, e.g. 2.0>`"))
    if bet == "over":
        return await _econ_send(message.channel, error_embed("Not enough cash", "You don't have that much."))
    try:
        target = float(args[1].lstrip("x×"))
    except Exception:
        return await _econ_send(message.channel, error_embed("Usage", f"Pick a cash-out like `{p}crash 100 2.0`."))
    target = max(1.01, min(target, 100.0))
    # Crash point with ~5% house edge: P(crash >= x) = 0.95 / x.
    r = _rnd.random()
    crash = min(1000.0, max(1.0, 0.95 / (1 - r))) if r < 0.9999 else 1000.0
    won = crash >= target
    detail = f"🚀 You set **×{target:.2f}** — the rocket crashed at **×{crash:.2f}**."
    await _econ_game_result(message, bet, won, int(bet * target), detail)


async def _econ_scratch(message, args):
    p = gambling_config["prefix"]
    bet = _econ_require_bet(message, args)
    if bet is None:
        return await _econ_send(message.channel, error_embed("Usage", f"`{p}scratch <amount>`"))
    if bet == "over":
        return await _econ_send(message.channel, error_embed("Not enough cash", "You don't have that much."))
    syms = ["🍒", "🔔", "⭐", "💎", "7️⃣", "🍀"]
    grid = [_rnd.choice(syms) for _ in range(3)]
    line = " ".join(grid)
    distinct = len(set(grid))
    if distinct == 1:  # three of a kind
        await _econ_game_result(message, bet, True, bet * 10, f"🎫 **[ {line} ]** — jackpot! ×10")
    elif distinct == 2:  # exactly a pair
        await _econ_game_result(message, bet, True, int(bet * 1.5), f"🎫 **[ {line} ]** — a pair! ×1.5")
    else:
        await _econ_game_result(message, bet, False, 0, f"🎫 **[ {line} ]** — no match.")


async def _econ_help(message):
    p = gambling_config["prefix"]
    body = (
        f"**💰 Economy**\n"
        f"`{p}balance` · `{p}give @user <amt>` · `{p}deposit <amt>` · `{p}withdraw <amt>` · `{p}leaderboard`\n"
        f"`{p}work` · `{p}slut` · `{p}crime` · `{p}rob @user` · `{p}collect`\n\n"
        f"**🏪 Property**\n"
        f"`{p}shop` · `{p}buy <name>` · `{p}sell <name>` · `{p}properties`\n\n"
        f"**🎰 Casino**\n"
        f"`{p}blackjack <amt>` · `{p}poker <amt>` · `{p}roulette <amt> <bet>` · `{p}baccarat <amt> <p|b|tie>`\n"
        f"`{p}slots <amt>` · `{p}dice <amt>` · `{p}coinflip <amt> <h|t>` · `{p}highlow <amt> <high|low>`\n"
        f"`{p}wheel <amt>` · `{p}crash <amt> <cashout>` · `{p}scratch <amt>`"
    )
    await _econ_send(message.channel, info_embed("📖 Economy & Casino Commands", body))


async def _econ_dispatch(message, cmd, args):
    # Never run commands until balances/properties have loaded — otherwise a user
    # would be acting on an empty in-memory economy that a pending reload will
    # replace, and their changes would be lost. Blocking is the safe choice.
    if not _econ_loaded:
        await _econ_send(message.channel, error_embed(
            "One moment", "The economy is still syncing — try again in a few seconds."))
        return
    gid, uid = message.guild.id, message.author.id
    p = gambling_config["prefix"]
    if cmd in ("money", "bal", "balance", "cash"):
        target = message.mentions[0] if message.mentions else message.author
        u = _econ_u(gid, target.id)
        ranked = sorted(((k, _econ_total(v)) for k, v in _econ_users(gid).items()), key=lambda x: x[1], reverse=True)
        rank = next((i + 1 for i, (k, _v) in enumerate(ranked) if k == str(target.id)), "—")
        e = info_embed(f"{target.display_name}'s balance",
                       f"**Cash:** {_econ_fmt(u['cash'])}\n**Bank:** {_econ_fmt(u['bank'])}\n"
                       f"**Total:** {_econ_fmt(_econ_total(u))}\n**Rank:** #{rank}")
        await _econ_send(message.channel, e)
    elif cmd in ("give-money", "give", "pay"):
        if not message.mentions or not args:
            await _econ_send(message.channel, error_embed("Usage", f"`{p}give @user amount`")); return
        target = message.mentions[0]
        amt = _econ_parse_amount(args[-1], _econ_u(gid, uid)["cash"])
        if amt <= 0 or amt > _econ_u(gid, uid)["cash"]:
            await _econ_send(message.channel, error_embed("Not enough cash", "You don't have that much on hand.")); return
        _econ_add(gid, uid, -amt); _econ_add(gid, target.id, amt)
        await _econ_send(message.channel, success_embed("Sent", f"You gave **{_econ_fmt(amt)}** to {target.mention}."))
    elif cmd in ("deposit", "dep"):
        u = _econ_u(gid, uid)
        amt = _econ_parse_amount(args[0] if args else "all", u["cash"])
        amt = min(amt, u["cash"])
        if amt <= 0:
            await _econ_send(message.channel, error_embed("Nothing to deposit", "You have no cash on hand.")); return
        u["cash"] -= amt; u["bank"] += amt; _save_econ_soon()
        await _econ_send(message.channel, success_embed("Deposited", f"Moved **{_econ_fmt(amt)}** to your bank."))
    elif cmd in ("withdraw", "with"):
        u = _econ_u(gid, uid)
        amt = _econ_parse_amount(args[0] if args else "all", u["bank"])
        amt = min(amt, u["bank"])
        if amt <= 0:
            await _econ_send(message.channel, error_embed("Nothing to withdraw", "Your bank is empty.")); return
        u["bank"] -= amt; u["cash"] += amt; _save_econ_soon()
        await _econ_send(message.channel, success_embed("Withdrew", f"Moved **{_econ_fmt(amt)}** to your cash."))
    elif cmd in ("work", "slut", "crime"):
        await _econ_earn(message, cmd)
    elif cmd == "rob":
        await _econ_rob(message, args)
    elif cmd in ("coinflip", "cf", "bet", "gamble"):
        await _econ_coinflip(message, args)
    elif cmd in ("dice", "roll"):
        await _econ_dice(message, args)
    elif cmd in ("slots", "slot"):
        await _econ_slots(message, args)
    elif cmd in ("roulette", "rl"):
        await _econ_roulette(message, args)
    elif cmd in ("blackjack", "bj"):
        await _econ_blackjack(message, args)
    elif cmd in ("poker", "videopoker"):
        await _econ_poker(message, args)
    elif cmd in ("baccarat", "bacc"):
        await _econ_baccarat(message, args)
    elif cmd in ("highlow", "hl", "higherlower"):
        await _econ_highlow(message, args)
    elif cmd in ("wheel", "spin"):
        await _econ_wheel(message, args)
    elif cmd in ("crash", "rocket"):
        await _econ_crash(message, args)
    elif cmd in ("scratch", "scratchcard"):
        await _econ_scratch(message, args)
    elif cmd in ("shop", "store"):
        await _econ_shop(message)
    elif cmd in ("buy", "buy-property", "buyproperty"):
        await _econ_buy(message, args)
    elif cmd in ("sell", "sell-property", "sellproperty"):
        await _econ_sell(message, args)
    elif cmd in ("properties", "props", "portfolio", "business", "businesses"):
        await _econ_portfolio(message)
    elif cmd in ("help", "commands", "economy-help", "eco-help"):
        await _econ_help(message)
    elif cmd in ("collect-income", "collect", "collectincome"):
        await _econ_collect(message)
    elif cmd in ("leaderboard", "lb", "rich"):
        await _econ_leaderboard(message)
    elif cmd in ("add-money", "addmoney", "remove-money", "removemoney"):
        if not _econ_is_admin(message.author):
            await _econ_send(message.channel, error_embed("No permission", "Admins only.")); return
        if not message.mentions or not args:
            await _econ_send(message.channel, error_embed("Usage", f"`{p}{cmd} @user amount`")); return
        target = message.mentions[0]
        amt = _econ_parse_amount(args[-1], 10**12)
        sign = -1 if "remove" in cmd else 1
        _econ_add(gid, target.id, sign * amt)
        await _econ_send(message.channel, success_embed("Done", f"{'Removed' if sign<0 else 'Added'} **{_econ_fmt(amt)}** {'from' if sign<0 else 'to'} {target.mention}."))
        await _econ_audit(message.guild, f"{message.author.mention} {'removed' if sign<0 else 'added'} {_econ_fmt(amt)} {'from' if sign<0 else 'to'} {target.mention}")
    elif cmd in ("reset-money", "resetmoney", "reset-economy", "reseteconomy"):
        if not _econ_is_admin(message.author):
            await _econ_send(message.channel, error_embed("No permission", "Admins only.")); return
        _econ_users(gid).clear(); await _econ_flush_now()
        await _econ_send(message.channel, success_embed("Reset", "Everyone's balance has been reset."))
    elif cmd in ("economy-stats", "economystats", "eco-stats"):
        users = _econ_users(gid)
        circ = sum(_econ_total(u) for u in users.values())
        await _econ_send(message.channel, info_embed("Economy stats",
            f"**Accounts:** {len(users)}\n**Total in circulation:** {_econ_fmt(circ)}\n"
            f"**Currency:** {_econ_sym()} {gambling_config.get('currency_name')}"))


def _econ_parse_amount(tok, ceiling):
    tok = str(tok or "").strip().lower().replace(",", "")
    if tok in ("all", "max"):
        return int(ceiling)
    if tok in ("half",):
        return int(ceiling) // 2
    mult = 1
    if tok.endswith("k"): mult, tok = 1_000, tok[:-1]
    elif tok.endswith("m"): mult, tok = 1_000_000, tok[:-1]
    elif tok.endswith("b"): mult, tok = 1_000_000_000, tok[:-1]
    try:
        return max(0, int(float(tok) * mult))
    except Exception:
        return 0


@bot.event
async def on_message(message):
    # Economy / gambling prefix commands.
    if (gambling_config.get("enabled") and message.guild and not message.author.bot
            and message.content.startswith(gambling_config.get("prefix") or "!")):
        allowed = [str(c) for c in (gambling_config.get("allowed_channel_ids") or []) if c]
        # If channels are locked, silently ignore commands anywhere else.
        if allowed and str(message.channel.id) not in allowed:
            pass
        else:
            try:
                parts = message.content[len(gambling_config["prefix"]):].strip().split()
                if parts:
                    await _econ_dispatch(message, parts[0].lower(), parts[1:])
            except Exception as e:
                print(f"[Econ] command error: {e}")
        # fall through so other on_message logic (TTS etc.) still runs below
    # TTS: if I'm /join'd into this voice channel, read its chat aloud.
    if (not message.author.bot and message.guild
            and _tts_channels.get(message.guild.id) == message.channel.id):
        try:
            await _tts_handle(message)
        except Exception as e:
            print(f"[TTS] handle error: {e}")
        return
    # Ticket text commands work only inside a ticket channel; everything else
    # falls through to the normal command processor.
    if not message.author.bot and message.guild:
        parts = (message.content or "").strip().split(maxsplit=1)
        cmd = parts[0].lower() if parts else ""
        if cmd == "-reroll":
            await _cmd_reroll(message)
            return
        if cmd == "-inactive":
            topic = getattr(message.channel, "topic", "") or ""
            if not topic.startswith("ticket|"):
                await message.channel.send(embed=error_embed("Not a ticket", "This command only works inside a ticket channel."), delete_after=10)
                return
            if not _is_ticket_staff(message.author, message.channel):
                await message.channel.send(embed=error_embed("No permission", "Only staff can change a ticket's inactivity check."), delete_after=10)
                return
            arg = (parts[1] if len(parts) > 1 else "").strip().lower()
            wid = str(message.channel.id)
            try:
                await message.delete()
            except Exception:
                pass
            if arg in ("hold", "off", "pause", "stop"):
                _ticket_ac_hold.add(wid)
                _ticket_warned.pop(wid, None)  # clear a pending warning too
                await _save_ticket_autoclose()
                await message.channel.send(embed=success_embed(
                    "Inactivity check held",
                    "This ticket won't get inactivity warnings or auto-close. Run `-inactive resume` to turn it back on."))
            elif arg in ("resume", "on", "start"):
                _ticket_ac_hold.discard(wid)
                await _save_ticket_autoclose()
                await message.channel.send(embed=success_embed(
                    "Inactivity check resumed",
                    "This ticket is back on the normal inactivity timer."))
            else:
                held = wid in _ticket_ac_hold
                await message.channel.send(embed=info_embed(
                    "Inactivity check",
                    f"Currently **{'held' if held else 'active'}** for this ticket.\n"
                    "`-inactive hold` — stop inactivity warnings/auto-close here\n"
                    "`-inactive resume` — turn them back on"), delete_after=20)
            return
        if cmd in ("-claim", "-unclaim", "-close"):
            topic = getattr(message.channel, "topic", "") or ""
            if topic.startswith("ticket|"):
                if cmd == "-close":
                    await _cmd_close(message, parts[1] if len(parts) > 1 else "")
                else:
                    try:
                        await message.delete()
                    except Exception:
                        pass
                    await _cmd_claim(message, cmd == "-claim")
                return
            else:
                await message.channel.send(embed=error_embed("Not a ticket", "This command only works inside a ticket channel."), delete_after=10)
                return
    await bot.process_commands(message)


@bot.event
async def on_command_error(ctx, error):
    # Every prefix message is handled by our own dispatchers (economy `!…`,
    # ticket `-…`) or as a slash command; process_commands still runs afterward,
    # so a plain "command not found" is expected noise — swallow just that.
    if isinstance(error, commands.CommandNotFound):
        return
    print(f"[Command] error: {error}")


# Preferred: a single form (modal) with the Instant/Manual dropdown inside it.
# Dropdowns inside modals require discord.py 2.6+ (discord.ui.Label). Where the
# runtime supports it this is what the user sees; otherwise ticket_close_prompt
# falls back to the plain-dropdown flow below so it can never time out.
class CloseOrderModal(discord.ui.Modal):
    def __init__(self):
        super().__init__(title="Close", timeout=600)
        self.close_type = discord.ui.Select(min_values=1, max_values=1, options=[
            discord.SelectOption(label="Instant Close", value="instant", default=True, description="Close this order right now"),
            discord.SelectOption(label="Manual Close", value="request", description="Ask the opener to confirm first"),
        ])
        # A TextInput wrapped in a Label must NOT carry its own label — the
        # Label provides it. Setting both makes Discord reject the modal
        # (error 50035: "Cannot set label on a TextInput in a Label component").
        self.reason = discord.ui.TextInput(
            style=discord.TextStyle.paragraph, required=False,
            max_length=500, placeholder="Reason for closing (optional)",
        )
        self.add_item(discord.ui.Label(text="Close Type", component=self.close_type))
        self.add_item(discord.ui.Label(text="Reason", component=self.reason))

    async def on_submit(self, interaction):
        mode = self.close_type.values[0] if self.close_type.values else "instant"
        reason = (self.reason.value or "").strip() or "No reason provided."
        if mode == "request":
            await do_request_close(interaction, reason)
        else:
            await do_instant_close(interaction, reason)


# Fallback path: a plain-message dropdown, then a text-only reason box. Used only
# when the single form above isn't supported by the running discord.py build.
class CloseReasonModal(discord.ui.Modal):
    def __init__(self, mode):
        super().__init__(title="Close", timeout=600)
        self.mode = mode
        self.reason = discord.ui.TextInput(
            label="Reason", style=discord.TextStyle.paragraph, required=False,
            max_length=500, placeholder="Reason for closing (optional)",
        )
        self.add_item(self.reason)

    async def on_submit(self, interaction):
        reason = (self.reason.value or "").strip() or "No reason provided."
        if self.mode == "request":
            await do_request_close(interaction, reason)
        else:
            await do_instant_close(interaction, reason)


def _close_type_view():
    view = discord.ui.View(timeout=300)
    view.add_item(discord.ui.Select(
        custom_id="ticket_closetype", placeholder="Choose how to close…",
        min_values=1, max_values=1, options=[
            discord.SelectOption(label="Instant Close", value="instant", description="Close this order right now"),
            discord.SelectOption(label="Manual Close", value="request", description="Ask the opener to confirm first"),
        ],
    ))
    return view


async def ticket_close_prompt(interaction):
    topic = getattr(interaction.channel, "topic", "") or ""
    if not topic.startswith("ticket|"):
        await interaction.response.send_message(embed=error_embed("Not a ticket", "This isn't a ticket channel."), ephemeral=True)
        return
    # Try the single form first. If this runtime can't build a dropdown-in-modal,
    # the modal is never sent (send_modal raises before acking), so we fall back
    # to the plain dropdown instead of leaving the click unanswered.
    if hasattr(discord.ui, "Label"):
        try:
            await interaction.response.send_modal(CloseOrderModal())
            return
        except Exception as e:
            print(f"[Ticket] single-form close modal unavailable ({e}); using dropdown fallback")
    await interaction.response.send_message(
        embed=info_embed("Close", "Choose how you'd like to close this order."),
        view=_close_type_view(), ephemeral=True,
    )


async def do_instant_close(interaction, reason):
    channel = interaction.channel
    await _ui_channel_or_embed(
        interaction, "ticket_closing",
        {"user": interaction.user.mention, "reason": reason or "\u2014"},
        "Closing order",
        f"Closed by {interaction.user.mention}\n**Reason:** {reason}\nSaving transcript\u2026")
    await _do_close(channel, interaction.guild, interaction.user, reason)


async def do_request_close(interaction, reason):
    topic = getattr(interaction.channel, "topic", "") or ""
    opener_id = topic.split("|")[1] if topic.startswith("ticket|") and len(topic.split("|")) > 1 else ""
    mention = f"<@{opener_id}>" if opener_id.isdigit() else ""
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label="Confirm Close", style=discord.ButtonStyle.danger, custom_id="ticket_close_confirm"))
    confirm_btn = {"type": 2, "style": 4, "label": "Confirm Close", "custom_id": "ticket_close_confirm"}
    await _ui_channel_or_embed(
        interaction, "ticket_close_request",
        {"user": interaction.user.mention, "reason": reason or "—"},
        "Close requested",
        f"{interaction.user.mention} requested to close this order.\n**Reason:** {reason}\n\nThe opener or staff can confirm below.",
        buttons=[confirm_btn], content=(mention or None), fallback_view=view)


async def build_transcript(channel):
    lines = [f"Transcript for #{channel.name}", ""]
    try:
        async for msg in channel.history(limit=500, oldest_first=True):
            stamp = msg.created_at.strftime("%Y-%m-%d %H:%M")
            content = msg.content or ""
            for a in msg.attachments:
                content += f" [attachment: {a.url}]"
            lines.append(f"[{stamp}] {msg.author}: {content}")
    except Exception as e:
        lines.append(f"(transcript error: {e})")
    return "\n".join(lines)


async def record_ticket(guild_id, channel_id, opener_id, category, status):
    await runtime_rpc("runtime_credits_op", {
        "_token": WORKER_TOKEN, "_bot_id": BOT_ORDER_ID, "_op": "ticket_log",
        "_payload": {"guild_id": str(guild_id), "channel_id": str(channel_id), "opener_id": str(opener_id), "category": category, "status": status},
    })


_V2_LAST_ERROR = {"msg": ""}


def _strip_galleries(items):
    """Return a copy of a V2 item tree with all media galleries removed.

    Expired / signed attachment URLs (media.discordapp.net/... ?ex=&is=&hm=)
    are the usual reason Discord rejects a Components V2 message, so dropping
    galleries lets the rest of the design still post."""
    out = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        t = it.get("type", "")
        if t in ("gallery", "media_gallery", "media"):
            continue
        it = dict(it)
        if isinstance(it.get("children"), list):
            it["children"] = _strip_galleries(it["children"])
        out.append(it)
    return out


def _build_v2(comp, guild):
    """Convert one dashboard V2 item into a raw Discord Components-V2 object.
    Module-level so both send_v2_message and the giveaway renderer can use it."""
    ctype = comp.get("type", "")
    if ctype in ("text", "text_display"):
        text = comp.get("text") or comp.get("content", "")
        title = comp.get("title", "")
        if title:
            text = f"**{title}**\n{text}" if text else f"**{title}**"
        return {"type": 10, "content": _render_guild_text(text, guild)} if text else None
    if ctype == "container":
        accent = comp.get("accentColor") or comp.get("accent_color", "")
        try:
            accent_int = int(str(accent).lstrip("#"), 16) if accent else None
        except Exception:
            accent_int = None
        children = [_build_v2(c, guild) for c in comp.get("children", [])]
        children = [c for c in children if c]
        if not children:
            return None
        obj = {"type": 17, "components": children}
        if accent_int is not None:
            obj["accent_color"] = accent_int
        return obj
    if ctype == "separator":
        spacing = comp.get("spacing", "small")
        return {"type": 14, "divider": comp.get("divider", True), "spacing": 2 if spacing == "large" else 1}
    if ctype in ("gallery", "media_gallery", "media"):
        urls = comp.get("images") or comp.get("image_urls", [])
        items = [{"media": {"url": u}} for u in urls if u and str(u).startswith("http")]
        return {"type": 12, "items": items} if items else None
    if ctype == "section":
        text = comp.get("text") or comp.get("content", "")
        title = comp.get("title", "")
        if title:
            text = f"**{title}**\n{text}" if text else f"**{title}**"
        if not text:
            return None
        text = _render_guild_text(text, guild)
        thumb = comp.get("thumbnailUrl") or comp.get("thumbnail_url")
        button = comp.get("button")
        accessory = None
        if thumb and str(thumb).startswith("http"):
            accessory = {"type": 11, "media": {"url": thumb}}
        elif isinstance(button, dict) and button.get("label"):
            accessory = build_button(button, guild)
        # A Components V2 Section (type 9) REQUIRES an accessory (thumbnail or
        # button). If the design has neither, Discord rejects the whole
        # message, so render the text as a plain text display instead.
        if accessory is None:
            return {"type": 10, "content": text}
        return {"type": 9, "components": [{"type": 10, "content": text}], "accessory": accessory}
    if ctype == "purchase":
        cfg = _purchase_cfg_from(comp)
        key = _comp_key(comp)
        title = cfg["title"]
        price = cfg["price"]
        text = f"**{title}**" + (f"\n{price}" if price else "")
        text = _render_guild_text(text, guild)
        if cfg.get("quantity"):
            # Display card — the button is an unclickable badge (no purchase action).
            label = _render_guild_text(cfg["button_label"] or "Quantity", guild)
            btn = {"type": 2, "style": 2, "label": (label or "Quantity")[:80],
                   "custom_id": f"noop:{key}"[:100], "disabled": True}
        else:
            purchase_msgs[key] = cfg
            btn = {"type": 2, "style": 2, "label": (cfg["button_label"] or "Purchase")[:80], "custom_id": f"purchase:{key}"}
        return {"type": 9, "components": [{"type": 10, "content": text}], "accessory": btn}
    if ctype in ("buttonRow", "button_row", "buttons", "action_row"):
        buttons = [build_button(b, guild) for b in comp.get("buttons", [])]
        buttons = [b for b in buttons if b]
        return {"type": 1, "components": buttons} if buttons else None
    if ctype in ("select_menu", "select"):
        placeholder = comp.get("placeholder", "Select an option")
        # In the claim panel (rendered with a viewer in scope), ANY dropdown you
        # place IS the "use an item" selector: the bot fills it with what the
        # viewer owns and wires it to the claim flow, so the owner doesn't have
        # to tick the "Ad inventory dropdown" box and no second dropdown is
        # auto-appended underneath. Outside the claim panel this branch is dead.
        if _ads_render_viewer:
            gid_, uid_ = _ads_render_viewer
            inv = _ads_inventory(gid_, uid_)
            opts = [{"label": f"{_ads_perk_label(k)} ({inv[k]})", "value": k} for k in ADS_PERK_KEYS if inv.get(k)]
            if not opts:
                opts = [{"label": "Nothing to claim", "value": "_none"}]
            global _ads_inventory_placed
            _ads_inventory_placed = True
            return {"type": 1, "components": [{"type": 3, "custom_id": "adsel_use",
                "placeholder": (placeholder or "Choose an item to use")[:150],
                "min_values": 1, "max_values": 1, "options": opts}]}
        options = []
        has_category = False
        for opt in comp.get("options", []):
            label = opt.get("label", "Option")
            category = opt.get("category", "")
            channel_id = opt.get("channel_id", "")
            url = opt.get("url", "")
            if "ticket" in opt:
                has_category = True
                value = f"ticket_msg:{_comp_key(opt)}"
            elif "form" in opt:
                has_category = True
                value = f"ticket_form:{_comp_key(opt)}"
            elif "ephemeral" in opt:
                has_category = True
                _ek = _comp_key(opt)
                eph_msgs[_ek] = opt.get("open_components") or []  # register on any surface
                _schedule_eph_save()
                value = f"eph:{_ek}"
            elif category:
                has_category = True
                value = category
            elif channel_id:
                value = f"ch:{channel_id}"
            elif url:
                value = f"url:{url}"[:100]
            else:
                value = label[:100]
            opt_label, opt_emoji = _extract_button_emoji(label)
            o = {"label": (opt_label or label)[:100], "value": value[:100]}
            if opt_emoji:
                o["emoji"] = opt_emoji
            if opt.get("description"):
                o["description"] = opt["description"][:100]
            options.append(o)
        if not options:
            return None
        custom_id = "ticket_select" if has_category else f"select_{placeholder[:20]}"
        return {"type": 1, "components": [{"type": 3, "custom_id": custom_id, "placeholder": placeholder[:150], "options": options}]}
    return None


async def send_v2_message(channel, components_v2, content=None, interaction=None, ephemeral=False, allowed_mentions=None, buttons=None, extra_rows=None):
    _guild = getattr(channel, "guild", None)

    built = [b for b in (_build_v2(c, _guild) for c in components_v2) if b]
    # A Components V2 message may NOT carry a top-level `content` field (Discord
    # rejects it with a 400). If a caller passes content (usually a ping), render
    # it as a leading text component instead so the message still posts and any
    # mention still fires via allowed_mentions.
    if content:
        built = [{"type": 10, "content": str(content)}] + built
        content = None
    if not built and not extra_rows:
        return False
    # These component types are all valid at the top level of a Components V2
    # message, so images (12), sections (9), action rows (1), separators (14),
    # etc. can live OUTSIDE a container. Only wrap if something invalid slips in.
    ALLOWED_TOP = {1, 9, 10, 12, 13, 14, 17}
    top_types = {c.get("type") for c in built}
    if built and not top_types.issubset(ALLOWED_TOP):
        built = [{"type": 17, "components": built}]
    # Attach buttons as an action row directly on this message (raw button dicts).
    if buttons:
        built.append({"type": 1, "components": list(buttons)})
    # Append fully-formed raw action rows (e.g. select menus + a button).
    for row in (extra_rows or []):
        built.append(row)
    flags = 1 << 15
    if ephemeral:
        flags |= 1 << 6
    payload = {"components": built, "flags": flags}
    if content:
        payload["content"] = content
    # Components V2 messages don't fire mention notifications unless the payload
    # explicitly allows them, so a <@&role> in a ticket message renders but never
    # pings without this.
    if allowed_mentions is not None:
        payload["allowed_mentions"] = allowed_mentions
    if interaction is not None:
        route = discord.http.Route("POST", "/webhooks/{application_id}/{interaction_token}", application_id=bot.application_id, interaction_token=interaction.token)
    else:
        route = discord.http.Route("POST", "/channels/{channel_id}/messages", channel_id=channel.id)
    try:
        resp = await bot.http.request(route, json=payload)
        # Return the new message id (truthy) so callers can track/replace it.
        if isinstance(resp, dict) and resp.get("id"):
            return str(resp["id"])
        return True
    except discord.HTTPException as e:
        body = getattr(e, "text", "") or ""
        _V2_LAST_ERROR["msg"] = f"HTTP {getattr(e, 'status', '?')}: {body[:400]}"
        print(f"[V2] send failed: HTTP {getattr(e, 'status', '?')} {body[:600]}")
        return False
    except Exception as e:
        _V2_LAST_ERROR["msg"] = str(e)[:400]
        print(f"[V2] send failed: {e}")
        return False


async def edit_v2_message(channel, message_id, components_v2, allowed_mentions=None):
    """Edit an existing Components V2 message in place (used to fill in a log's
    Reason after it was first posted as N/A)."""
    _guild = getattr(channel, "guild", None)
    built = [b for b in (_build_v2(c, _guild) for c in components_v2) if b]
    if not built:
        return False
    ALLOWED_TOP = {1, 9, 10, 12, 13, 14, 17}
    if not {c.get("type") for c in built}.issubset(ALLOWED_TOP):
        built = [{"type": 17, "components": built}]
    payload = {"components": built, "flags": 1 << 15}
    if allowed_mentions is not None:
        payload["allowed_mentions"] = allowed_mentions
    route = discord.http.Route("PATCH", "/channels/{channel_id}/messages/{message_id}",
                               channel_id=channel.id, message_id=int(message_id))
    try:
        await bot.http.request(route, json=payload)
        return True
    except Exception as e:
        print(f"[V2] edit failed: {e}")
        return False


def _v2_thread_name(components_v2, default="Portfolio"):
    """Derive a forum-post title from the first bit of text in the design."""
    def _first_text(items):
        for c in items or []:
            if not isinstance(c, dict):
                continue
            if c.get("type") in ("text", "text_display"):
                t = (c.get("title") or c.get("text") or c.get("content") or "").strip()
                if t:
                    return t
            for kids in (c.get("children"), c.get("components")):
                if isinstance(kids, list):
                    t = _first_text(kids)
                    if t:
                        return t
        return ""
    raw = _first_text(components_v2) or default
    # First line only, strip markdown emphasis, cap at Discord's 100-char limit.
    line = raw.splitlines()[0].replace("*", "").replace("_", "").replace("#", "").strip()
    return (line or default)[:100]


async def send_v2_forum_post(forum, components_v2, name=None):
    """Create a forum post (thread) carrying a Components-V2 message. Forum
    channels can't take a plain message — a thread with a starter message is
    the only way to post into them. Returns the thread id (truthy) or False."""
    _guild = getattr(forum, "guild", None)
    built = [b for b in (_build_v2(c, _guild) for c in components_v2) if b]
    if not built:
        return False
    ALLOWED_TOP = {1, 9, 10, 12, 13, 14, 17}
    if not {c.get("type") for c in built}.issubset(ALLOWED_TOP):
        built = [{"type": 17, "components": built}]
    payload = {
        "name": name or _v2_thread_name(components_v2),
        "message": {"components": built, "flags": 1 << 15},
    }
    route = discord.http.Route("POST", "/channels/{channel_id}/threads", channel_id=forum.id)
    try:
        resp = await bot.http.request(route, json=payload)
        if isinstance(resp, dict) and resp.get("id"):
            return str(resp["id"])
        return True
    except discord.HTTPException as e:
        body = getattr(e, "text", "") or ""
        _V2_LAST_ERROR["msg"] = f"HTTP {getattr(e, 'status', '?')}: {body[:400]}"
        print(f"[V2] forum post failed: HTTP {getattr(e, 'status', '?')} {body[:600]}")
        return False
    except Exception as e:
        _V2_LAST_ERROR["msg"] = str(e)[:400]
        print(f"[V2] forum post failed: {e}")
        return False


_BUTTON_EMOJI_RE = re.compile(r"<(a?):([a-zA-Z0-9_]+):(\d+)>")


def _extract_button_emoji(label):
    match = _BUTTON_EMOJI_RE.search(label)
    if not match:
        return label, None
    emoji = {"id": match.group(3), "name": match.group(2), "animated": bool(match.group(1))}
    clean = (label[: match.start()] + label[match.end():]).strip()
    return clean, emoji


def build_button(btn, guild):
    label = btn.get("label", "Button")
    category = btn.get("category", "")
    channel_id = btn.get("channel_id", "")
    url = btn.get("url", "")
    style_name = str(btn.get("style", "primary")).lower()
    # Resolve :emoji: shortcodes and {count}-style variables in the label so a
    # button labeled ":w_love: {count}" shows the emoji + live count.
    label = _render_guild_text(label, guild)
    label, emoji = _extract_button_emoji(label)

    def _btn(data):
        if emoji:
            data["emoji"] = emoji
        if label:
            data["label"] = label[:80]
        elif "label" in data:
            del data["label"]
        return data

    if btn.get("counter"):
        # Giveaway "Counter" (enter) button. The real custom_id (gw:<gid>) is
        # patched in per-giveaway by _giveaway_render_design.
        return _btn({"type": 2, "label": (label[:80] or "Enter"), "style": BUTTON_STYLE_MAP.get(style_name, 1), "custom_id": "gw:__COUNTER__"})
    if "notify_roles" in btn:
        # Notification button — clicking toggles the selected role(s) on the
        # member. Role ids are baked into the custom_id so it survives restarts.
        role_objs = _resolve_role_names(guild, btn.get("notify_roles"))
        ids = ",".join(str(r.id) for r in role_objs)
        cid = f"notifyrole:{ids}"[:100]
        return _btn({"type": 2, "label": (label[:80] or "Notify me"), "style": BUTTON_STYLE_MAP.get(style_name, 2), "custom_id": cid})
    if btn.get("orderstatus"):
        # Order Status button — shows a live per-service open/limited/closed embed.
        return _btn({"type": 2, "label": (label[:80] or "Order Status"), "style": BUTTON_STYLE_MAP.get(style_name, 2), "custom_id": "orderstatus"})
    if btn.get("adclaim"):
        # Advertising "Post an Ad" button — opens the buyer's ad inventory + post flow.
        return _btn({"type": 2, "label": (label[:80] or ads_config.get("claim_button_label") or "Post an Ad"), "style": BUTTON_STYLE_MAP.get(style_name, 1), "custom_id": "ad_claim"})
    if btn.get("adqueue"):
        # "View Queue" button — opens the paginated Live Advertisement Queue.
        return _btn({"type": 2, "label": (label[:80] or "View Queue"), "style": BUTTON_STYLE_MAP.get(style_name, 2), "custom_id": "ad_queue"})
    if btn.get("__verify"):
        return _btn({"type": 2, "label": (label[:80] or "Verify"), "style": BUTTON_STYLE_MAP.get(style_name, 1), "custom_id": "roblox_verify"})
    if btn.get("__ticket_open"):
        cat = str(btn.get("category") or "support")[:80]
        return _btn({"type": 2, "label": (label[:80] or "Open Ticket"), "style": BUTTON_STYLE_MAP.get(style_name, 1), "custom_id": f"ticket_cat:{cat}"})
    if btn.get("__ticket_claim"):
        return _btn({"type": 2, "label": (label[:80] or "Claim"), "style": 3, "custom_id": "ticket_claim"})
    if btn.get("__ticket_unclaim"):
        return _btn({"type": 2, "label": (label[:80] or "Unclaim"), "style": 2, "custom_id": "ticket_unclaim"})
    if btn.get("__ticket_close"):
        return _btn({"type": 2, "label": (label[:80] or "Close"), "style": BUTTON_STYLE_MAP.get(style_name, 4), "custom_id": "ticket_close"})
    if btn.get("disabled"):
        cid = f"display_{btn.get('id') or label[:20] or 'x'}"
        return _btn({"type": 2, "label": label[:80], "style": BUTTON_STYLE_MAP.get(style_name, 2), "custom_id": cid[:100], "disabled": True})
    if "ticket" in btn:
        key = _comp_key(btn)
        return _btn({"type": 2, "label": label[:80], "style": BUTTON_STYLE_MAP.get(style_name, 1), "custom_id": f"ticket_msg:{key}"})
    if "form" in btn:
        key = _comp_key(btn)
        return _btn({"type": 2, "label": label[:80], "style": BUTTON_STYLE_MAP.get(style_name, 1), "custom_id": f"ticket_form:{key}"})
    if "ephemeral" in btn:
        key = _comp_key(btn)
        eph_msgs[key] = btn.get("open_components") or []  # works on ANY surface, not just ticket panels
        _schedule_eph_save()
        return _btn({"type": 2, "label": label[:80], "style": BUTTON_STYLE_MAP.get(style_name, 1), "custom_id": f"eph:{key}"})
    if category:
        return _btn({"type": 2, "label": label[:80], "style": BUTTON_STYLE_MAP.get(style_name, 1), "custom_id": f"ticket_cat:{category[:80]}"})
    if channel_id:
        gid = getattr(guild, "id", 0)
        return _btn({"type": 2, "label": label[:80], "style": 5, "url": f"https://discord.com/channels/{gid}/{channel_id}"})
    if url:
        return _btn({"type": 2, "label": label[:80], "style": 5, "url": url})
    return _btn({"type": 2, "label": label[:80], "style": BUTTON_STYLE_MAP.get(style_name, 1), "custom_id": f"btn_{label[:20] or 'x'}"})


def build_embed(data, guild=None):
    def _r(v):
        return _render_guild_text(v, guild) if isinstance(v, str) else v
    try:
        color = int(data.get("color")) if data.get("color") is not None else ACCENT
    except Exception:
        color = ACCENT
    embed = discord.Embed(color=color)
    if data.get("title"):
        embed.title = _r(data["title"])
    if data.get("title_url"):
        embed.url = data["title_url"]
    if data.get("description"):
        embed.description = _r(data["description"])
    author = data.get("author")
    if isinstance(author, dict) and author.get("name"):
        embed.set_author(name=_r(author["name"]), icon_url=author.get("icon_url") or None)
    footer = data.get("footer")
    if isinstance(footer, dict) and footer.get("text"):
        embed.set_footer(text=_r(footer["text"]), icon_url=footer.get("icon_url") or None)
    for f in data.get("fields", []) or []:
        if f.get("name") and f.get("value"):
            embed.add_field(name=_r(f["name"]), value=_r(f["value"]), inline=bool(f.get("inline")))
    if data.get("thumbnail_url"):
        embed.set_thumbnail(url=data["thumbnail_url"])
    if data.get("image_url"):
        embed.set_image(url=data["image_url"])
    if data.get("timestamp"):
        embed.timestamp = discord.utils.utcnow()
    return embed


async def handle_post(channel, payload):
    components_v2 = payload.get("components_v2")
    # Files the poster wants dropped into a THREAD off the message (e.g. a
    # custom-feature "Example" upload), rather than inlined on the message
    # itself. Each entry: {url, filename, label}.
    thread_files = [f for f in (payload.get("thread_files") or []) if isinstance(f, dict) and f.get("url")]
    if components_v2:
        mid = await send_v2_message(channel, components_v2, payload.get("content") or None)
        if thread_files and mid:
            tname = _clean_label(thread_files[0].get("label") or "Example") or "Example"
            await _post_form_files_thread(
                channel, mid if isinstance(mid, str) else None, thread_files, tname,
            )
        return
    embeds_data = payload.get("embeds") or []
    _guild = getattr(channel, "guild", None)
    content = _render_guild_text(payload.get("content") or "", _guild) or None
    embeds = [build_embed(e, _guild) for e in embeds_data if isinstance(e, dict)]
    extra_images = payload.get("images") or []
    for url in extra_images[1:10]:
        eb = discord.Embed(color=ACCENT)
        eb.set_image(url=url)
        embeds.append(eb)
    sent = None
    if embeds:
        sent = await channel.send(content=content, embeds=embeds[:10])
    elif content:
        sent = await channel.send(content=content)
    if thread_files and sent is not None:
        tname = _clean_label(thread_files[0].get("label") or "Example") or "Example"
        await _post_form_files_thread(channel, str(sent.id), thread_files, tname)
    for extra in payload.get("trailing_messages", []) or []:
        if extra:
            await channel.send(extra)


async def _resync_ticket_support_perms(config):
    """Grant the configured 'global support roles' view access on EVERY existing
    open ticket channel. Discord applies permission overwrites only at channel
    creation, so a support role added after some tickets already exist wouldn't
    be able to see those older tickets without this. Only touches a channel when
    the overwrite is actually missing, so it's cheap once everything is in sync."""
    role_ids = [str(x) for x in (config.get("support_role_ids") or []) if x]
    if not role_ids:
        return
    updated, tickets = 0, 0
    for guild in list(bot.guilds):
        roles = [r for r in (guild.get_role(int(rid)) for rid in role_ids) if r]
        if not roles:
            continue
        for ch in list(getattr(guild, "text_channels", [])):
            topic = getattr(ch, "topic", "") or ""
            if not topic.startswith("ticket|"):
                continue
            tickets += 1
            for role in roles:
                try:
                    ow = ch.overwrites_for(role)
                    if ow.view_channel is not True:
                        await ch.set_permissions(role, view_channel=True, send_messages=True,
                                                 reason="Sync global support role access")
                        updated += 1
                except Exception as e:
                    print(f"[Tickets] perm sync failed on #{getattr(ch, 'name', ch.id)}: {e}")
    print(f"[Tickets] support-role sync: checked {tickets} open ticket(s), "
          f"granted access on {updated} ticket/role pair(s)")


async def resolve_channel(channel_id):
    if not channel_id:
        return None
    try:
        cid = int(channel_id)
    except (TypeError, ValueError):
        # A non-numeric id (e.g. a stray "design" from a misconfigured field)
        # shouldn't crash the caller — just resolve to nothing.
        return None
    channel = bot.get_channel(cid)
    if channel is None:
        try:
            channel = await bot.fetch_channel(cid)
        except Exception:
            channel = None
    return channel


async def apply_config(feature, cfg, post_panel=False):
    if not isinstance(cfg, dict):
        return
    if feature in ("welcome", "join-logs", "welcome-logs"):
        if "enabled" in cfg:
            welcome_config["enabled"] = bool(cfg["enabled"])
        if cfg.get("channel_id"):
            welcome_config["channel_id"] = str(cfg["channel_id"])
        if cfg.get("message") is not None:
            welcome_config["message"] = cfg.get("message") or ""
        print(f"[Config] welcome — channel {welcome_config['channel_id']} enabled {welcome_config['enabled']}")
    elif feature in ("tickets", "ticket-panels"):
        if cfg.get("category_id"):
            ticket_config["category_id"] = str(cfg["category_id"])
        if cfg.get("support_role_ids") is not None:
            ticket_config["support_role_ids"] = [str(x) for x in cfg["support_role_ids"] if x]
        if cfg.get("log_channel_id"):
            ticket_config["log_channel_id"] = str(cfg["log_channel_id"])
        if cfg.get("open_message") is not None:
            ticket_config["open_message"] = cfg.get("open_message") or ""
        if "ping_support" in cfg:
            ticket_config["ping_support"] = bool(cfg["ping_support"])
        if "one_per_user" in cfg:
            ticket_config["one_per_user"] = bool(cfg["one_per_user"])
        # Multi-panel: cfg.panels = [{channel_id, components}, ...]. Falls back to
        # the single panel_channel_id + panel_components for older configs. ALL
        # panels are registered so every posted panel keeps working.
        # The dashboard sends panel_channel_id = the panel currently being edited.
        # We register ALL panels (so every panel's buttons keep working) but only
        # (re)post that one on save.
        panels = _parse_ticket_panels(cfg)
        edited_ch = str(cfg.get("panel_channel_id") or (panels[0]["channel_id"] if panels else ""))
        edited_panel = next((p for p in panels if p["channel_id"] == edited_ch), (panels[0] if panels else {"components": []}))
        ticket_config["panel_channel_id"] = edited_ch
        ticket_config["panel_components"] = edited_panel.get("components", [])
        _ticket_sources["tickets"] = {"panels": panels, "types": _parse_ticket_types(cfg)}
        _rebuild_ticket_registry()
        print(f"[Config] tickets — category {ticket_config['category_id']} roles {ticket_config['support_role_ids']} panel_ch {ticket_config['panel_channel_id']} panel {len(ticket_config['panel_components'])} types {len(ticket_config['types'])}")
        # Post/refresh ONLY the panel being edited on a save (not on boot, and
        # not the other panels — those stay put). On a save we also re-apply the
        # support roles to every open ticket (boot does this separately, in
        # on_ready, so it always runs on a redeploy).
        if post_panel:
            asyncio.create_task(_resync_ticket_support_perms(ticket_config))
            await post_ticket_panel(only_channel_id=edited_ch or None)
    elif feature in ("marketplace", "customs-marketplace"):
        # An independent second ticket system with its own category/roles/log.
        if cfg.get("category_id"):
            marketplace_config["category_id"] = str(cfg["category_id"])
        if cfg.get("support_role_ids") is not None:
            marketplace_config["support_role_ids"] = [str(x) for x in cfg["support_role_ids"] if x]
        if cfg.get("log_channel_id"):
            marketplace_config["log_channel_id"] = str(cfg["log_channel_id"])
        if cfg.get("open_message") is not None:
            marketplace_config["open_message"] = cfg.get("open_message") or ""
        if "ping_support" in cfg:
            marketplace_config["ping_support"] = bool(cfg["ping_support"])
        if "one_per_user" in cfg:
            marketplace_config["one_per_user"] = bool(cfg["one_per_user"])
        panels = _parse_ticket_panels(cfg)
        edited_ch = str(cfg.get("panel_channel_id") or (panels[0]["channel_id"] if panels else ""))
        edited_panel = next((p for p in panels if p["channel_id"] == edited_ch), (panels[0] if panels else {"components": []}))
        marketplace_config["panel_channel_id"] = edited_ch
        marketplace_config["panel_components"] = edited_panel.get("components", [])
        # The ad post channel + interval are configured here (in Marketplace).
        if "ad_post_channel_id" in cfg:
            ads_config["post_channel_id"] = str(cfg.get("ad_post_channel_id") or "")
        if cfg.get("ad_interval_minutes"):
            try:
                ads_config["interval_minutes"] = max(1, int(cfg["ad_interval_minutes"]))
            except Exception:
                pass
        _ticket_sources["marketplace"] = {"panels": panels, "types": _parse_ticket_types(cfg)}
        _rebuild_ticket_registry()
        print(f"[Config] marketplace — category {marketplace_config['category_id']} roles {marketplace_config['support_role_ids']} panel_ch {edited_ch} panels {len(panels)} types {len(_ticket_sources['marketplace']['types'])}")
        if post_panel:
            await post_ticket_panel(only_channel_id=edited_ch or None)
    elif feature in ("giveaway", "customs-giveaway"):
        if cfg.get("title") is not None:
            giveaway_config["title"] = str(cfg.get("title") or "🎉 GIVEAWAY 🎉")
        if cfg.get("color") is not None:
            try:
                giveaway_config["color"] = int(cfg["color"])
            except Exception:
                pass
        if cfg.get("button_label") is not None:
            giveaway_config["button_label"] = str(cfg.get("button_label") or "🎉 Enter")
        if cfg.get("host_line") is not None:
            giveaway_config["host_line"] = str(cfg.get("host_line") or "")
        if cfg.get("ping") is not None:
            giveaway_config["ping"] = str(cfg.get("ping") or "")
        if cfg.get("default_winners") is not None:
            try:
                giveaway_config["default_winners"] = max(1, int(cfg["default_winners"]))
            except Exception:
                pass
        if cfg.get("default_duration"):
            giveaway_config["default_duration"] = str(cfg["default_duration"])
        if cfg.get("manager_role_ids") is not None:
            giveaway_config["manager_role_ids"] = [str(x) for x in cfg["manager_role_ids"] if x]
        comps = cfg.get("components")
        giveaway_config["components"] = comps if isinstance(comps, list) else []
        ended = cfg.get("ended_components")
        giveaway_config["ended_components"] = ended if isinstance(ended, list) else []
        print(f"[Config] giveaway — managers {giveaway_config['manager_role_ids']} design {len(giveaway_config['components'])} ended {len(giveaway_config['ended_components'])}")
    elif feature in ("customs-messages", "messages"):
        raw = cfg.get("messages")
        msgs = []
        if isinstance(raw, list):
            for m in raw:
                if isinstance(m, dict) and m.get("channel_id"):
                    msgs.append({"channel_id": str(m["channel_id"]),
                                 "components": m.get("components") if isinstance(m.get("components"), list) else []})
        saved_messages_config["messages"] = msgs
        for _m in msgs:
            _register_eph_from_tree(_m.get("components") or [])
        edited = str(cfg.get("edited_channel_id") or "")
        print(f"[Config] customs-messages — {len(msgs)} saved message(s), edited {edited or '(none)'}")
        # Only (re)post on a deliberate save, never on boot — like ticket panels.
        if post_panel:
            await post_saved_messages(only_channel_id=edited or None)
    elif feature in ("customs-suggestions", "customs-feedback", "customs-reportbug"):
        # A prompt form: the designer saves {messages:[{channel_id, components}]}.
        # First saved message holds both the output design (with {question:} etc.
        # tokens) and the destination channel the admin picked.
        design, channel_id = [], ""
        raw = cfg.get("messages")
        if isinstance(raw, list) and raw:
            m0 = raw[0] or {}
            design = m0.get("components") if isinstance(m0.get("components"), list) else []
            channel_id = str(m0.get("channel_id") or "")
        # Allow flat overrides too.
        channel_id = str(cfg.get("channel_id") or channel_id)
        prompt_forms_config[feature] = {
            "design": design,
            "channel_id": channel_id,
            "title": str(cfg.get("title") or _PF_TITLES.get(feature) or ""),
        }
        _register_eph_from_tree(design)
        print(f"[Config] {feature} — channel {channel_id or '(none)'}, "
              f"{len(_pf_inputs(design))} form field(s)")
    elif feature == "roleplay-sessions":
        session_config["manager_role_ids"] = [str(x) for x in (cfg.get("manager_role_ids") or []) if x]
        session_config["channel_id"] = str(cfg.get("channel_id") or "")
        session_config["ping_role_id"] = str(cfg.get("ping_role_id") or "")
        try:
            session_config["vote_needed"] = max(1, int(cfg.get("vote_needed") or 5))
        except (TypeError, ValueError):
            session_config["vote_needed"] = 5
        for k in SESSION_DEFAULTS:
            v = cfg.get(f"{k}_components")
            session_config["designs"][k] = v if isinstance(v, list) else []
            _register_eph_from_tree(session_config["designs"][k])
        print(f"[Config] roleplay-sessions, channel {session_config['channel_id'] or '(none)'} "
              f"ping {session_config['ping_role_id'] or '(none)'} votes {session_config['vote_needed']} "
              f"designs {[k for k, v in session_config['designs'].items() if v]}")
    elif feature == "roleplay-shifts":
        shift_config["staff_role_ids"] = [str(x) for x in (cfg.get("staff_role_ids") or []) if x]
        shift_config["onshift_role_ids"] = [str(x) for x in (cfg.get("onshift_role_ids") or []) if x]
        try:
            shift_config["quota_hours"] = max(0.0, float(cfg.get("quota_hours") or 0))
        except (TypeError, ValueError):
            shift_config["quota_hours"] = 0.0
        shift_config["log_channel_id"] = str(cfg.get("log_channel_id") or "")
        for k, default in (("manage_message", SHIFT_DEFAULT_MANAGE), ("leaderboard_message", SHIFT_DEFAULT_LEADERBOARD),
                           ("online_message", SHIFT_DEFAULT_ONLINE)):
            v = cfg.get(k)
            shift_config[k] = str(v) if isinstance(v, str) and v.strip() else default
        print(f"[Config] roleplay-shifts, staff roles {len(shift_config['staff_role_ids'])} "
              f"on-shift roles {len(shift_config['onshift_role_ids'])} quota {shift_config['quota_hours']}h "
              f"log {shift_config['log_channel_id'] or '(none)'}")
    elif feature in ("customs-blacklist",):
        raw = cfg.get("messages")
        design, channel_id = [], ""
        if isinstance(raw, list) and raw:
            m0 = raw[0] or {}
            design = m0.get("components") if isinstance(m0.get("components"), list) else []
            channel_id = str(m0.get("channel_id") or "")
        blacklist_config["design"] = design if isinstance(design, list) else []
        blacklist_config["channel_id"] = str(cfg.get("channel_id") or channel_id)
        # Optional enforcement: on /blacklist, strip the member's roles and give
        # them a chosen "blacklisted" role (so staff don't have to ban/lose them).
        blacklist_config["apply_role"] = bool(cfg.get("apply_role"))
        blacklist_config["role_id"] = str(cfg.get("role_id") or cfg.get("blacklist_role_id") or "")
        blacklist_config["strip_roles"] = bool(cfg.get("strip_roles", True))
        _register_eph_from_tree(blacklist_config["design"])
        print(f"[Config] customs-blacklist — channel {blacklist_config['channel_id'] or '(none)'} "
              f"| role {'on '+blacklist_config['role_id'] if blacklist_config['apply_role'] else 'off'}")
    elif feature in ("customs-smallui",):
        uis = cfg.get("uis")
        if isinstance(uis, dict):
            for k, v in uis.items():
                small_ui_config[k] = v if isinstance(v, list) else []
                _register_eph_from_tree(small_ui_config[k])
        ac = cfg.get("ticket_autoclose") or {}
        if isinstance(ac, dict):
            if "enabled" in ac:
                ticket_autoclose_config["enabled"] = bool(ac["enabled"])
            if ac.get("warn_hours"):
                ticket_autoclose_config["warn_hours"] = int(ac["warn_hours"])
            if ac.get("close_hours"):
                ticket_autoclose_config["close_hours"] = int(ac["close_hours"])
        print(f"[Config] customs-smallui — {len(small_ui_config)} UI(s), "
              f"autoclose {'on' if ticket_autoclose_config['enabled'] else 'off'}")
    elif feature in ("invite-tracker",):
        invite_tracker_config["enabled"] = bool(cfg.get("enabled", True))
        comps = cfg.get("board_components")
        invite_tracker_config["board_components"] = comps if isinstance(comps, list) else []
        _register_eph_from_tree(invite_tracker_config["board_components"])
        print(f"[Config] invite-tracker — enabled {invite_tracker_config['enabled']} "
              f"design {len(invite_tracker_config['board_components'])}")
    elif feature in ("ads", "advertisements"):
        ads_config["enabled"] = bool(cfg.get("enabled", True))
        ads_config["approval_channel_id"] = str(cfg.get("approval_channel_id") or "")
        ads_config["staff_role_ids"] = [str(x) for x in (cfg.get("staff_role_ids") or []) if x]
        # NOTE: post channel + interval are set in the Marketplace block.
        perks = cfg.get("perks")
        if isinstance(perks, dict):
            for k in ADS_PERK_KEYS:
                if perks.get(k):
                    ads_config["perks"][k] = str(perks[k])
        rd = cfg.get("regular_design")
        ads_config["regular_design"] = rd if isinstance(rd, list) else []
        gd = cfg.get("giveaway_design")
        ads_config["giveaway_design"] = gd if isinstance(gd, list) else []
        cd = cfg.get("claim_design")
        ads_config["claim_design"] = cd if isinstance(cd, list) else []
        ed = cfg.get("empty_design")
        ads_config["empty_design"] = ed if isinstance(ed, list) else []
        npd = cfg.get("noposts_design")
        ads_config["noposts_design"] = npd if isinstance(npd, list) else []
        _register_eph_from_tree(ads_config["regular_design"])
        _register_eph_from_tree(ads_config["giveaway_design"])
        if cfg.get("claim_button_label"):
            ads_config["claim_button_label"] = str(cfg["claim_button_label"])
        # Customizable wording of the claim panel — only overwrite when provided.
        for _k in ("claim_title", "claim_note", "ping_placeholder", "type_placeholder",
                   "addon_placeholder", "continue_label", "regular_label", "giveaway_label"):
            if cfg.get(_k) is not None:
                ads_config[_k] = str(cfg.get(_k) or "") or ads_config[_k]
        # claim_note is allowed to be empty (no note).
        if "claim_note" in cfg:
            ads_config["claim_note"] = str(cfg.get("claim_note") or "")
        print(f"[Config] ads — enabled {ads_config['enabled']} "
              f"approval {ads_config['approval_channel_id'] or '(none)'}")
    elif feature in ("customs-gambling", "gambling", "economy"):
        gambling_config["enabled"] = bool(cfg.get("enabled", True))
        pfx = str(cfg.get("prefix") or "!").strip()
        gambling_config["prefix"] = (pfx or "!")[:5]
        gambling_config["currency_symbol"] = str(cfg.get("currency_symbol") or "🪙").strip() or "🪙"
        gambling_config["currency_name"] = str(cfg.get("currency_name") or "coins").strip() or "coins"
        try:
            gambling_config["start_balance"] = max(0, int(cfg.get("start_balance") or 0))
        except Exception:
            gambling_config["start_balance"] = 0
        try:
            gambling_config["max_balance"] = max(0, int(cfg.get("max_balance") or 0))
        except Exception:
            gambling_config["max_balance"] = 0
        gambling_config["audit_log_channel_id"] = str(cfg.get("audit_log_channel_id") or "")
        gambling_config["allowed_channel_ids"] = [str(x) for x in (cfg.get("allowed_channel_ids") or []) if x]
        gambling_config["admin_role_ids"] = [str(x) for x in (cfg.get("admin_role_ids") or []) if x]
        for k in ("work", "slut", "crime", "rob"):
            try:
                gambling_config["cooldowns"][k] = max(0, int(cfg.get(f"cooldown_{k}") or gambling_config["cooldowns"][k]))
            except Exception:
                pass
        for k in ("work", "slut", "crime"):
            lo, hi = cfg.get(f"payout_{k}_min"), cfg.get(f"payout_{k}_max")
            if lo is not None or hi is not None:
                gambling_config["payouts"][k] = [int(lo or 0), int(hi or lo or 0)]
        for k in ("slut", "crime"):
            lo, hi = cfg.get(f"fine_{k}_min"), cfg.get(f"fine_{k}_max")
            if lo is not None or hi is not None:
                gambling_config["fines"][k] = [int(lo or 0), int(hi or lo or 0)]
            fr = cfg.get(f"failrate_{k}")
            if fr is not None:
                try:
                    gambling_config["fail_rate"][k] = max(0.0, min(1.0, float(fr) / 100.0))
                except Exception:
                    pass
        if cfg.get("fine_type"):
            gambling_config["fine_type"] = "fixed" if str(cfg.get("fine_type")) == "fixed" else "percent"
        if isinstance(cfg.get("role_income"), list):
            gambling_config["role_income"] = cfg.get("role_income")
        if isinstance(cfg.get("properties"), list):
            gambling_config["properties"] = cfg.get("properties")
        print(f"[Config] gambling — enabled {gambling_config['enabled']} prefix '{gambling_config['prefix']}' currency {gambling_config['currency_symbol']} {gambling_config['currency_name']}")
    elif feature in ("customs-tts", "tts"):
        eng = str(cfg.get("engine") or "gtts").lower()
        tts_config["engine"] = eng if eng in ("gtts", "eleven") else "gtts"
        tts_config["accent"] = str(cfg.get("accent") or TTS_TLD).strip() or TTS_TLD
        try:
            tts_config["speed"] = max(0.5, min(2.0, float(cfg.get("speed") or TTS_PLAYBACK_SPEED)))
        except Exception:
            tts_config["speed"] = TTS_PLAYBACK_SPEED
        if str(cfg.get("voice_id") or "").strip():
            tts_config["voice_id"] = str(cfg.get("voice_id")).strip()
        tts_config["join_message"] = str(cfg.get("join_message") or "")
        tts_config["leave_message"] = str(cfg.get("leave_message") or "")
        print(f"[Config] tts — engine {tts_config['engine']} accent {tts_config['accent']} speed {tts_config['speed']}")
    elif feature in ("music-addon", "customs-music-addon"):
        music_config["enabled"] = True
        music_config["dj_role_ids"] = [str(x) for x in (cfg.get("dj_role_ids") or []) if x]
        music_config["everyone_can_queue"] = bool(cfg.get("everyone_can_queue", True))
        try:
            music_config["max_queue_length"] = max(1, int(cfg.get("max_queue_length") or 100))
        except Exception:
            music_config["max_queue_length"] = 100
        try:
            music_config["default_volume"] = max(1, min(100, int(cfg.get("default_volume") or 50)))
        except Exception:
            music_config["default_volume"] = 50
        music_config["auto_leave"] = bool(cfg.get("auto_leave", True))
        music_config["now_playing_v2"] = bool(cfg.get("now_playing_v2", False))
        print(f"[Config] music — dj_roles {music_config['dj_role_ids']} everyone_queue {music_config['everyone_can_queue']} maxq {music_config['max_queue_length']} vol {music_config['default_volume']} node {'set' if LAVALINK_URI else 'UNSET'}")
    elif feature in ("auto-radio", "customs-auto-radio"):
        music_config["enabled"] = True  # radio implies the music engine is on
        music_config["radio_channel_id"] = str(cfg.get("voice_channel_id") or "")
        music_config["radio_genre"] = str(cfg.get("genre") or "pop")
        print(f"[Config] auto-radio — channel {music_config['radio_channel_id']} genre {music_config['radio_genre']}")
    elif feature in FORM_LOG_DEFS:
        # Form logs (/orderlog, /infraction, /promote): pop a form from the
        # {Question:} tokens in the design, then post the completed message to the
        # configured channel (answers filled in). Not a ticket.
        key = FORM_LOG_DEFS[feature]["key"]
        comps = cfg.get("components")
        if comps is None:
            comps = cfg.get("panel_components")
            if not comps and isinstance(cfg.get("panels"), list) and cfg["panels"]:
                comps = (cfg["panels"][0] or {}).get("components")
        fc = form_log_configs[key]
        fc["components"] = comps if isinstance(comps, list) else []
        fc["channel_id"] = str(cfg.get("channel_id") or "")
        fc["allowed_role_ids"] = [str(x) for x in (cfg.get("allowed_role_ids") or []) if x]
        fc["run_role_ids"] = [str(x) for x in (cfg.get("run_role_ids") or []) if x]
        # Watched role SETS: each set auto-triggers a log when at least `min` of its
        # roles are added (promotion) / removed (infraction) from a member.
        fc["groups"] = _parse_role_groups(cfg)
        # Fallback so dashboard-picked roles work even if the separate "Set" fields
        # aren't in play: with no explicit sets, watch the config's roles directly —
        # removing (infraction) / adding (promotion) ANY one of them fires a log.
        if not fc["groups"] and key in ("infraction", "promotion") and fc["allowed_role_ids"]:
            fc["groups"] = [{"roles": set(fc["allowed_role_ids"]), "min": 1}]
        _ticket_sources.pop(feature, None)  # not a panel source
        print(f"[Config] {key}(form) — design {len(fc['components'])} channel {fc['channel_id']} "
              f"allowed {fc['allowed_role_ids']} groups {[(len(g['roles']), g['min']) for g in fc['groups']]}")
    elif feature in ("logging", "customs-logging"):
        logging_config["purchase_log_channel_id"] = str(cfg.get("purchase_log_channel_id") or "")
        comps = cfg.get("purchase_components")
        logging_config["purchase_components"] = comps if isinstance(comps, list) else []
        print(f"[Config] logging — purchase_log {logging_config['purchase_log_channel_id']} design {len(logging_config['purchase_components'])}")
    elif feature == "invite":
        if cfg.get("channel_id"):
            invite_config["channel_id"] = str(cfg["channel_id"])
        comps = cfg.get("components")
        invite_config["components"] = comps if isinstance(comps, list) else []
        embeds = cfg.get("embeds")
        invite_config["embeds"] = embeds if isinstance(embeds, list) else []
        msgs = cfg.get("messages")
        invite_config["messages"] = msgs if isinstance(msgs, list) else []
        print(f"[Config] invite — channel {invite_config['channel_id']} components {len(invite_config['components'])} embeds {len(invite_config['embeds'])}")
    elif feature in ("roblox-verify", "verification"):
        roblox_config["channel_id"] = str(cfg.get("channel_id") or "")
        # Roles to add — new multi shape, with legacy single verified_role_id fallback.
        add_ids = cfg.get("verified_role_ids")
        if not isinstance(add_ids, list):
            add_ids = [cfg.get("verified_role_id")] if cfg.get("verified_role_id") else []
        roblox_config["verified_role_ids"] = [str(r) for r in add_ids if r]
        rem_ids = cfg.get("remove_role_ids")
        roblox_config["remove_role_ids"] = [str(r) for r in rem_ids if r] if isinstance(rem_ids, list) else []
        roblox_config["set_nickname"] = bool(cfg.get("set_nickname", True))
        roblox_config["log_channel_id"] = str(cfg.get("log_channel_id") or "")
        roblox_config["client_id"] = str(cfg.get("roblox_client_id") or "")
        roblox_config["client_secret"] = str(cfg.get("roblox_client_secret") or "")
        comps = cfg.get("components")
        roblox_config["components"] = comps if isinstance(comps, list) else []
        roblox_config["button_label"] = str(cfg.get("verify_button_label") or "Verify")
        roblox_config["button_style"] = str(cfg.get("verify_button_style") or "primary")
        print(f"[Config] roblox-verify — channel {roblox_config['channel_id']} add_roles {roblox_config['verified_role_ids']} remove_roles {roblox_config['remove_role_ids']} nick {roblox_config['set_nickname']} components {len(roblox_config['components'])}")
        # Post the panel when this came from a save/apply (deliberate action),
        # but NOT on boot — that avoids the surprise repost on every restart.
        # _replace_panel dedupes so a re-post replaces the old panel.
        if post_panel:
            await post_verify_panel()

    elif feature in ("roblox-group-sync", "customs-roblox-group-sync"):
        group_sync_config["group_id"] = str(cfg.get("group_id") or "").strip()
        if not group_sync_config["group_id"]:
            # Nothing typed in the block — use the Roblox group ID saved under
            # API keys & credentials, so the group only has to be entered once.
            group_sync_config["group_id"] = await _bot_secret("ROBLOX_GROUP_ID")
        group_sync_config["tiers"] = _parse_group_sync_tiers(cfg)
        try:
            dr = cfg.get("demote_rank")
            group_sync_config["demote_rank"] = int(dr) if dr not in (None, "") else None
        except (TypeError, ValueError):
            group_sync_config["demote_rank"] = None
        group_sync_config["enabled"] = bool(cfg.get("enabled", True)) and bool(
            group_sync_config["group_id"]) and bool(group_sync_config["tiers"])
        print(f"[Config] roblox-group-sync — group {group_sync_config['group_id']} "
              f"tiers {[(t['rank'], sorted(t['role_ids'])) for t in group_sync_config['tiers']]} "
              f"demote {group_sync_config['demote_rank']} enabled {group_sync_config['enabled']}")


def _is_tracked_giveaway_message(mid):
    """True if a message id belongs to a giveaway this process is tracking, so no
    panel-replacement logic can ever delete a giveaway message by mistake."""
    try:
        mid = str(mid)
        return any(str(g.get("message_id")) == mid for g in active_giveaways.values())
    except Exception:
        return False


async def _replace_panel(new_channel_id, new_message_id):
    """Record the freshly-posted panel and delete the previous one, so posting
    again REPLACES the old panel instead of stacking duplicates."""
    old = roblox_config.get("panel_ref")
    roblox_config["panel_ref"] = (
        {"channel_id": str(new_channel_id), "message_id": str(new_message_id)}
        if new_message_id and new_message_id is not True
        else None
    )
    if old and old.get("message_id") and not _is_tracked_giveaway_message(old["message_id"]):
        try:
            ch = await resolve_channel(old.get("channel_id"))
            if ch:
                msg = await ch.fetch_message(int(old["message_id"]))
                await msg.delete()
        except Exception:
            pass


async def _log_verify(text):
    """Post a diagnostic line to the verify log channel, if one is set."""
    ch = await resolve_channel(roblox_config.get("log_channel_id"))
    if not ch:
        return
    try:
        await ch.send(text[:1900])
    except Exception:
        pass


async def post_verify_panel():
    """(Re)post the Verify panel with the Roblox verify button.

    If the owner designed a custom panel in the dashboard (components), render
    that and attach the Verify button underneath. Otherwise post a default
    embed + button.
    """
    ch = await resolve_channel(roblox_config.get("channel_id"))
    if not ch:
        return

    btn_label = roblox_config.get("button_label") or "Verify"
    btn_style = roblox_config.get("button_style") or "primary"
    verify_row = {"type": "buttonRow", "buttons": [{"label": btn_label, "style": btn_style, "__verify": True}]}
    comps = roblox_config.get("components") or []

    def _with_button(source):
        # Tuck the Verify button inside a container (with the text) so it doesn't
        # dangle at the very bottom outside the box. Prefer the last container;
        # if the design has none, add it as a top-level sibling row.
        panel = [dict(c) for c in source]
        container_idxs = [i for i, c in enumerate(panel) if c.get("type") == "container"]
        if container_idxs:
            i = container_idxs[-1]
            panel[i] = dict(panel[i])
            panel[i]["children"] = list(panel[i].get("children") or []) + [verify_row]
        else:
            panel.append(verify_row)
        return panel

    if comps:
        _V2_LAST_ERROR["msg"] = ""
        # Attempt 1: the panel exactly as designed in the dashboard.
        try:
            mid = await send_v2_message(ch, _with_button(comps))
            if mid:
                print("[Verify] custom panel posted")
                await _replace_panel(ch.id, mid)
                return
        except Exception as e:
            print(f"[Verify] custom panel error: {e}")

        # Attempt 2: retry with media galleries removed — a rejected image URL
        # is the most common reason a Components V2 message fails to send.
        stripped = _strip_galleries(comps)
        if stripped != comps:
            try:
                mid = await send_v2_message(ch, _with_button(stripped))
                if mid:
                    print("[Verify] custom panel posted (images dropped — an image URL was rejected)")
                    await _replace_panel(ch.id, mid)
                    await _log_verify(f"⚠️ Verify panel posted without its image(s): Discord rejected the image URL. {_V2_LAST_ERROR['msg']}")
                    return
            except Exception as e:
                print(f"[Verify] stripped panel error: {e}")

        # Both attempts failed — surface the real reason instead of silently
        # posting an unrelated default that looks like 'a random thing'.
        print(f"[Verify] custom panel failed twice, using default. reason={_V2_LAST_ERROR['msg']}")
        await _log_verify(f"⚠️ Your custom Verify panel could not be posted, so the default was used. Reason: {_V2_LAST_ERROR['msg'] or 'unknown'}")
    else:
        # No components saved at all — tell the owner so they know the default
        # is showing because nothing was designed/saved, not because it broke.
        print("[Verify] no custom components saved — posting default panel")
        await _log_verify("ℹ️ No custom Verify panel was saved, so the default is being used. Design one in the dashboard and press Save changes.")

    embed = discord.Embed(
        title="Verify with Roblox",
        description="Click **Verify** to link your Roblox account. Once you're done, your nickname is set to your Roblox name and you get access to the server.",
        color=0x2B2D31,
    )
    _style_map = {
        "primary": discord.ButtonStyle.primary,
        "success": discord.ButtonStyle.success,
        "secondary": discord.ButtonStyle.secondary,
        "danger": discord.ButtonStyle.danger,
    }
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(
        label=(btn_label or "Verify")[:80],
        style=_style_map.get(btn_style, discord.ButtonStyle.primary),
        custom_id="roblox_verify",
    ))
    try:
        msg = await ch.send(embed=embed, view=view)
        await _replace_panel(ch.id, msg.id)
    except Exception as e:
        print(f"[Verify] panel post failed: {e}")


async def _robux_locker_call(action, amount=0, time_frame=None, **extra):
    """POST to the robux-locker edge function (funds / stock / rate / sales /
    purchase-log ops). `amount` may be fractional (the rate is dollars per 1k)."""
    payload = {"action": action, "amount": amount}
    if time_frame:
        payload["timeFrame"] = time_frame
    for k, v in extra.items():
        payload[k] = v
    try:
        async with _http() as client:
            r = await client.post(
                f"{SUPABASE_FN_URL}/robux-locker",
                headers=_fn_headers(),
                json=payload,
                timeout=20,
            )
            data = r.json() if r.content else {}
            if r.status_code == 200:
                return data
            return {"error": data.get("error") or f"HTTP {r.status_code}"}
    except Exception as e:
        return {"error": str(e)[:200]}


async def _devproduct_call(action, **extra):
    """POST to the roblox-devproduct edge function (find-or-create a Roblox
    developer product by name; the cookie lives server-side)."""
    payload = {"action": action}
    payload.update(extra)
    try:
        async with _http() as client:
            r = await client.post(
                f"{SUPABASE_FN_URL}/roblox-devproduct",
                headers=_fn_headers(),
                json=payload,
                timeout=25,
            )
            data = r.json() if r.content else {}
            if r.status_code == 200:
                return data
            return {"error": data.get("error") or f"HTTP {r.status_code}"}
    except Exception as e:
        return {"error": str(e)[:200]}


async def _payments_call(action, **extra):
    """POST an action to the payments-create edge function (Stripe purchase-log
    poller: stripe_recent / stripe_state_get / stripe_state_set)."""
    payload = {"action": action}
    for k, v in extra.items():
        payload[k] = v
    try:
        async with _http() as client:
            r = await client.post(
                f"{SUPABASE_FN_URL}/payments-create",
                headers=_fn_headers(),
                json=payload,
                timeout=20,
            )
            data = r.json() if r.content else {}
            if r.status_code == 200:
                return data
            return {"error": data.get("error") or f"HTTP {r.status_code}"}
    except Exception as e:
        return {"error": str(e)[:200]}


async def _replace_ticket_panel(new_channel_id, new_message_id):
    """Record the freshly-posted ticket panel per channel. Every save re-posts
    all panels, so we replace the previous message IN THE SAME channel (no
    duplicate stacking) while panels in other channels are untouched — you keep
    as many panels as you have channels."""
    refs = ticket_config.get("panel_refs")
    if not isinstance(refs, dict):
        refs = {}
        ticket_config["panel_refs"] = refs
    ch_key = str(new_channel_id)
    old_mid = refs.get(ch_key)
    if new_message_id and new_message_id is not True:
        refs[ch_key] = str(new_message_id)
    if old_mid and not _is_tracked_giveaway_message(old_mid):
        try:
            ch = await resolve_channel(ch_key)
            if ch:
                msg = await ch.fetch_message(int(old_mid))
                await msg.delete()
        except Exception:
            pass


async def post_ticket_panel(only_channel_id=None):
    """(Re)post ticket panels. With only_channel_id set (a save while editing one
    panel), post JUST that panel and leave the others untouched. Without it,
    post every configured panel."""
    panels = ticket_config.get("panels")
    if not isinstance(panels, list) or not panels:
        panels = [{"channel_id": ticket_config.get("panel_channel_id"), "components": ticket_config.get("panel_components") or []}]
    target = str(only_channel_id) if only_channel_id else None
    for p in panels:
        if target and str(p.get("channel_id")) != target:
            continue
        ch = await resolve_channel(p.get("channel_id"))
        if not ch:
            continue
        await _post_one_panel(ch, p.get("components") or [])


async def _post_one_panel(ch, comps):
    if comps:
        try:
            mid = await send_v2_message(ch, comps)
            if mid:
                print("[Tickets] panel posted")
                await _replace_ticket_panel(ch.id, mid)
                return
        except Exception as e:
            print(f"[Tickets] panel error: {e}")
        stripped = _strip_galleries(comps)
        if stripped != comps:
            try:
                mid = await send_v2_message(ch, stripped)
                if mid:
                    print("[Tickets] panel posted (images dropped)")
                    await _replace_ticket_panel(ch.id, mid)
                    return
            except Exception as e:
                print(f"[Tickets] stripped panel error: {e}")

    # Fallback: a classic embed with an Open Ticket button per type — used only
    # when a panel has no custom design (or the custom one wouldn't send).
    types = ticket_config.get("types") or [{"id": "support", "name": "Support", "button_label": "Open Ticket", "button_style": "primary"}]
    embed = discord.Embed(
        title="Support Tickets",
        description="Need help? Pick an option below and our team will be with you.",
        color=ACCENT,
    )
    _style_map = {
        "primary": discord.ButtonStyle.primary, "success": discord.ButtonStyle.success,
        "secondary": discord.ButtonStyle.secondary, "danger": discord.ButtonStyle.danger,
    }
    view = discord.ui.View(timeout=None)
    for t in types[:25]:
        view.add_item(discord.ui.Button(
            label=(t.get("button_label") or "Open Ticket")[:80],
            style=_style_map.get(t.get("button_style") or "primary", discord.ButtonStyle.primary),
            custom_id=f"ticket_cat:{(t.get('id') or 'support')[:80]}",
        ))
    try:
        msg = await ch.send(embed=embed, view=view)
        await _replace_ticket_panel(ch.id, msg.id)
    except Exception as e:
        print(f"[Tickets] panel post failed: {e}")


async def _replace_saved_message(channel_id, message_id):
    """Track the live saved-message per channel and delete the previous one, so
    re-saving a message edits it in place (no duplicate stacking) — same idea as
    ticket panels."""
    refs = saved_messages_config.get("refs")
    if not isinstance(refs, dict):
        refs = {}
        saved_messages_config["refs"] = refs
    ch_key = str(channel_id)
    old_mid = refs.get(ch_key)
    if message_id and message_id is not True:
        refs[ch_key] = str(message_id)
    if old_mid and not _is_tracked_giveaway_message(old_mid):
        try:
            ch = await resolve_channel(ch_key)
            if ch:
                msg = await ch.fetch_message(int(old_mid))
                await msg.delete()
        except Exception:
            pass


async def post_saved_messages(only_channel_id=None):
    """(Re)post saved messages. With only_channel_id set (a save while editing one
    message), post JUST that one and leave the others alone; without it, post
    every saved message."""
    target = str(only_channel_id) if only_channel_id else None
    for m in saved_messages_config.get("messages") or []:
        if target and str(m.get("channel_id")) != target:
            continue
        ch = await resolve_channel(m.get("channel_id"))
        if not ch:
            continue
        comps = m.get("components") or []
        if not comps:
            continue
        try:
            mid = await send_v2_message(ch, comps)
            if mid:
                print("[Messages] saved message posted")
                await _replace_saved_message(ch.id, mid)
                continue
        except Exception as e:
            print(f"[Messages] post error: {e}")
        stripped = _strip_galleries(comps)
        if stripped != comps:
            try:
                mid = await send_v2_message(ch, stripped)
                if mid:
                    print("[Messages] saved message posted (images dropped)")
                    await _replace_saved_message(ch.id, mid)
            except Exception as e:
                print(f"[Messages] stripped post error: {e}")


# ---------------- Advertisement claim -> approval -> posting ----------------

def _is_ads_staff(member):
    try:
        if member.guild_permissions.manage_guild:
            return True
    except Exception:
        pass
    return has_any_role(member, ads_config.get("staff_role_ids", []))


def _ads_inventory_text(inv):
    owned = [f"• **{_ads_perk_label(k)}** × {inv[k]}" for k in ADS_PERK_KEYS if inv.get(k)]
    return "\n".join(owned) if owned else "Empty — buy a ping credit from the ad shop to post."


_INV_LIST_RE = re.compile(r"\{inventory[ _]list\}", re.IGNORECASE)


def _ads_inventory_cards(guild_id, user_id):
    """One card (Section: name + a disabled 'Quantity | N' button) per owned perk
    — what the {inventory list} token expands into."""
    inv = _ads_inventory(guild_id, user_id)
    cards = []
    for k in ADS_PERK_KEYS:
        n = inv.get(k)
        if n:
            cards.append({
                "type": "section",
                "title": _ads_perk_label(k),
                "text": "",
                "button": {"label": f"Quantity | {n}", "disabled": True, "style": "secondary"},
            })
    if not cards:
        cards.append({"type": "text", "text": "*Your inventory is empty.*"})
    return cards


def _ads_fill_quantities(tree, guild_id, user_id):
    """Fill {quantity} on Purchase 'Quantity' cards from the viewer's own
    inventory (matched by the card's title to a perk), and HIDE any card they
    don't own (count 0). Containers left with nothing meaningful are dropped too."""
    inv = _ads_inventory(guild_id, user_id)

    def _walk(items):
        out = []
        for it in (items or []):
            if not isinstance(it, dict):
                out.append(it)
                continue
            it = dict(it)
            if it.get("type") == "container":
                kids = it.get("children") if isinstance(it.get("children"), list) else it.get("components")
                new_kids = _walk(kids or [])
                meaningful = [k for k in new_kids if isinstance(k, dict) and k.get("type") != "separator"]
                if not meaningful:
                    continue  # container emptied out (its quantity cards were all 0)
                it["children"] = new_kids
                it.pop("components", None)
                out.append(it)
            elif it.get("type") == "purchase" and it.get("quantity"):
                perk = _ads_perk_for_name(it.get("title") or "")
                n = int(inv.get(perk, 0)) if perk else 0
                if n <= 0:
                    continue  # they don't own this — hide the card
                lbl = it.get("button_label") or "Quantity | {quantity}"
                it["button_label"] = lbl.replace("{quantity}", str(n)).replace("{Quantity}", str(n))
                out.append(it)
            else:
                out.append(it)
        return out

    return _walk(tree)


def _ads_expand_inventory(tree, guild_id, user_id):
    """Replace any {inventory list} text node in a V2 tree with the per-perk cards
    (done at the component level since it becomes real Section components)."""
    cards = None

    def _has_token(item):
        if not isinstance(item, dict):
            return False
        t = item.get("text") or item.get("content") or ""
        return isinstance(t, str) and bool(_INV_LIST_RE.search(t))

    def _expand(items):
        nonlocal cards
        out = []
        for it in (items or []):
            if isinstance(it, dict) and it.get("type") == "container":
                it = dict(it)
                kids = it.get("children") if isinstance(it.get("children"), list) else it.get("components")
                it["children"] = _expand(kids or [])
                it.pop("components", None)
                out.append(it)
            elif _has_token(it):
                if cards is None:
                    cards = _ads_inventory_cards(guild_id, user_id)
                out.extend(cards)
            else:
                out.append(it)
        return out

    return _expand(tree)


def _ads_summary(ad):
    lines = [f"**By:** <@{ad.get('user_id')}>", f"**Ping:** {_ads_perk_label(ad.get('ping'))}"]
    if ad.get("addon"):
        lines.append(f"**Add-on:** {_ads_perk_label(ad.get('addon'))}")
    if ad.get("type") == "giveaway":
        lines.append(f"**Type:** Sponsored Giveaway\n**Prize:** {ad.get('prize')}\n"
                     f"**Winners:** {ad.get('winners')}\n**Length:** {ad.get('length')}\n"
                     f"**Discord:** {ad.get('server_link') or '—'}")
    else:
        lines.append(f"**Type:** Regular Post\n**Link:** {ad.get('server_link')}")
    return "\n".join(lines)


def _ads_render(design, tokens):
    """Deep-fill {tokens} in a V2 design tree so it's ready for send_v2_message."""
    if not design:
        return []
    raw = json.dumps(design)
    for k, v in tokens.items():
        raw = raw.replace("{" + k + "}", json.dumps(str(v))[1:-1])
    try:
        return json.loads(raw)
    except Exception:
        return design


async def _ad_invite_valid(link):
    """True if the ad's Discord invite still resolves. Only a genuine NotFound
    (expired/invalid) is treated as bad — transient API errors are assumed OK so
    a rate-limit doesn't wrongly flag a working invite."""
    code = (link or "").strip()
    if not code:
        return False
    try:
        inv = await bot.fetch_invite(code)
        return inv is not None
    except discord.NotFound:
        return False
    except Exception:
        return True  # transient (rate limit, network) — don't flag


async def _ad_invite_warn_dm(guild, ad, position, ts):
    """DM the advertiser: a preview of how their ad will look in the channel,
    then a yellow ATTENTION embed telling them the invite is expired/invalid."""
    uid = str(ad.get("user_id") or "")
    if not uid.isdigit():
        return
    user = guild.get_member(int(uid))
    if user is None:
        try:
            user = await bot.fetch_user(int(uid))
        except Exception:
            return
    try:
        dm = await user.create_dm()
    except Exception:
        return
    advertiser = f"<@{uid}>"
    # 1) Preview of the ad (no ping in DMs).
    if ad.get("type") == "giveaway":
        design = _ads_render(ads_config.get("giveaway_design") or [],
                             {"advertiser": advertiser, "prize": ad.get("prize") or "",
                              "winners": ad.get("winners") or 1, "duration": ad.get("length") or "",
                              "ping": "", "server_link": ad.get("server_link") or "",
                              "server_name": ad.get("server_name") or ""})
    else:
        design = _ads_render(ads_config.get("regular_design") or [],
                             {"advertiser": advertiser, "server_link": ad.get("server_link") or "", "ping": ""})
    if design:
        try:
            await send_v2_message(dm, design)
        except Exception:
            pass
    # 2) Yellow ATTENTION embed.
    typ = "Sponsored Giveaway" if ad.get("type") == "giveaway" else "Regular Post"
    desc = (
        "## **ATTENTION NEEDED**\n\n"
        "The invite linked to your advertisement is currently **expired or invalid**.\n\n"
        "Please provide an updated invite before your scheduled posting date to prevent any "
        "delays with your advertisement.\n\n"
        f"**User:** <@{uid}>\n"
        f"**Ad Type:** {typ}\n"
        f"**Invite:** {ad.get('server_link') or '—'}\n"
        f"**Scheduled Date:** <t:{int(ts)}:F>\n"
        f"**Queue Position:** {position}"
    )
    view = None
    if ad.get("id"):
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(
            label="Update Invite", style=discord.ButtonStyle.primary,
            custom_id=f"adinv:{guild.id}:{ad.get('id')}"))
    try:
        await dm.send(embed=discord.Embed(description=desc, color=0xF1C40F), view=view)
    except Exception:
        pass


def _ads_find_by_id(gid, ad_id):
    """Find an ad by its id across a guild's bypass, queue, and pending."""
    gd = ads_data.get(str(gid)) or {}
    for lane in ("bypass", "queue"):
        for ad in (gd.get(lane) or []):
            if str(ad.get("id")) == str(ad_id):
                return ad
    for ad in (gd.get("pending") or {}).values():
        if str(ad.get("id")) == str(ad_id):
            return ad
    return None


class AdUpdateInviteModal(discord.ui.Modal):
    """Opened from the 'Update Invite' button in the expired-invite DM. Sets a new
    invite on the queued ad in place — no resubmit, no lost queue spot."""
    def __init__(self, gid, ad_id):
        super().__init__(title="Update Invite", timeout=600)
        self._gid = str(gid)
        self._ad_id = str(ad_id)
        self.link = discord.ui.TextInput(
            label="New server invite link", placeholder="https://discord.gg/…",
            required=True, max_length=200)
        self.add_item(self.link)

    async def on_submit(self, interaction):
        ad = _ads_find_by_id(self._gid, self._ad_id)
        if not ad:
            await interaction.response.send_message(embed=error_embed(
                "Not found", "That ad is no longer in the queue — it may have already posted. "
                "Submit a new ad if you'd like to advertise again."), ephemeral=True)
            return
        new_link = _normalize_invite(self.link.value)
        if not await _ad_invite_valid(new_link):
            await interaction.response.send_message(embed=error_embed(
                "Still invalid", f"`{new_link}` didn't work either — make sure it's a live, "
                "non-expiring invite, then try the button again."), ephemeral=True)
            return
        ad["server_link"] = new_link
        ad.pop("invite_flagged", None)
        try:
            inv = await bot.fetch_invite(new_link.strip())
            ad["server_name"] = (inv.guild.name if inv and inv.guild else "") or ""
        except Exception:
            pass
        await _ads_flush_now()
        await interaction.response.send_message(embed=success_embed(
            "Invite updated", f"Your advertisement now points to `{new_link}`. "
            "It'll post on schedule — no need to resubmit."), ephemeral=True)


def _ads_date_snapshot(guild):
    """{ad_id -> estimated post unix ts} for everything currently queued — taken
    before a queue change so we can tell whose posting time moved."""
    return {str(a.get("id")): ts for a, lane, ts in _ads_queue_entries(guild)}


async def _ads_reschedule_dm(guild, ad, new_ts, old_ts, position):
    """DM the advertiser that their posting time moved later because a
    higher-priority ad jumped ahead of them."""
    uid = str(ad.get("user_id") or "")
    if not uid.isdigit():
        return
    user = guild.get_member(int(uid))
    if user is None:
        try:
            user = await bot.fetch_user(int(uid))
        except Exception:
            return
    try:
        dm = await user.create_dm()
    except Exception:
        return
    link = ad.get("server_link") or ""
    sname = ad.get("server_name") or "Server"
    members = 0
    try:
        inv = await bot.fetch_invite(link.strip(), with_counts=True)
        if inv:
            sname = (inv.guild.name if inv.guild else "") or sname
            members = int(inv.approximate_member_count or 0)
    except Exception:
        pass
    desc = (
        "### **Advertisement Rescheduled**\n\n"
        "An adjustment has been made to the posting schedule for your advertisement. Your "
        "booking is still active and confirmed, but the expected publishing date has been moved.\n\n"
        "> **New Posting Time**\n"
        f"> <t:{int(new_ts)}:F>\n\n"
        "**Originally Scheduled For**\n"
        f"<t:{int(old_ts)}:F>\n\n"
        "**Queue Placement**\n"
        f"`#{position}`\n\n"
        "There is nothing you need to do at this time. Your advertisement will remain in the "
        "queue and will be posted automatically at its newly assigned time.\n\n"
        f"**{sname}**\n"
        f"`{members:,} members`\n"
        f"{link}"
    )
    tree = [{"type": "container", "children": [{"type": "text", "text": desc}]}]
    try:
        if not await send_v2_message(dm, tree):
            await dm.send(embed=discord.Embed(description=desc, color=ACCENT))
    except Exception:
        pass


async def _ads_notify_reschedules(guild, before):
    """After a queue change, DM anyone whose estimated post time moved later."""
    for i, (ad, lane, ts) in enumerate(_ads_queue_entries(guild)):
        old = before.get(str(ad.get("id")))
        if old is not None and ts > old + 30:  # moved meaningfully later
            await _ads_reschedule_dm(guild, ad, ts, old, i + 1)


async def _ads_post(guild, ad):
    """Post an approved ad to the configured ad channel."""
    ch = await resolve_channel(ads_config.get("post_channel_id"))
    if not ch:
        print("[Ads] no post channel configured")
        return False
    ping = _ADS_PING_CONTENT.get(ad.get("ping"), "")
    advertiser = f"<@{ad.get('user_id')}>"
    mentions = {"parse": ["everyone", "users", "roles"]}
    if ad.get("type") == "giveaway":
        prize = ad.get("prize") or "a prize"
        winners = int(ad.get("winners") or 1)
        seconds = int(ad.get("seconds") or 86400)
        length = ad.get("length") or ""
        design = _ads_render(ads_config.get("giveaway_design") or [],
                             {"advertiser": advertiser, "prize": prize, "winners": winners,
                              "duration": length, "ping": ping,
                              "server_link": ad.get("server_link") or "",
                              "server_name": ad.get("server_name") or ""}) or None
        if ping:
            try:
                await ch.send(ping, allowed_mentions=discord.AllowedMentions(everyone=True, users=True, roles=True))
            except Exception:
                pass
        await start_giveaway(ch, prize, winners, seconds, ad.get("user_id"), guild.id, design=design, length=length)
        return True
    design = _ads_render(ads_config.get("regular_design") or [],
                         {"advertiser": advertiser, "server_link": ad.get("server_link") or "", "ping": ping})
    if design:
        try:
            await send_v2_message(ch, design, content=(ping or None), allowed_mentions=mentions)
            return True
        except Exception as e:
            print(f"[Ads] regular post failed: {e}")
    embed = discord.Embed(title="New Advertisement",
                          description=f"{advertiser} shared:\n{ad.get('server_link') or ''}", color=ACCENT)
    try:
        await ch.send(content=(ping or None), embed=embed,
                      allowed_mentions=discord.AllowedMentions(everyone=True, users=True, roles=True))
        return True
    except Exception as e:
        print(f"[Ads] fallback post failed: {e}")
        return False


async def _ads_submit(interaction, ad):
    """Spend the ping (+ optional add-on), then send the ad for staff approval."""
    gid = interaction.guild.id
    uid = interaction.user.id
    if not _ads_consume(gid, uid, ad["ping"]):
        await interaction.response.send_message(embed=error_embed("Out of stock", "You no longer have that ping credit."), ephemeral=True)
        return
    if ad.get("addon"):
        if not _ads_consume(gid, uid, ad["addon"]):
            _ads_grant(gid, uid, ad["ping"])
            await interaction.response.send_message(embed=error_embed("Out of stock", "You no longer have that add-on."), ephemeral=True)
            return
    # Resolve the advertised server's name from its invite (for the queue list).
    if ad.get("server_link"):
        try:
            inv = await bot.fetch_invite(ad["server_link"].strip())
            ad["server_name"] = (inv.guild.name if inv and inv.guild else "") or ""
        except Exception:
            ad["server_name"] = ""
    ad_id = secrets.token_hex(6)
    ad["id"] = ad_id
    ad["guild_id"] = str(gid)
    ad["user_id"] = str(uid)
    _ads_g(gid).setdefault("pending", {})[ad_id] = ad
    # Persist NOW (durably, with retries) so a redeploy right after submitting
    # can't lose the pending ad and leave the approval buttons dead.
    await _ads_flush_now()
    appr = await resolve_channel(ads_config.get("approval_channel_id"))
    if appr:
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(label="Approve", style=discord.ButtonStyle.success, custom_id=f"ad_ok:{ad_id}"))
        view.add_item(discord.ui.Button(label="Deny", style=discord.ButtonStyle.danger, custom_id=f"ad_no:{ad_id}"))
        view.add_item(discord.ui.Button(label="Delay", style=discord.ButtonStyle.secondary, custom_id=f"ad_delay:{ad_id}"))
        try:
            await appr.send(embed=info_embed("Ad awaiting approval", _ads_summary(ad)), view=view)
        except Exception as e:
            print(f"[Ads] approval send failed: {e}")
    await interaction.response.send_message(
        embed=success_embed("Submitted!", "Your ad was sent for staff approval — you'll see it posted once approved."), ephemeral=True)


def _ads_reconstruct_from_embed(desc):
    """Rebuild an ad from its approval-message embed — the durable fallback so
    staff can approve even if the stored pending record was lost in a restart.
    Returns None if the embed can't be parsed into a usable ad."""
    if not desc:
        return None

    def field(name):
        m = re.search(rf"\*\*{re.escape(name)}:\*\*\s*(.+)", desc)
        return m.group(1).strip() if m else ""

    m = re.search(r"<@!?(\d+)>", desc)
    user_id = m.group(1) if m else ""
    ping = _ads_perk_for_name(field("Ping"))
    if not (user_id and ping):
        return None
    addon_label = field("Add-on")
    ad = {
        "user_id": user_id,
        "ping": ping,
        "addon": _ads_perk_for_name(addon_label) if addon_label else None,
        "type": "giveaway" if "giveaway" in field("Type").lower() else "regular",
    }
    if ad["type"] == "giveaway":
        ad["prize"] = field("Prize")
        try:
            ad["winners"] = max(1, int(re.sub(r"\D", "", field("Winners")) or "1"))
        except ValueError:
            ad["winners"] = 1
        ad["length"] = field("Length")
        ad["server_link"] = field("Discord")
        ad["seconds"] = _parse_duration_seconds(ad["length"]) or 86400
    else:
        ad["server_link"] = field("Link")
    return ad


async def _ads_decide(interaction, ad_id, approve):
    gid = str(interaction.guild.id)
    gd = _ads_g(gid)
    if not _is_ads_staff(interaction.user):
        await interaction.response.send_message(embed=error_embed("No permission", "Only ad staff can do that."), ephemeral=True)
        return
    ad = (gd.get("pending") or {}).pop(ad_id, None)
    if not ad:
        # Storage lost the pending record (e.g. a redeploy during a backend
        # outage), but the approval message is a durable copy — rebuild from its
        # embed. Only if it's still awaiting (title unchanged), so an
        # already-handled ad can't be approved twice.
        emb = interaction.message.embeds[0] if (interaction.message and interaction.message.embeds) else None
        if emb and "awaiting" in ((emb.title or "").lower()):
            ad = _ads_reconstruct_from_embed(emb.description or "")
        if ad:
            ad["id"] = ad_id
            ad["guild_id"] = gid
        else:
            await interaction.response.send_message(embed=error_embed(
                "Not pending anymore",
                "This ad was already approved or denied. If it went missing after a restart, "
                "ask the advertiser to submit it again."),
                ephemeral=True)
            try:
                await interaction.message.edit(view=None)  # retire the dead buttons
            except Exception:
                pass
            return
    await _ads_flush_now()
    if not approve:
        _ads_grant(gid, ad["user_id"], ad["ping"])
        if ad.get("addon"):
            _ads_grant(gid, ad["user_id"], ad["addon"])
        try:
            await interaction.response.edit_message(embed=info_embed("Ad denied", _ads_summary(ad) + "\n\n**Denied** — perks refunded."), view=None)
        except Exception:
            pass
        return
    # Snapshot everyone's estimated post time so we can tell whose time moves when
    # this approval jumps the queue (a Bypass ad pushes the normal queue back).
    before = _ads_date_snapshot(interaction.guild)
    addon = ad.get("addon")
    if addon == "instant":
        try:
            await interaction.response.edit_message(embed=info_embed("Ad approved", _ads_summary(ad) + "\n\n**Posted instantly.**"), view=None)
        except Exception:
            pass
        await _ads_post(interaction.guild, ad)
    elif addon == "bypass":
        gd.setdefault("bypass", []).append(ad)
        await _ads_flush_now()
        try:
            await interaction.response.edit_message(embed=info_embed("Ad approved", _ads_summary(ad) + "\n\n**Queued — Bypass lane** (posts before the regular queue)."), view=None)
        except Exception:
            pass
        await _ads_notify_reschedules(interaction.guild, before)
    else:
        gd.setdefault("queue", []).append(ad)
        await _ads_flush_now()
        try:
            await interaction.response.edit_message(embed=info_embed("Ad approved", _ads_summary(ad) + "\n\n**Queued.**"), view=None)
        except Exception:
            pass


def _ads_parse_delay_date(raw):
    """Accepts 9/2/2026, 09-02-2026 or 2026-09-02 → unix ts at 16:00 UTC that
    day (mid-day across US timezones, so the queue's <t:…> stamp reads as the
    day staff picked). Returns None if unparseable."""
    t = (raw or "").strip()
    m = re.match(r"^(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})$", t)
    if m:
        mo, d, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        m = re.match(r"^(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})$", t)
        if not m:
            return None
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        return int(datetime.datetime(y, mo, d, 16, 0, tzinfo=datetime.timezone.utc).timestamp())
    except ValueError:
        return None


async def _ads_retrofit_delay_buttons():
    """One-shot boot pass: any 'Ad awaiting approval' card posted before the
    Delay feature shipped only has Approve/Deny — edit those messages to add
    the Delay button so staff can schedule ads that were already pending."""
    ch = await resolve_channel(ads_config.get("approval_channel_id"))
    if not ch:
        return
    fixed = 0
    try:
        async for msg in ch.history(limit=100):
            if msg.author.id != (bot.user.id if bot.user else 0) or not msg.embeds:
                continue
            if "awaiting approval" not in ((msg.embeds[0].title or "").lower()):
                continue  # already approved/denied cards have a different title
            ids = [c.custom_id for row in msg.components for c in getattr(row, "children", []) if getattr(c, "custom_id", None)]
            if not ids or any(i.startswith("ad_delay:") for i in ids):
                continue  # no buttons left, or already retrofitted
            ok = next((i for i in ids if i.startswith("ad_ok:")), None)
            if not ok:
                continue
            ad_id = ok.split(":", 1)[1]
            view = discord.ui.View(timeout=None)
            view.add_item(discord.ui.Button(label="Approve", style=discord.ButtonStyle.success, custom_id=f"ad_ok:{ad_id}"))
            view.add_item(discord.ui.Button(label="Deny", style=discord.ButtonStyle.danger, custom_id=f"ad_no:{ad_id}"))
            view.add_item(discord.ui.Button(label="Delay", style=discord.ButtonStyle.secondary, custom_id=f"ad_delay:{ad_id}"))
            try:
                await msg.edit(view=view)
                fixed += 1
            except Exception as e:
                print(f"[Ads] delay-button retrofit edit failed: {e}")
    except Exception as e:
        print(f"[Ads] delay-button retrofit scan failed: {e}")
    if fixed:
        print(f"[Ads] added Delay button to {fixed} pending approval card(s)")


class AdDelayModal(discord.ui.Modal):
    """Staff picks the day a pending ad should post. The ad is approved into
    the queue with that date pinned — everything behind it fills the spots
    before it (see _ads_queue_entries / _ads_pop_postable)."""

    def __init__(self, ad_id):
        super().__init__(title="Delay this ad", timeout=600)
        self._ad_id = ad_id
        self.date = discord.ui.TextInput(
            label="Post date (M/D/YYYY)", placeholder="9/2/2026", required=True, max_length=20)
        self.add_item(self.date)

    async def on_submit(self, interaction):
        await _ads_delay_submit(interaction, self._ad_id, str(self.date.value or ""))


async def _ads_open_delay(interaction, ad_id):
    if not _is_ads_staff(interaction.user):
        await interaction.response.send_message(embed=error_embed("No permission", "Only ad staff can do that."), ephemeral=True)
        return
    await interaction.response.send_modal(AdDelayModal(ad_id))


async def _ads_delay_submit(interaction, ad_id, raw_date):
    gid = str(interaction.guild.id)
    gd = _ads_g(gid)
    ts = _ads_parse_delay_date(raw_date)
    if ts is None:
        await interaction.response.send_message(embed=error_embed(
            "Couldn't read that date", "Use M/D/YYYY — for example **9/2/2026**."), ephemeral=True)
        return
    if ts < int(time.time()) - 12 * 3600:  # "today" stays valid even past 16:00 UTC
        await interaction.response.send_message(embed=error_embed(
            "Date already passed", "Pick today or a future date."), ephemeral=True)
        return
    ad = (gd.get("pending") or {}).pop(ad_id, None)
    if not ad:
        # Same durable-copy recovery as Approve/Deny: rebuild from the approval
        # embed if storage lost the pending record across a redeploy.
        emb = interaction.message.embeds[0] if (interaction.message and interaction.message.embeds) else None
        if emb and "awaiting" in ((emb.title or "").lower()):
            ad = _ads_reconstruct_from_embed(emb.description or "")
        if ad:
            ad["id"] = ad_id
            ad["guild_id"] = gid
        else:
            await interaction.response.send_message(embed=error_embed(
                "Not pending anymore",
                "This ad was already approved or denied. If it went missing after a restart, "
                "ask the advertiser to submit it again."),
                ephemeral=True)
            try:
                await interaction.message.edit(view=None)
            except Exception:
                pass
            return
    ad["not_before"] = ts
    lane = "bypass" if ad.get("addon") == "bypass" else "queue"
    gd.setdefault(lane, []).append(ad)
    await _ads_flush_now()
    try:
        await interaction.response.edit_message(
            embed=info_embed("Ad approved", _ads_summary(ad)
                             + f"\n\n**Scheduled** — posts <t:{ts}:D>. The queue fills the spots before it."),
            view=None)
    except Exception:
        pass


class AdRegularModal(discord.ui.Modal):
    def __init__(self, state):
        super().__init__(title="Regular Post", timeout=600)
        self._state = state
        self.link = discord.ui.TextInput(label="Server invite link", placeholder="https://discord.gg/…", required=True, max_length=200)
        self.add_item(self.link)

    async def on_submit(self, interaction):
        ad = dict(self._state)
        ad["type"] = "regular"
        ad["server_link"] = _normalize_invite(self.link.value)
        await _ads_submit(interaction, ad)


class AdGiveawayModal(discord.ui.Modal):
    def __init__(self, state):
        super().__init__(title="Sponsored Giveaway", timeout=600)
        self._state = state
        self.link = discord.ui.TextInput(label="Your Discord server link", placeholder="https://discord.gg/…", required=True, max_length=200)
        self.prize = discord.ui.TextInput(label="Prize", required=True, max_length=200)
        self.winners = discord.ui.TextInput(label="Winners", placeholder="1", required=True, max_length=3)
        self.length = discord.ui.TextInput(label="Length", placeholder="1d, 12h, 30m", required=True, max_length=20)
        self.add_item(self.link)
        self.add_item(self.prize)
        self.add_item(self.winners)
        self.add_item(self.length)

    async def on_submit(self, interaction):
        try:
            winners = max(1, int((self.winners.value or "1").strip()))
        except ValueError:
            winners = 1
        seconds = _parse_duration_seconds((self.length.value or "").strip()) or 86400
        ad = dict(self._state)
        ad["type"] = "giveaway"
        ad["prize"] = (self.prize.value or "").strip()
        ad["winners"] = winners
        ad["seconds"] = seconds
        ad["length"] = (self.length.value or "").strip()
        ad["server_link"] = _normalize_invite(self.link.value)
        await _ads_submit(interaction, ad)


class AdDetailsView(discord.ui.View):
    """Bridge between the two forms. Discord won't let a modal submit open another
    modal directly, so after the first form (type + Terms) we hand the member a
    one-tap Continue button that opens the matching details form."""
    def __init__(self, state):
        super().__init__(timeout=300)
        self._state = state
        b = discord.ui.Button(label="Continue", style=discord.ButtonStyle.primary)
        b.callback = self._go
        self.add_item(b)

    async def _go(self, interaction):
        if self._state.get("type") == "giveaway":
            await interaction.response.send_modal(AdGiveawayModal(self._state))
        else:
            await interaction.response.send_modal(AdRegularModal(self._state))


class AdStartModal(discord.ui.Modal):
    """First form after picking a ping credit: choose Regular Post vs Sponsored
    Giveaway and agree to the Advertisement Terms of Service. On submit it hands
    over the Continue button that opens the details form (see AdDetailsView)."""
    def __init__(self, ping):
        super().__init__(title="Post an Ad", timeout=600)
        self._ping = ping
        self.kind = discord.ui.Select(custom_id="kind", min_values=1, max_values=1, options=[
            discord.SelectOption(label=(ads_config.get("regular_label") or "Regular Post")[:100], value="regular", default=True),
            discord.SelectOption(label=(ads_config.get("giveaway_label") or "Sponsored Giveaway")[:100], value="giveaway"),
        ])
        self.agree = discord.ui.Checkbox(custom_id="agree")
        self.add_item(discord.ui.Label(text="Post type", description="Regular Post or Sponsored Giveaway.", component=self.kind))
        self.add_item(discord.ui.Label(
            text="Advertisement Terms of Service",
            description=f"I agree to the {BRAND} Advertisement Terms of Service.",
            component=self.agree))

    async def on_submit(self, interaction):
        if not self.agree.value:
            await interaction.response.send_message(
                embed=error_embed("Agreement required",
                    f"You must agree to the {BRAND} Advertisement Terms of Service before posting."),
                ephemeral=True)
            return
        kind = self.kind.values[0] if self.kind.values else "regular"
        state = {"ping": self._ping, "type": kind, "addon": None}
        # Option 1: no embed — just a bare Continue button (Discord won't let a
        # modal submit open a modal directly, so this one tap bridges the forms).
        await interaction.response.send_message(view=AdDetailsView(state), ephemeral=True)


class ApplyAddonView(discord.ui.View):
    """Pick which of your active posts an add-on applies to."""
    def __init__(self, addon, posts):
        super().__init__(timeout=180)
        self._addon = addon
        opts = []
        for loc, ad in posts[:25]:
            typ = "Sponsored Giveaway" if ad.get("type") == "giveaway" else "Regular Post"
            status = {"pending": "awaiting approval", "queue": "queued", "bypass": "bypass lane"}.get(loc, loc)
            sub = ad.get("prize") if ad.get("type") == "giveaway" else (ad.get("server_name") or ad.get("server_link") or "")
            opts.append(discord.SelectOption(label=f"{typ} · {status}"[:100], value=str(ad.get("id")), description=(sub or "")[:100]))
        self.sel = discord.ui.Select(placeholder=f"Apply {_ads_perk_label(addon)} to…"[:150], options=opts, min_values=1, max_values=1)
        self.sel.callback = self._apply
        self.add_item(self.sel)

    async def _apply(self, interaction):
        await _ads_apply_addon(interaction, self._addon, self.sel.values[0])


def _ads_user_active_posts(guild_id, user_id):
    """This member's posts that haven't gone out yet: pending approval, queued,
    or in the bypass lane."""
    gd = ads_data.get(str(guild_id)) or {}
    out = []
    for ad in (gd.get("pending") or {}).values():
        if str(ad.get("user_id")) == str(user_id):
            out.append(("pending", ad))
    for ad in (gd.get("bypass") or []):
        if str(ad.get("user_id")) == str(user_id):
            out.append(("bypass", ad))
    for ad in (gd.get("queue") or []):
        if str(ad.get("user_id")) == str(user_id):
            out.append(("queue", ad))
    return out


async def _ads_apply_addon(interaction, addon, ad_id):
    gid = interaction.guild.id
    uid = interaction.user.id
    gd = _ads_g(gid)
    ad, loc = None, None
    if ad_id in (gd.get("pending") or {}):
        ad, loc = gd["pending"][ad_id], "pending"
    if not ad:
        for a in (gd.get("bypass") or []):
            if str(a.get("id")) == str(ad_id):
                ad, loc = a, "bypass"
                break
    if not ad:
        for a in (gd.get("queue") or []):
            if str(a.get("id")) == str(ad_id):
                ad, loc = a, "queue"
                break
    if not ad or str(ad.get("user_id")) != str(uid):
        await interaction.response.send_message(embed=error_embed("Not found", "That post is no longer active."), ephemeral=True)
        return
    if addon == "bypass" and loc == "bypass":
        await interaction.response.send_message(embed=info_embed("Already priority", "That post is already in the Bypass lane."), ephemeral=True)
        return
    if not _ads_consume(gid, uid, addon):
        await interaction.response.send_message(embed=error_embed("Out of stock", "You don't have that add-on anymore."), ephemeral=True)
        return
    if loc == "pending":
        ad["addon"] = addon
        _save_ads_soon()
        await interaction.response.send_message(
            embed=success_embed("Applied", f"**{_ads_perk_label(addon)}** will apply as soon as staff approve this post."), ephemeral=True)
        return
    # Already approved (queued / bypass lane).
    if addon == "instant":
        try:
            gd.get(loc, []).remove(ad)
        except Exception:
            pass
        _save_ads_soon()
        await interaction.response.send_message(embed=success_embed("Applied", "Posting it now."), ephemeral=True)
        await _ads_post(interaction.guild, ad)
    else:  # bypass, from the normal queue
        before = _ads_date_snapshot(interaction.guild)
        try:
            gd["queue"].remove(ad)
        except Exception:
            pass
        gd.setdefault("bypass", []).append(ad)
        await _ads_flush_now()
        await interaction.response.send_message(embed=success_embed("Applied", "Moved to the Bypass lane — it'll post sooner."), ephemeral=True)
        await _ads_notify_reschedules(interaction.guild, before)


async def _ads_handle_use(interaction, v):
    """A member picked an item from the claim dropdown."""
    gid = interaction.guild.id
    uid = interaction.user.id
    if not v or v == "_none":
        try:
            await interaction.response.defer()
        except Exception:
            pass
        return
    if int(_ads_inventory(gid, uid).get(v, 0)) <= 0:
        await interaction.response.send_message(embed=error_embed("Out of stock", "You don't have that item."), ephemeral=True)
        return
    if v in ADS_PING_KEYS:
        # Dropdown is a component interaction, so it can open the first form
        # (post type + Terms of Service) directly.
        await interaction.response.send_modal(AdStartModal(v))
        return
    # Add-on (Instant Post / Bypass Queue) — apply to an active post.
    posts = _ads_user_active_posts(gid, uid)
    if not posts:
        empty = ads_config.get("noposts_design") or []
        if empty:
            tree = _ads_render(empty, {"user": interaction.user.mention})
            await interaction.response.defer(ephemeral=True)
            if not await send_v2_message(interaction.channel, tree, interaction=interaction, ephemeral=True):
                await interaction.followup.send(embed=info_embed("No current posts available", "If this is a mistake please contact support."), ephemeral=True)
            return
        embed = discord.Embed(title="No current posts available",
                              description="If this is a mistake please contact support.", color=ACCENT)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    await interaction.response.send_message(
        embed=info_embed("Apply add-on", f"Pick which of your posts to apply **{_ads_perk_label(v)}** to:"),
        view=ApplyAddonView(v, posts), ephemeral=True)


def _ads_claim_rows(guild_id, user_id):
    """A single 'use an item' dropdown listing everything the member owns."""
    inv = _ads_inventory(guild_id, user_id)
    opts = [{"label": f"{_ads_perk_label(k)} ({inv[k]})", "value": k} for k in ADS_PERK_KEYS if inv.get(k)]
    if not opts:
        opts = [{"label": "Nothing to claim", "value": "_none"}]
    return [{"type": 1, "components": [{"type": 3, "custom_id": "adsel_use",
        "placeholder": (ads_config.get("ping_placeholder") or "Choose an item to use")[:150],
        "min_values": 1, "max_values": 1, "options": opts}]}]


async def _ads_open_claim(interaction):
    if not ads_config.get("enabled"):
        await interaction.response.send_message(embed=error_embed("Ads are off", "The advertisement system isn't set up yet."), ephemeral=True)
        return
    gid, uid = interaction.guild.id, interaction.user.id
    inv = _ads_inventory(gid, uid)
    title = ads_config.get("claim_title") or "Your Ad Inventory"
    note = ads_config.get("claim_note") or ""
    if not any(inv.get(k) for k in ADS_PERK_KEYS):
        # Owns nothing at all.
        empty = ads_config.get("empty_design") or []
        if empty:
            tree = _ads_render(empty, {"inventory": _ads_inventory_text(inv), "user": interaction.user.mention})
            await interaction.response.defer(ephemeral=True)
            if not await send_v2_message(interaction.channel, tree, interaction=interaction, ephemeral=True):
                await interaction.followup.send(embed=info_embed(title, _ads_inventory_text(inv)), ephemeral=True)
            return
        await interaction.response.send_message(
            embed=info_embed(title, _ads_inventory_text(inv) + "\n\nBuy a perk from the ad shop to get started."), ephemeral=True)
        return
    design = ads_config.get("claim_design") or []
    if not design:
        body = _ads_inventory_text(inv) + (f"\n\n{note}" if note else "")
        design = [{"type": "text", "text": f"**{title}**\n{body}"}]
    tree = _ads_render(design, {"inventory": _ads_inventory_text(inv), "user": interaction.user.mention})
    tree = _ads_fill_quantities(tree, gid, uid)
    tree = _ads_expand_inventory(tree, gid, uid)
    await interaction.response.defer(ephemeral=True)
    # Render with the viewer in scope so any "inventory" select in the design
    # fills with what THEY own. If the design placed one, don't auto-append ours.
    global _ads_render_viewer, _ads_inventory_placed
    _ads_render_viewer = (gid, uid)
    _ads_inventory_placed = False
    try:
        # Pre-build (viewer in scope) to learn if the design placed an inventory
        # select; keep the viewer set through the real send so it renders too.
        [b for b in (_build_v2(c, interaction.guild) for c in tree) if b]
        rows = [] if _ads_inventory_placed else _ads_claim_rows(gid, uid)
        ok = await send_v2_message(interaction.channel, tree, interaction=interaction, ephemeral=True, extra_rows=rows)
    finally:
        _ads_render_viewer = None
    if not ok:
        await interaction.followup.send(embed=error_embed("Couldn't open", "The claim panel couldn't render."), ephemeral=True)


class AdQueueView(discord.ui.View):
    """Ephemeral, paginated 'Live Advertisement Queue' — 5 per page."""
    PER = 5

    def __init__(self, guild, page=0):
        super().__init__(timeout=180)
        self.guild = guild
        self.page = page
        self._build()

    def _pages(self, n):
        return max(1, (n + self.PER - 1) // self.PER)

    def build_embed(self):
        entries = _ads_queue_entries(self.guild)
        pages = self._pages(len(entries))
        self.page = max(0, min(self.page, pages - 1))
        embed = discord.Embed(title="Live Advertisement Queue", color=ACCENT)
        if not entries:
            embed.description = "The queue is empty."
            return embed, pages
        start = self.page * self.PER
        lines = [_ads_queue_line(a, lane, ts, i + 1)
                 for i, (a, lane, ts) in enumerate(entries[start:start + self.PER], start=start)]
        embed.description = "\n\n".join(lines)
        embed.set_footer(text=f"Page {self.page + 1}/{pages}")
        return embed, pages

    def _build(self):
        self.clear_items()
        entries = _ads_queue_entries(self.guild)
        pages = self._pages(len(entries))
        self.page = max(0, min(self.page, pages - 1))
        prev = discord.ui.Button(label="Previous", style=discord.ButtonStyle.secondary, disabled=self.page <= 0)
        prev.callback = self._prev
        label = discord.ui.Button(label=f"{self.page + 1}/{pages}", style=discord.ButtonStyle.secondary, disabled=True)
        nxt = discord.ui.Button(label="Next", style=discord.ButtonStyle.secondary, disabled=self.page >= pages - 1)
        nxt.callback = self._next
        self.add_item(prev)
        self.add_item(label)
        self.add_item(nxt)

    async def _refresh(self, interaction):
        self._build()
        embed, _ = self.build_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    async def _prev(self, interaction):
        self.page = max(0, self.page - 1)
        await self._refresh(interaction)

    async def _next(self, interaction):
        self.page += 1
        await self._refresh(interaction)


async def _ads_open_queue(interaction):
    view = AdQueueView(interaction.guild, 0)
    embed, _ = view.build_embed()
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


async def _ads_pop_postable(guild, gd):
    """Remove and return the next queued ad whose invite still works (bypass lane
    first). Any dead-invite ad it passes over is left in place and its advertiser
    is DM'd once (a yellow ATTENTION notice). Returns None if none are postable."""
    pos = 0
    for ad, lane, ts in _ads_queue_entries(guild):
        pos += 1
        if int(ad.get("not_before") or 0) > int(time.time()):
            continue  # delayed via the Delay button — the ads behind it post first
        if await _ad_invite_valid(ad.get("server_link")):
            lst = gd.get("bypass" if lane == "bypass" else "queue") or []
            try:
                lst.remove(ad)
            except ValueError:
                pass
            ad.pop("invite_flagged", None)
            return ad
        if not ad.get("invite_flagged"):
            ad["invite_flagged"] = True
            await _ad_invite_warn_dm(guild, ad, pos, ts)
    return None


@tasks.loop(seconds=300)
async def ads_invite_check():
    """Proactively verify queued ads' invites so advertisers get a heads-up
    BEFORE their posting date. DMs once per broken invite; clears the flag (so a
    later break re-notifies) once the invite works again."""
    if not ads_config.get("enabled") or not _ads_loaded:
        return
    changed = False
    for gid in list(ads_data.keys()):
        guild = bot.get_guild(int(gid)) if str(gid).isdigit() else None
        if not guild:
            continue
        pos = 0
        for ad, lane, ts in _ads_queue_entries(guild):
            pos += 1
            if await _ad_invite_valid(ad.get("server_link")):
                if ad.pop("invite_flagged", None) is not None:
                    changed = True
            elif not ad.get("invite_flagged"):
                ad["invite_flagged"] = True
                changed = True
                await _ad_invite_warn_dm(guild, ad, pos, ts)
    if changed:
        await _ads_flush_now()


@ads_invite_check.before_loop
async def _before_ads_invite_check():
    await bot.wait_until_ready()


@tasks.loop(seconds=60)
async def ads_drip():
    if not ads_config.get("enabled"):
        return
    interval = max(1, int(ads_config.get("interval_minutes") or 60)) * 60
    now = int(time.time())
    for gid, gd in list(ads_data.items()):
        if not (gd.get("queue") or gd.get("bypass")):
            continue
        if now - int(gd.get("last_drip", 0)) < interval:
            continue
        guild = bot.get_guild(int(gid)) if str(gid).isdigit() else None
        if not guild:
            continue
        ad = await _ads_pop_postable(guild, gd)
        if ad:
            gd["last_drip"] = now
            try:
                await _ads_post(guild, ad)
            except Exception as e:
                print(f"[Ads] drip post failed: {e}")
        # Persist queue changes + any invite flags set while scanning.
        await _ads_flush_now()


@ads_drip.before_loop
async def _before_ads_drip():
    await bot.wait_until_ready()


@tasks.loop(seconds=25)
async def persist_ads_state():
    """Crash safety net — flush ad inventory/queue/pending on a steady cadence so
    even an ungraceful kill (no SIGTERM) loses at most ~25s of activity."""
    global _ads_dirty
    if not _ads_loaded:
        return  # boot read failed — don't clobber stored inventory with empty
    if _ads_dirty:
        _ads_dirty = False
        try:
            await _bot_config_upsert("ads-data", {"guilds": ads_data})
        except Exception as e:
            _ads_dirty = True
            print(f"[Ads] periodic persist failed: {e}")


@persist_ads_state.before_loop
async def _before_persist_ads_state():
    await bot.wait_until_ready()


@bot.tree.command(name="ads", description="Opens your ad inventory so you can post an ad.")
async def ads_cmd(interaction: discord.Interaction):
    await _ads_open_claim(interaction)


@bot.tree.command(name="adsgrant", description="Gives a member an ad perk.")
@app_commands.describe(user="Member to give the perk to", perk="Which perk", amount="How many. One if you leave it.")
@app_commands.choices(perk=[
    app_commands.Choice(name="Everyone Ping", value="ping_everyone"),
    app_commands.Choice(name="Here Ping", value="ping_here"),
    app_commands.Choice(name="No Ping", value="ping_none"),
    app_commands.Choice(name="Instant Post", value="instant"),
    app_commands.Choice(name="Bypass Queue", value="bypass"),
])
async def adsgrant_cmd(interaction: discord.Interaction, user: discord.Member, perk: app_commands.Choice[str], amount: int = 1):
    if not _is_ads_staff(interaction.user):
        await interaction.response.send_message(embed=error_embed("Staff only", "You need to be ad staff (or Manage Server)."), ephemeral=True)
        return
    amount = max(1, min(100, amount))
    _ads_grant(interaction.guild.id, user.id, perk.value, amount)
    inv = _ads_inventory(interaction.guild.id, user.id)
    await interaction.response.send_message(
        embed=success_embed("Perk granted", f"Gave {user.mention} **{amount}× {_ads_perk_label(perk.value)}**.\n\n**Their inventory:**\n{_ads_inventory_text(inv)}"),
        ephemeral=True)


async def apply_roblox_verification(payload):
    """Bot side of a completed Roblox verify: set nickname + give the role."""
    guild = bot.get_guild(int(payload["guild_id"])) if payload.get("guild_id") else None
    if not guild:
        return
    uid = payload.get("discord_user_id")
    member = guild.get_member(int(uid)) if uid else None
    if member is None and uid:
        try:
            member = await guild.fetch_member(int(uid))
        except Exception:
            member = None
    if not member:
        return
    roblox_username = (payload.get("roblox_username") or "").strip()
    notes = []

    # Nickname
    if roblox_config.get("set_nickname", True) and roblox_username:
        try:
            await member.edit(nick=roblox_username[:32], reason="Roblox verified")
        except discord.Forbidden:
            notes.append("• Couldn't set nickname, I need **Manage Nicknames**, and I can't rename the server owner or anyone with a role above mine.")
            print("[Verify] nickname change forbidden")
        except Exception as e:
            notes.append(f"• Couldn't set nickname, {e}")
            print(f"[Verify] nickname change failed: {e}")

    # Roles: add the configured verify roles, remove the configured ones.
    add_ids = roblox_config.get("verified_role_ids") or []
    remove_ids = roblox_config.get("remove_role_ids") or []
    if not add_ids:
        notes.append("• No 'Roles to add on verify' is set in the dashboard, open the Verification block, pick one or more roles, and Save.")
        print("[Verify] no verified_role_ids configured")

    add_roles = [r for r in (guild.get_role(int(x)) for x in add_ids if str(x).isdigit()) if r]
    if add_roles:
        try:
            await member.add_roles(*add_roles, reason="Roblox verified")
            print(f"[Verify] added {[r.name for r in add_roles]} to {member}")
        except discord.Forbidden:
            notes.append("• Couldn't add one or more verify roles, my role must sit **above** them in Server Settings → Roles, and I need **Manage Roles**.")
            print("[Verify] add roles forbidden (hierarchy/perms)")
        except Exception as e:
            notes.append(f"• Couldn't add verify roles, {e}")
            print(f"[Verify] add roles failed: {e}")

    remove_roles = [r for r in (guild.get_role(int(x)) for x in remove_ids if str(x).isdigit()) if r and r in member.roles]
    if remove_roles:
        try:
            await member.remove_roles(*remove_roles, reason="Roblox verified")
            print(f"[Verify] removed {[r.name for r in remove_roles]} from {member}")
        except discord.Forbidden:
            notes.append("• Couldn't remove one or more roles, my role must sit **above** them in Server Settings → Roles, and I need **Manage Roles**.")
            print("[Verify] remove roles forbidden (hierarchy/perms)")
        except Exception as e:
            notes.append(f"• Couldn't remove roles, {e}")
            print(f"[Verify] remove roles failed: {e}")

    # Ban-evasion guard: if this Roblox account was blacklisted, apply the
    # blacklist role to whatever (possibly brand-new) Discord account linked it.
    try:
        rid = str(payload.get("roblox_id") or "") or await _bl_roblox_id(member)
        if (rid and _bl_is_roblox_blacklisted(guild.id, rid)
                and blacklist_config.get("apply_role")):
            extra = await _bl_apply_punishment(member)
            print(f"[Blacklist] verify-time enforcement on {member} (roblox {rid}){extra}")
            bch = await resolve_channel(blacklist_config.get("channel_id"))
            if bch:
                try:
                    await bch.send(embed=error_embed(
                        "Blacklisted account re-verified",
                        f"{member.mention} verified a **blacklisted Roblox account** "
                        f"([profile]({_bl_roblox_url_from_id(rid)})) — blacklist role applied."))
                except Exception:
                    pass
    except Exception as e:
        print(f"[Blacklist] verify enforcement failed: {e}")

    # Report the outcome to the log channel so the owner can see it in Discord.
    log_id = str(roblox_config.get("log_channel_id") or "").strip()
    if log_id:
        log_ch = guild.get_channel(int(log_id))
        if log_ch:
            try:
                if notes:
                    await log_ch.send(embed=error_embed(
                        "Verified, but something needs fixing",
                        f"{member.mention} linked **{roblox_username}**, however:\n" + "\n".join(notes),
                    ))
                else:
                    await log_ch.send(embed=success_embed(
                        "Roblox verified",
                        f"{member.mention} linked **{roblox_username}**, nickname and role applied.",
                    ))
            except Exception:
                pass


async def start_roblox_verify(interaction):
    """A member clicked Verify — ask the edge function for their Roblox login URL."""
    await interaction.response.defer(ephemeral=True)
    if not roblox_config.get("client_id"):
        await interaction.followup.send(
            embed=error_embed("Verification not set up", "An admin still needs to add the Roblox Client ID/Secret in the dashboard."),
            ephemeral=True,
        )
        return
    try:
        session = await get_poll_session()
        async with session.post(
            f"{SUPABASE_FN_URL}/roblox-verify",
            headers=_fn_headers(),
            json={
                "action": "start",
                "bot_id": BOT_ORDER_ID,
                "guild_id": str(interaction.guild_id),
                "discord_user_id": str(interaction.user.id),
            },
        ) as r:
            data = await r.json() if r.status == 200 else {}
        url = data.get("url") if isinstance(data, dict) else None
        if not url:
            await interaction.followup.send(
                embed=error_embed("Couldn't start verification", "Please try again in a moment."),
                ephemeral=True,
            )
            return
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="Link Roblox", url=url, style=discord.ButtonStyle.link, emoji="🔗"))
        await interaction.followup.send(
            "Click **Link Roblox** to log in. When Roblox says you're verified, come back here, your nickname and role update automatically.",
            view=view,
            ephemeral=True,
        )
    except Exception as e:
        print(f"[Verify] start failed: {e}")
        await interaction.followup.send(embed=error_embed("Something went wrong", "Please try again."), ephemeral=True)


async def fetch_config(feature, attempts=4):
    """Fetch one feature's saved config. The config endpoint occasionally returns
    a transient gateway timeout (5xx) or drops the connection at boot; when that
    happens we must NOT treat it as 'no config saved' — that would silently leave
    the feature disabled for the whole session (e.g. 'Ads are off'). So retry a
    few times with a short backoff before giving up."""
    if not (BOT_ORDER_ID and WORKER_TOKEN):
        return None
    for i in range(attempts):
        last = i == attempts - 1
        try:
            session = await get_poll_session()
            async with session.get(
                f"{SUPABASE_FN_URL}/{BOT_API}/bot-config?feature={feature}&bot_id={BOT_ORDER_ID}",
                headers=_fn_headers(),
            ) as r:
                if r.status == 200:
                    data = await r.json()
                    cfg = data.get("config") if isinstance(data, dict) else None
                    if isinstance(cfg, dict) and "config" in cfg:
                        cfg = cfg["config"]
                    return cfg
                # 5xx = transient (gateway timeout, cold start). Retry before
                # giving up. 4xx is a real 'not there', so don't bother.
                if r.status >= 500 and not last:
                    print(f"[Config] fetch {feature} — HTTP {r.status}, retry {i+1}/{attempts-1}")
                    await asyncio.sleep(1.0 * (i + 1))
                    continue
                print(f"[Config] fetch {feature} — HTTP {r.status}")
                return None
        except Exception as e:
            if not last:
                print(f"[Config] fetch {feature} failed: {e}; retry {i+1}/{attempts-1}")
                await asyncio.sleep(1.0 * (i + 1))
                continue
            print(f"[Config] fetch {feature} failed: {e}")
            return None
    return None


async def mark_config_applied(feature):
    try:
        session = await get_poll_session()
        await session.post(
            f"{SUPABASE_FN_URL}/{BOT_API}/mark-config-applied",
            headers=_fn_headers(),
            json={"bot_id": BOT_ORDER_ID, "feature": feature},
        )
    except Exception as e:
        print(f"[Config] mark applied {feature} failed: {e}")


async def seed_secret_slots():
    """Register this bot's credential slots so the dashboard's 'API keys &
    credentials' card shows them. Worker-token authed; ON CONFLICT DO NOTHING,
    so it's safe to run every boot."""
    if not WORKER_TOKEN:
        return
    slots = [{
        "addon_id": BOT_BASE,
        "key": "ROBLOX_COOKIE",
        "label": "Roblox account cookie",
        "description": ("The .ROBLOSECURITY cookie of your community's Roblox bot account. Powers "
                        "verification lookups and Roblox group rank sync. Encrypted, and only ever "
                        "read by your bot, never shown back. Use a dedicated account."),
        "placeholder": "_|WARNING:-DO-NOT-SHARE...  (paste the full .ROBLOSECURITY value)",
        "required": False,
        "sort_order": 0,
    }, {
        "addon_id": BOT_BASE,
        "key": "ROBLOX_GROUP_ID",
        "label": "Roblox group ID",
        "description": ("The Roblox group your community runs. Used by Roblox Group Sync when no "
                        "group is typed into that block, and by sales tracking. Find it in the "
                        "group's URL: roblox.com/groups/<ID>/... just the number."),
        "placeholder": "e.g. 691798472",
        "required": False,
        "sort_order": 1,
    }]
    res = await runtime_rpc("runtime_seed_secret_slots", {"_token": WORKER_TOKEN, "_slots": slots})
    print(f"[Startup] secret slots seeded for {BOT_BASE}: {bool(res)}")


async def load_all_configs():
    if not (BOT_ORDER_ID and WORKER_TOKEN):
        print(f"[Config] load skipped — BOT_ORDER_ID set: {bool(BOT_ORDER_ID)}, WORKER_TOKEN set: {bool(WORKER_TOKEN)}")
        return
    print(f"[Config] loading for bot {BOT_ORDER_ID} (base {BOT_BASE})")
    features = [f for f in ("welcome", "invite", "tickets", "roblox-verify", "customs-giveaway", "customs-infraction", "customs-promotion", "customs-logging", "music-addon", "auto-radio", "roblox-group-sync", "customs-messages", "customs-suggestions", "customs-blacklist", "customs-smallui", "invite-tracker", "marketplace", "ads", "customs-tts", "customs-gambling", "roleplay-shifts", "roleplay-sessions") if _base_allows_feature(f)]
    # Fetch every config at once (a few at a time) instead of one after another:
    # this used to be ~15s of serial round trips on every boot. Apply in the
    # original order so panels and sources register exactly as before.
    sem = asyncio.Semaphore(8)

    async def _fetch_one(f):
        async with sem:
            return f, await fetch_config(f)

    t0 = time.time()
    results = await asyncio.gather(*(_fetch_one(f) for f in features))
    print(f"[Config] fetched {len(results)} config(s) in {time.time() - t0:.1f}s")
    for feature, cfg in results:
        if cfg:
            await apply_config(feature, cfg)
        else:
            print(f"[Config] {feature} — none saved")
    # /suggestion and /reportbug: if no per-bot config was saved, use the owner's
    # global Custom Feature / Report a Bug channel so the command still works.
    for feature in ("customs-suggestions", "customs-reportbug"):
        if not _pf_config_for(feature).get("channel_id"):
            try:
                if await _pf_platform_fallback(feature):
                    print(f"[Config] {feature} — using global Extras channel (fallback)")
            except Exception as e:
                print(f"[Config] {feature} extras fallback failed: {e}")


async def complete_command(command_id, status="done", error=None):
    body = {"command_id": command_id, "status": status}
    if error:
        body["error_message"] = error
    try:
        session = await get_poll_session()
        await session.post(f"{SUPABASE_FN_URL}/{BOT_API}/complete-command", headers=_fn_headers(), json=body)
    except Exception as e:
        print(f"[Command] complete failed: {e}")


_processing_roblox = set()


@tasks.loop(seconds=8)
async def poll_roblox_apply():
    """Claim pending roblox_apply commands straight from the DB via REST and
    process them (nickname + role). This bypasses the shared claim-command
    allowlist, so verification works regardless of the bot-api function's
    action whitelist."""
    if not (SUPABASE_URL and SUPABASE_KEY and BOT_ORDER_ID):
        return
    try:
        url = (
            f"{SUPABASE_URL}/rest/v1/bot_commands?bot_id=eq.{BOT_ORDER_ID}"
            f"&action=eq.roblox_apply&status=eq.pending&order=created_at.asc&select=id,payload&limit=10"
        )
        async with _http() as client:
            r = await client.get(
                url,
                headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
                timeout=20,
            )
        if r.status_code != 200:
            return
        rows = r.json()
    except httpx.TransportError:
        return  # transient network blip (timeout/connection) — retried next cycle
    except Exception as e:
        print(f"[Verify] roblox_apply poll failed: {e}")
        return
    if not isinstance(rows, list):
        return
    for row in rows:
        cid = row.get("id")
        if not cid or cid in _processing_roblox:
            continue
        _processing_roblox.add(cid)
        try:
            print(f"[Verify] processing roblox_apply {cid}")
            await apply_roblox_verification(row.get("payload") or {})
        except Exception as e:
            print(f"[Verify] roblox_apply {cid} failed: {e}")
        finally:
            # Mark done either way so we don't loop on a bad row forever.
            await complete_command(cid)
            _processing_roblox.discard(cid)


@poll_roblox_apply.before_loop
async def before_poll_roblox_apply():
    await bot.wait_until_ready()


async def save_ticket_panel(guild_id, channel_id, message_id, channel_name):
    try:
        async with _http() as client:
            await client.post(
                f"{SUPABASE_FN_URL}/save-ticket-panel",
                headers=_fn_headers(),
                json={"bot_id": BOT_ORDER_ID, "guild_id": str(guild_id), "channel_id": str(channel_id), "message_id": str(message_id), "channel_name": channel_name},
                timeout=10,
            )
    except Exception as e:
        print(f"[Ticket] save panel failed: {e}")


@tasks.loop(seconds=5)
async def poll_configs():
    if not (BOT_ORDER_ID and WORKER_TOKEN):
        return
    try:
        session = await get_poll_session()
        async with session.post(
            f"{SUPABASE_FN_URL}/{BOT_API}/claim-command",
            headers=_fn_headers(),
            json={"bot_id": BOT_ORDER_ID},
        ) as r:
            if r.status != 200:
                if r.status not in (401, 403):
                    return
                global _auth_warned
                if not _auth_warned:
                    _auth_warned = True
                    body = (await r.text())[:200]
                    print(f"[Poll] claim-command auth failed — HTTP {r.status} body={body}")
                return
            data = await r.json()
        cmd = data.get("command") if isinstance(data, dict) else None
        if not cmd:
            return
        action = cmd.get("action")
        payload = cmd.get("payload") or {}
        command_id = cmd.get("id")
        print(f"[Poll] {action} ({command_id})")

        if action in ("post_message", "send_channel_message"):
            if payload.get("verify_panel"):
                # Owner pressed "Post panel" for Roblox verification.
                if payload.get("channel_id"):
                    roblox_config["channel_id"] = str(payload["channel_id"])
                await post_verify_panel()
            else:
                channel = await resolve_channel(payload.get("channel_id"))
                if channel:
                    await handle_post(channel, payload)
            await complete_command(command_id)

        elif action == "edit_ticket_panel":
            channel = await resolve_channel(payload.get("channel_id"))
            if channel and payload.get("components_v2"):
                ok = await send_v2_message(channel, payload["components_v2"], payload.get("content") or None)
            await complete_command(command_id)

        elif action == "apply_config":
            feature = payload.get("feature")
            if feature:
                cfg = await fetch_config(feature)
                if cfg:
                    # A save/apply command is a deliberate action, so post the
                    # verify panel here (boot loads config without posting).
                    await apply_config(feature, cfg, post_panel=True)
                await mark_config_applied(feature)
            await complete_command(command_id)

        elif action == "roblox_apply":
            await apply_roblox_verification(payload)
            await complete_command(command_id)

        elif action == "set_status":
            await refresh_status()
            await complete_command(command_id)

        elif action == "list_roles":
            await cache_roles(payload.get("guild_id"))
            await complete_command(command_id)

        elif action == "list_channels":
            await cache_channels(payload.get("guild_id"))
            await complete_command(command_id)

        else:
            await complete_command(command_id)

    except Exception as e:
        print(f"[Poll] error: {e}")


@poll_configs.before_loop
async def before_poll_configs():
    await bot.wait_until_ready()


async def cache_roles(guild_id):
    if not guild_id:
        return
    guild = bot.get_guild(int(guild_id))
    if not guild:
        return
    now = discord.utils.utcnow().isoformat()
    roles = [{
        "bot_id": BOT_ORDER_ID, "guild_id": str(guild_id), "role_id": str(r.id), "role_name": r.name,
        "color": r.color.value, "position": r.position, "managed": r.managed, "is_everyone": r.id == guild.id, "fetched_at": now,
    } for r in guild.roles]
    try:
        session = await get_poll_session()
        await session.post(f"{SUPABASE_FN_URL}/{BOT_API}/upsert-role-cache", headers=_fn_headers(), json={"bot_id": BOT_ORDER_ID, "guild_id": str(guild_id), "roles": roles})
    except Exception as e:
        print(f"[Cache] roles failed: {e}")


async def cache_channels(guild_id):
    if not guild_id:
        return
    guild = bot.get_guild(int(guild_id))
    if not guild:
        return
    now = discord.utils.utcnow().isoformat()
    channels = []
    for ch in guild.channels:
        if isinstance(ch, discord.TextChannel):
            ctype = "text"
        elif isinstance(ch, discord.ForumChannel):
            ctype = "forum"
        elif isinstance(ch, discord.VoiceChannel):
            ctype = "voice"
        elif isinstance(ch, discord.CategoryChannel):
            ctype = "category"
        else:
            ctype = "other"
        channels.append({
            "bot_id": BOT_ORDER_ID, "guild_id": str(guild_id), "channel_id": str(ch.id), "channel_name": ch.name,
            "channel_type": ctype, "parent_id": str(ch.category_id) if ch.category_id else None, "position": ch.position, "fetched_at": now,
        })
    try:
        session = await get_poll_session()
        await session.post(f"{SUPABASE_FN_URL}/{BOT_API}/upsert-channel-cache", headers=_fn_headers(), json={"bot_id": BOT_ORDER_ID, "guild_id": str(guild_id), "channels": channels})
    except Exception as e:
        print(f"[Cache] channels failed: {e}")


async def fire_online_status():
    if not (SUPABASE_URL and BOT_ORDER_ID):
        return
    try:
        guilds = [{"id": str(g.id), "name": g.name, "member_count": g.member_count or 0} for g in bot.guilds]
        payload = {"bot_id": BOT_ORDER_ID, "last_heartbeat_at": discord.utils.utcnow().isoformat(), "status": "online"}
        if guilds:
            payload["guilds"] = guilds
        async with _http() as client:
            await client.patch(
                f"{SUPABASE_URL}/rest/v1/bot_runtime_status?bot_id=eq.{BOT_ORDER_ID}",
                headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json", "Prefer": "return=minimal"},
                json=payload, timeout=5,
            )
        print("[Boot] online status fired")
    except Exception as e:
        print(f"[Boot] online status failed: {e}")


_hb_warned = False


@tasks.loop(seconds=30)
async def send_heartbeat():
    if not (BOT_ORDER_ID and WORKER_TOKEN):
        return
    try:
        guilds = [{"id": str(g.id), "name": g.name, "member_count": g.member_count or 0} for g in bot.guilds]
        session = await get_poll_session()
        async with session.post(
            f"{SUPABASE_FN_URL}/{BOT_API}/heartbeat",
            headers=_fn_headers(),
            json={"bot_id": BOT_ORDER_ID, "status": "online", "guilds": guilds},
        ) as r:
            # A rejected heartbeat is what makes the dashboard think the bot is
            # down, so say so in the log instead of failing silently.
            if r.status != 200:
                global _hb_warned
                if not _hb_warned:
                    _hb_warned = True
                    print(f"[Heartbeat] rejected: HTTP {r.status} {(await r.text())[:160]}")
            else:
                _hb_warned = False
    except Exception as e:
        print(f"[Heartbeat] error: {e}")


@send_heartbeat.before_loop
async def before_heartbeat():
    await bot.wait_until_ready()


@tasks.loop(minutes=5)
async def record_metrics_loop():
    if not (BOT_ORDER_ID and WORKER_TOKEN):
        return
    try:
        session = await get_poll_session()
        await session.post(
            f"{SUPABASE_FN_URL}/{BOT_API}/record-metrics",
            headers=_fn_headers(),
            json={
                "bot_id": BOT_ORDER_ID, "commands": 0, "messages": 0, "errors": 0,
                "active_servers": len(bot.guilds), "member_count": sum(g.member_count or 0 for g in bot.guilds),
            },
        )
    except Exception as e:
        print(f"[Metrics] error: {e}")


@record_metrics_loop.before_loop
async def before_metrics():
    await bot.wait_until_ready()


# ==================== Music / DJ (Lavalink + AI DJ "Carla") ====================
# Faithful port of the Oversite Utilities music system: rich Now Playing card
# (album art + live progress bar + button controls; classic V1 and Components V2),
# YouTube Music search via the shared Lavalink node, Spotify link/genre resolution,
# adaptive genre radio, favorites, and the AI DJ ("Carla").
#
# This repo is PUBLIC, so every secret comes from env (never hardcoded):
#   SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET   album art + genre/link resolution
#   YOUTUBE_OAUTH_REFRESH_TOKEN                 registers the node's YT OAuth (optional
#                                               if the node already has it)
#   ANTHROPIC_API_KEY                           AI DJ lines/picks (optional; templates otherwise)
#   ELEVENLABS_API_KEY / ELEVENLABS_VOICE_ID    DJ voice (optional; Edge-TTS otherwise)
#   DJ_PUBLIC_URL                               this service's public URL, for the DJ clip server

SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "")
YOUTUBE_OAUTH_REFRESH_TOKEN = os.getenv("YOUTUBE_OAUTH_REFRESH_TOKEN", "")
DASHBOARD_URL = os.getenv("DASHBOARD_URL", "https://oversite.shop/bot-dashboard")
ACCENT_COLOR = ACCENT  # customs' accent, reused by the ported UI
MUSIC_ALERT_CHANNEL = 0  # Utilities-only alert channel; disabled here

music_available = True  # native engine (yt-dlp + FFmpeg) — always available

# Auto radio config (dashboard "Auto Radio" block).
auto_radio_config = {
    "voice_channel_id": None, "text_channel_id": None,
    "genre": "pop", "auto_start": False, "allow_vote": True,
}


async def supabase_rpc(op: str, payload: dict | None = None):
    """Best-effort music persistence via the runtime_music_op RPC. If the RPC
    isn't present on this project it simply no-ops (favorites/taste stay
    in-memory for the session)."""
    try:
        async with _http() as client:
            r = await client.post(
                f"{SUPABASE_URL}/rest/v1/rpc/runtime_music_op",
                headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
                         "Content-Type": "application/json"},
                json={"p_token": WORKER_TOKEN, "p_op": op, "p_payload": payload or {}},
                timeout=10)
            if r.status_code == 200:
                j = r.json()
                if isinstance(j, dict):
                    for k in ("rows", "data", "result"):
                        if isinstance(j.get(k), list):
                            return j[k]
                return [] if j is None else j
    except Exception as e:
        print(f"[RPC] {op} error: {e}")
    return None


def get_player(guild):
    return guild.voice_client


async def _ensure_wl_player(guild, channel):
    """Music needs a NativePlayer. If the guild's current voice client is the
    plain TTS VoiceClient (or an old wavelink player), replace it."""
    vc = guild.voice_client
    if vc is not None and not isinstance(vc, NativePlayer):
        try:
            await vc.disconnect(force=True)
        except Exception:
            pass
        vc = None
    if not vc:
        vc = await channel.connect(cls=NativePlayer)
    return vc


def is_dj(member: discord.Member) -> bool:
    """DJ = Administrator, or a configured DJ role. No DJ roles set = everyone."""
    try:
        if member.guild_permissions.administrator or member.guild_permissions.manage_guild:
            return True
    except Exception:
        pass
    dj_role_ids = music_config.get("dj_role_ids", [])
    if not dj_role_ids:
        return True
    return has_any_role(member, dj_role_ids)


def _is_admin(member) -> bool:
    try:
        return member.guild_permissions.administrator or member.guild_permissions.manage_guild
    except Exception:
        return False


# ---- Genre search pools ----
GENRE_QUERIES = [
    "greatest {genre} songs of all time",
    "best {genre} hits ever",
    "top {genre} songs all time",
    "classic {genre} songs",
]

GENRE_SONGS = {
    "pop": [
        "Taylor Swift Anti-Hero", "Ed Sheeran Shape Of You", "Ariana Grande 7 Rings",
        "The Weeknd Blinding Lights", "Dua Lipa Levitating", "Harry Styles As It Was",
        "Billie Eilish Bad Guy", "Olivia Rodrigo Good 4 U", "Justin Bieber Peaches",
        "Bruno Mars Uptown Funk", "Lady Gaga Bad Romance", "Katy Perry Roar",
        "Doja Cat Say So", "Post Malone Circles", "Miley Cyrus Flowers",
    ],
    "country": [
        "Morgan Wallen Last Night", "Luke Combs Fast Car", "Chris Stapleton Tennessee Whiskey",
        "Zach Bryan Something In The Orange", "Kacey Musgraves Golden Hour", "Jelly Roll Need A Favor",
        "Lainey Wilson Heart Like A Truck", "Carrie Underwood Before He Cheats",
        "Garth Brooks Friends In Low Places", "George Strait Check Yes or No",
        "Kane Brown Heaven", "Blake Shelton God's Country", "Dolly Parton Jolene",
    ],
    "rnbhiphop": [
        "Drake God's Plan", "Kendrick Lamar HUMBLE", "SZA Kill Bill", "Frank Ocean Nights",
        "The Weeknd Save Your Tears", "Usher Yeah", "Daniel Caesar Get You",
        "Beyonce Crazy In Love", "Rihanna We Found Love", "Eminem Lose Yourself",
        "Kanye West Stronger", "Tyler The Creator EARFQUAKE", "Childish Gambino Redbone",
    ],
    "rockalternative": [
        "Queen Bohemian Rhapsody", "Led Zeppelin Stairway To Heaven", "Nirvana Smells Like Teen Spirit",
        "Foo Fighters Everlong", "Red Hot Chili Peppers Californication", "Green Day Basket Case",
        "Arctic Monkeys Do I Wanna Know", "The Strokes Reptilia", "Imagine Dragons Radioactive",
        "Coldplay Yellow", "Radiohead Creep", "Pearl Jam Black", "Hozier Take Me To Church",
    ],
    "latin": [
        "Bad Bunny Titi Me Pregunto", "J Balvin Mi Gente", "Daddy Yankee Gasolina",
        "Karol G Provenza", "Rauw Alejandro Todo De Ti", "Maluma Hawai", "Shakira Hips Don't Lie",
        "Enrique Iglesias Bailando", "Ozuna Taki Taki", "Feid Classy 101",
    ],
    "dance": [
        "Daft Punk Get Lucky", "Calvin Harris Summer", "Avicii Wake Me Up", "David Guetta Titanium",
        "Marshmello Alone", "The Chainsmokers Closer", "Martin Garrix Animals", "Alan Walker Faded",
        "Kygo Firestone", "Swedish House Mafia Don't You Worry Child", "Zedd Clarity",
    ],
    "christian": [
        "Hillsong Oceans", "Chris Tomlin How Great Is Our God", "Elevation Worship Graves Into Gardens",
        "Lauren Daigle You Say", "Phil Wickham Living Hope", "Maverick City Music Jireh",
        "For King And Country God Only Knows", "Cory Asbury Reckless Love",
    ],
    "gospel": [
        "Kirk Franklin Love Theory", "Tasha Cobbs Leonard Break Every Chain", "CeCe Winans Believe For It",
        "Tamela Mann Take Me To The King", "Travis Greene Made A Way", "Marvin Sapp Never Would Have Made It",
    ],
    "jazz": [
        "Miles Davis So What", "John Coltrane My Favorite Things", "Louis Armstrong What A Wonderful World",
        "Dave Brubeck Take Five", "Norah Jones Don't Know Why", "Frank Sinatra Fly Me To The Moon",
        "Nina Simone Feeling Good", "Ella Fitzgerald Dream A Little Dream",
    ],
    "classical": [
        "Beethoven Moonlight Sonata", "Mozart Eine Kleine Nachtmusik", "Bach Air On G String",
        "Chopin Nocturne Op 9", "Debussy Clair De Lune", "Vivaldi Four Seasons Spring",
        "Tchaikovsky Swan Lake", "Pachelbel Canon In D",
    ],
    "lofi": [
        "Lofi hip hop beats to relax", "Chillhop essentials", "Jinsang affection",
        "Idealism controlla", "Nujabes Feather", "tomppabeats harbor",
    ],
}

# Spotify "artist:X" queries per genre (fetched live, ranked by popularity).
SPOTIFY_GENRE_QUERIES = {
    "country": ["artist:Morgan Wallen", "artist:Luke Combs", "artist:Zach Bryan", "artist:Jelly Roll",
                "artist:Lainey Wilson", "artist:Chris Stapleton", "artist:Kacey Musgraves", "artist:Cody Johnson",
                "artist:Shaboozey", "artist:Megan Moroney", "artist:Jordan Davis", "artist:Bailey Zimmerman",
                "artist:HARDY", "artist:Kane Brown", "artist:Thomas Rhett", "artist:Post Malone"],
    "pop": ["artist:Taylor Swift", "artist:Olivia Rodrigo", "artist:Sabrina Carpenter", "artist:Chappell Roan",
            "artist:Billie Eilish", "artist:Dua Lipa", "artist:The Weeknd", "artist:Ariana Grande",
            "artist:Ed Sheeran", "artist:Benson Boone", "artist:Teddy Swims", "artist:Gracie Abrams",
            "artist:Post Malone", "artist:Bruno Mars", "artist:Charlie Puth", "artist:Doja Cat"],
    "rnbhiphop": ["artist:Drake", "artist:Kendrick Lamar", "artist:SZA", "artist:Travis Scott",
                  "artist:21 Savage", "artist:Future", "artist:Lil Baby", "artist:Doja Cat",
                  "artist:Chris Brown", "artist:Brent Faiyaz", "artist:Summer Walker", "artist:Metro Boomin",
                  "artist:J. Cole", "artist:Tyler The Creator", "artist:Giveon", "artist:PARTYNEXTDOOR"],
    "rockalternative": ["artist:Imagine Dragons", "artist:Arctic Monkeys", "artist:The 1975", "artist:Hozier",
                        "artist:Noah Kahan", "artist:Tame Impala", "artist:Coldplay", "artist:Foo Fighters",
                        "artist:Red Hot Chili Peppers", "artist:The Killers", "artist:Linkin Park", "artist:Nirvana",
                        "artist:Green Day", "artist:Twenty One Pilots", "artist:Paramore", "artist:Metallica"],
    "latin": ["artist:Bad Bunny", "artist:Karol G", "artist:Peso Pluma", "artist:Feid",
              "artist:Rauw Alejandro", "artist:J Balvin", "artist:Ozuna", "artist:Maluma",
              "artist:Shakira", "artist:Grupo Frontera", "artist:Fuerza Regida", "artist:Anitta"],
    "dance": ["artist:Calvin Harris", "artist:David Guetta", "artist:Martin Garrix", "artist:Marshmello",
              "artist:Kygo", "artist:The Chainsmokers", "artist:Tiesto", "artist:Fisher",
              "artist:Skrillex", "artist:Odesza", "artist:Illenium", "artist:Fred again"],
    "christian": ["artist:Lauren Daigle", "artist:Elevation Worship", "artist:Chris Tomlin", "artist:Phil Wickham",
                  "artist:Maverick City Music", "artist:Hillsong Worship", "artist:Bethel Music", "artist:Cody Carnes",
                  "artist:Brandon Lake", "artist:For King And Country", "artist:CAIN", "artist:Zach Williams"],
    "gospel": ["artist:Kirk Franklin", "artist:Tasha Cobbs Leonard", "artist:CeCe Winans", "artist:Travis Greene",
               "artist:Maverick City Music", "artist:Tamela Mann", "artist:Marvin Sapp", "artist:Jekalyn Carr"],
    "jazz": ["artist:Norah Jones", "artist:Miles Davis", "artist:John Coltrane", "artist:Frank Sinatra",
             "artist:Louis Armstrong", "artist:Nina Simone", "artist:Ella Fitzgerald", "artist:Michael Buble",
             "artist:Gregory Porter", "artist:Diana Krall", "artist:Chet Baker", "artist:Bill Evans"],
    "classical": ["artist:Ludovico Einaudi", "artist:Hans Zimmer", "artist:Lang Lang", "artist:Yo-Yo Ma",
                  "artist:Beethoven", "artist:Mozart", "artist:Chopin", "artist:Bach",
                  "artist:Debussy", "artist:Vivaldi", "artist:Max Richter", "artist:Tchaikovsky"],
    "lofi": ["artist:Lofi Girl", "artist:Chillhop Music", "artist:Jinsang", "artist:Idealism",
             "artist:tomppabeats", "artist:Kupla", "artist:Sleepy Fish", "artist:Nujabes"],
}

_used_songs: dict = {}
_used_genre_queries: dict = {}


def get_genre_query(genre: str) -> str:
    import random as _r
    genre_lower = genre.lower().replace(" ", "").replace("-", "")
    pool = GENRE_SONGS.get(genre_lower)
    if not pool:
        return _r.choice(GENRE_QUERIES).format(genre=genre)
    used = _used_genre_queries.get(genre_lower, [])
    available = [q for q in pool if q not in used]
    if not available:
        _used_genre_queries[genre_lower] = []
        available = pool[:]
    query = _r.choice(available)
    _used_genre_queries.setdefault(genre_lower, []).append(query)
    return query


KARAOKE_KEYWORDS = [
    "karaoke", "instrumental", "cover", "tribute", "made famous", "in the style of",
    "backing track", "minus one", "no vocal", "sing along", "originally performed",
    "as made", "workout remix", "nightcore", "sped up", "slowed", "reverb", "lofi", "lo-fi",
]


def best_track(tracks, query: str):
    """Pick the best result, filtering karaoke/cover/instrumental versions."""
    if not tracks:
        return None
    query_lower = query.lower()
    query_words = set(query_lower.split())
    HARD_FILTER = ["karaoke", "instrumental", "tribute", "made famous", "in the style of",
                   "backing track", "minus one", "no vocal", "sing along", "originally performed",
                   "as made", "nightcore", "cover version", "cover by"]
    # A filter keyword the user TYPED is what they want — don't filter it away.
    clean = [t for t in tracks
             if not any(kw in t.title.lower() and kw not in query_lower for kw in HARD_FILTER)]
    if not clean:
        clean = tracks

    def score(track):
        title_lower = track.title.lower()
        author_lower = (track.author or "").lower()
        s = 0
        overlap = len(query_words & (set(title_lower.split()) | set(author_lower.split())))
        s += overlap * 15
        for w in query_words:
            if len(w) > 3 and w in author_lower:
                s += 20
        for kw in ["cover", "sped up", "slowed", "reverb", "lofi", "lo-fi", "workout", "remix",
                   "version", "acoustic", "bass boost", "8d", "live", "mashup",
                   "extended", "loop", "pitched", "chipmunk"]:
            if kw in title_lower and kw not in query_lower:
                s -= 15
        # Music videos carry intros/skits/sound effects — prefer pure audio.
        for kw in ["official video", "official music video", "music video",
                   "official hd video", "official 4k", "(video", "[video", "m/v"]:
            if kw in title_lower:
                s -= 25
                break
        if "official audio" in title_lower or "lyric" in title_lower or author_lower.endswith(" - topic"):
            s += 15
        if len(track.title) > 80:
            s -= 10
        return s

    scored = sorted(((score(t), t) for t in clean[:10]), key=lambda x: x[0], reverse=True)
    return scored[0][1]


# ---- Spotify integration ----
_spotify_token = None
_spotify_token_expiry = 0.0


async def get_spotify_token() -> str | None:
    global _spotify_token, _spotify_token_expiry
    import time as _t
    if _spotify_token and _t.time() < _spotify_token_expiry - 60:
        return _spotify_token
    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        return None
    try:
        import base64
        creds = base64.b64encode(f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}".encode()).decode()
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://accounts.spotify.com/api/token",
                headers={"Authorization": f"Basic {creds}", "Content-Type": "application/x-www-form-urlencoded"},
                data="grant_type=client_credentials") as r:
                data = await r.json()
                _spotify_token = data.get("access_token")
                _spotify_token_expiry = _t.time() + data.get("expires_in", 3600)
                return _spotify_token
    except Exception as e:
        print(f"[Spotify] Token error: {e}")
        return None


async def spotify_get_tracks(url: str) -> list:
    token = await get_spotify_token()
    if not token:
        return []
    headers = {"Authorization": f"Bearer {token}"}
    results = []
    try:
        async with aiohttp.ClientSession() as session:
            if "track/" in url:
                tid = url.split("track/")[1].split("?")[0].split("/")[0]
                async with session.get(f"https://api.spotify.com/v1/tracks/{tid}", headers=headers) as r:
                    data = await r.json()
                    results.append({"name": data["name"], "artist": data["artists"][0]["name"]})
            elif "playlist/" in url:
                pid = url.split("playlist/")[1].split("?")[0].split("/")[0]
                offset = 0
                while len(results) < 50:
                    async with session.get(
                        f"https://api.spotify.com/v1/playlists/{pid}/tracks?limit=50&offset={offset}",
                        headers=headers) as r:
                        data = await r.json()
                        items = data.get("items", [])
                        if not items:
                            break
                        for item in items:
                            t = item.get("track")
                            if t and t.get("name"):
                                results.append({"name": t["name"], "artist": t["artists"][0]["name"]})
                        if not data.get("next"):
                            break
                        offset += 50
            elif "album/" in url:
                aid = url.split("album/")[1].split("?")[0].split("/")[0]
                async with session.get(f"https://api.spotify.com/v1/albums/{aid}/tracks?limit=50", headers=headers) as r:
                    data = await r.json()
                    for t in data.get("items", []):
                        results.append({"name": t["name"], "artist": t["artists"][0]["name"]})
    except Exception as e:
        print(f"[Spotify] Fetch error: {e}")
    return results


_artwork_cache: dict = {}


async def get_spotify_artwork(title: str, artist: str = "") -> str | None:
    cache_key = f"{title}|{artist}".lower()
    if cache_key in _artwork_cache:
        return _artwork_cache[cache_key]
    token = await get_spotify_token()
    if not token:
        return None
    try:
        query = f"{title} {artist}".strip()
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://api.spotify.com/v1/search?q={query}&type=track&limit=1",
                headers={"Authorization": f"Bearer {token}"}) as r:
                if r.status == 429:
                    return None
                data = await r.json()
                items = data.get("tracks", {}).get("items", [])
                if items:
                    images = items[0].get("album", {}).get("images", [])
                    if images:
                        url = images[0]["url"]
                        _artwork_cache[cache_key] = url
                        return url
    except Exception as e:
        if "429" not in str(e):
            print(f"[Spotify] Artwork lookup error: {e}")
    return None


_spotify_genre_cache: dict = {}
_genre_fetch_locks: dict = {}
_spotify_genre_cache_ttl = 6 * 3600


async def fetch_spotify_genre_songs(genre_lower: str) -> list:
    import time, urllib.parse
    queries = SPOTIFY_GENRE_QUERIES.get(genre_lower)
    if not queries:
        return []
    cached = _spotify_genre_cache.get(genre_lower)
    if cached and time.time() - cached[0] < _spotify_genre_cache_ttl:
        return cached[1]
    try:
        token = await get_spotify_token()
        if not token:
            return []
        song_popularity: dict = {}
        headers = {"Authorization": f"Bearer {token}"}
        import random as _rq
        selected = _rq.sample(queries, min(15, len(queries)))
        async with aiohttp.ClientSession() as session:
            for query in selected:
                url = f"https://api.spotify.com/v1/search?q={urllib.parse.quote(query)}&type=track&limit=10&market=US"
                try:
                    async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                        if resp.status != 200:
                            continue
                        data = await resp.json()
                except Exception:
                    continue
                for track in data.get("tracks", {}).get("items", []):
                    name = track.get("name", "").strip()
                    artists = track.get("artists", [])
                    artist = artists[0].get("name", "").strip() if artists else ""
                    pop = track.get("popularity", 0)
                    if name and artist:
                        entry = f"{artist} {name}"
                        if pop > song_popularity.get(entry, -1):
                            song_popularity[entry] = pop
        songs = [e for e, _p in sorted(song_popularity.items(), key=lambda kv: kv[1], reverse=True)][:60]
        if len(songs) < 5:
            return []
        _spotify_genre_cache[genre_lower] = (time.time(), songs)
        return songs
    except Exception as e:
        print(f"[Spotify Genre] Failed for {genre_lower}: {e}")
        return []


# ---- Adaptive taste (in-memory) ----
music_taste = {}
_artist_song_cache = {}


def _adjust_taste(guild_id, artist, delta):
    if not guild_id or not artist:
        return
    t = music_taste.setdefault(guild_id, {})
    a = str(artist).strip().lower()
    if not a:
        return
    t[a] = max(-8.0, min(12.0, t.get(a, 0.0) + delta))


def _taste_event_current(guild, delta):
    try:
        vc = guild.voice_client
        if vc and getattr(vc, "current", None):
            _adjust_taste(guild.id, getattr(vc.current, "author", None), delta)
    except Exception:
        pass


def _decay_taste(guild_id):
    t = music_taste.get(guild_id)
    if t:
        for a in list(t):
            t[a] *= 0.985
            if abs(t[a]) < 0.05:
                del t[a]


async def fetch_artist_songs(artist: str) -> list:
    import time as _t
    key = artist.lower()
    cached = _artist_song_cache.get(key)
    if cached and (_t.time() - cached[0]) < 21600:
        return cached[1]
    out = []
    try:
        token = await get_spotify_token()
        if token:
            async with _http() as client:
                r = await client.get(
                    "https://api.spotify.com/v1/search",
                    params={"q": f'artist:"{artist}"', "type": "track", "limit": 10, "market": "US"},
                    headers={"Authorization": f"Bearer {token}"}, timeout=10)
                for t in r.json().get("tracks", {}).get("items", []):
                    nm = t.get("name", "").strip()
                    ar = (t.get("artists") or [{}])[0].get("name", "").strip()
                    if nm and ar and ar.lower() == key:
                        e = f"{ar} {nm}"
                        if e not in out:
                            out.append(e)
    except Exception as e:
        print(f"[Adaptive] Artist fetch failed for {artist}: {e}")
    _artist_song_cache[key] = (_t.time(), out)
    return out


async def get_genre_playlist_tracks(genre: str, count: int = 50, guild_id=None, source: str = "?") -> list:
    import random as _r
    import wavelink as _wl
    genre_lower = genre.lower().replace(" ", "").replace("-", "").replace("&", "").replace("/", "")
    genre_aliases = {
        "rock": "rockalternative", "alternative": "rockalternative", "rockandalt": "rockalternative",
        "rnb": "rnbhiphop", "hiphop": "rnbhiphop", "hip hop": "rnbhiphop", "rb": "rnbhiphop",
        "randb": "rnbhiphop", "rbhiphop": "rnbhiphop",
    }
    genre_lower = genre_aliases.get(genre_lower, genre_lower)
    if genre_lower == "all":
        import itertools as _it
        songs = list(_it.chain.from_iterable(GENRE_SONGS.values()))
    else:
        _gl = _genre_fetch_locks.setdefault(genre_lower, asyncio.Lock())
        async with _gl:
            spotify_songs = await fetch_spotify_genre_songs(genre_lower)
        songs = spotify_songs or GENRE_SONGS.get(genre_lower, [])
    if not songs:
        return []
    taste = music_taste.get(guild_id, {}) if guild_id else {}
    if taste:
        for fa in [a for a, s in sorted(taste.items(), key=lambda kv: -kv[1]) if s >= 4][:3]:
            try:
                for e in await fetch_artist_songs(fa):
                    if e not in songs:
                        songs.append(e)
            except Exception:
                pass
    used = _used_songs.get(genre_lower, [])
    available = [s for s in songs if s not in used]
    if not available:
        _used_songs[genre_lower] = []
        available = songs[:]
    if taste:
        def _w(entry):
            el = entry.lower()
            for artist, s in taste.items():
                if el.startswith(artist):
                    return max(0.15, 1.0 + s * 0.35)
            return 1.0
        pool = available[:]
        picks = []
        weighted_target = max(1, int(count * 0.7))
        while pool and len(picks) < weighted_target:
            choice = _r.choices(pool, weights=[_w(s) for s in pool], k=1)[0]
            pool.remove(choice)
            picks.append(choice)
        _r.shuffle(pool)
        picks.extend(pool[:count - len(picks)])
        _r.shuffle(picks)
    else:
        _r.shuffle(available)
        picks = available[:count]
    _used_songs.setdefault(genre_lower, []).extend(picks)
    tracks = []
    for song in picks:
        try:
            results, _sp = await search_any(song)
            if results and not isinstance(results, _wl.Playlist):
                tracks.append(best_track(results, song) or results[0])
        except Exception as e:
            print(f"[AutoRadio] Search error for {song}: {e}")
    print(f"[AutoRadio] Loaded {len(tracks)} tracks for {genre} (via {source}, target {count})")
    return tracks


# ---- Favorites (in-memory + best-effort persistence) ----
liked_songs = {}
track_history = {}
play_channels = {}
repeat_enabled = {}


async def save_liked_song_to_supabase(user_id, track_title):
    await supabase_rpc("like_add", {"user_id": str(user_id), "track_title": track_title})


async def delete_liked_song_from_supabase(user_id, track_title):
    await supabase_rpc("like_remove", {"user_id": str(user_id), "track_title": track_title})


async def clear_liked_songs_in_supabase(user_id):
    await supabase_rpc("likes_clear", {"user_id": str(user_id)})


async def set_vc_status(voice_channel, status):
    try:
        route = discord.http.Route("PUT", "/channels/{channel_id}/voice-status", channel_id=voice_channel.id)
        await bot.http.request(route, json={"status": (status or "")[:500]})
    except Exception as e:
        print(f"[Music] VC status error: {e}")


# ---- Now Playing card (album art + live progress bar + buttons) ----
now_playing_messages = {}
_np_artwork = {}
_progress_tasks = {}
_np_swept_channels = set()
_resume_card_pos = {}
# Resume-after-redeploy: snapshot live playback to bot_config every few seconds
# and restore it when the node comes back, so a redeploy rejoins, re-posts the
# card, and picks up mid-song. _music_state_ready gates persistence so the empty
# boot state can't clobber the snapshot before it's restored.
_music_state_ready = False
_music_restore_done = False

# Portable unicode button icons (this is a different Discord app than Utilities).
EMOJI_HEART = "🤍"
EMOJI_HEART_RED = "❤️"
EMOJI_SKIP = "⏭️"
EMOJI_BACK = "⏮️"
# The DJ ("activate DJ") button shows the Oversite logo, not the mirrored
# headphones emoji — same id the welcome message uses, so it always renders here.
EMOJI_DJ_OFF = discord.PartialEmoji(name="oversite", id=WELCOME_EMOJI_ID)
EMOJI_DJ_ON = discord.PartialEmoji(name="oversite", id=WELCOME_EMOJI_ID)
EMOJI_PAUSE = "⏸️"
EMOJI_PLAY = "▶️"
MUSIC_X_LABEL = "ㅤㅤㅤㅤㅤㅤ    ㅤ  ㅤ     ㅤㅤ✕ㅤㅤㅤㅤㅤㅤ   ㅤ  ㅤ     ㅤㅤ"

# The Utilities bot's custom button emojis live on ITS application, so this bot
# can't reuse their ids. Instead we pull the same images from Discord's public
# emoji CDN and (re)upload them to THIS bot's application once, then swap the
# unicode fallbacks above for the matching app emojis — identical look, no manual
# uploads. Source ids are from the Utilities app.
_SRC_EMOJIS = {
    "music_heart": 1504992052323025056,
    "music_heart_filled": 1505000754426024010,
    "music_skip": 1504992134371999924,
    "music_back": 1514072205103857778,
    "music_pause": 1504992315029065918,
    "music_play": 1504992503601041549,
}
_app_emojis = {}
_emojis_ready = False


async def _ensure_app_emojis():
    """Idempotently mirror the music button emojis onto this bot's application."""
    global _emojis_ready, EMOJI_HEART, EMOJI_HEART_RED, EMOJI_SKIP, EMOJI_BACK
    global EMOJI_DJ_OFF, EMOJI_DJ_ON, EMOJI_PAUSE, EMOJI_PLAY
    if _emojis_ready:
        return
    try:
        app_id = bot.application_id or (bot.user.id if bot.user else None)
        if not app_id:
            return
        existing = {}
        try:
            data = await bot.http.request(discord.http.Route("GET", "/applications/{app_id}/emojis", app_id=app_id))
            for e in (data.get("items") if isinstance(data, dict) else data) or []:
                existing[e["name"]] = e
        except Exception as le:
            print(f"[Emoji] list failed: {le}")
        import base64 as _b64
        for name, src_id in _SRC_EMOJIS.items():
            e = existing.get(name)
            if not e:
                img = None
                try:
                    async with aiohttp.ClientSession() as s:
                        for ext, mime in (("png", "image/png"), ("gif", "image/gif")):
                            async with s.get(f"https://cdn.discordapp.com/emojis/{src_id}.{ext}?size=128") as r:
                                if r.status == 200:
                                    img = f"data:{mime};base64," + _b64.b64encode(await r.read()).decode()
                                    break
                except Exception:
                    pass
                if not img:
                    continue
                try:
                    e = await bot.http.request(
                        discord.http.Route("POST", "/applications/{app_id}/emojis", app_id=app_id),
                        json={"name": name, "image": img})
                    print(f"[Emoji] uploaded {name} -> {e.get('id')}")
                except Exception as ce:
                    print(f"[Emoji] create {name} failed: {ce}")
                    continue
            if e:
                _app_emojis[name] = discord.PartialEmoji(name=name, id=int(e["id"]), animated=bool(e.get("animated")))
        g = _app_emojis.get
        EMOJI_HEART = g("music_heart", EMOJI_HEART)
        EMOJI_HEART_RED = g("music_heart_filled", EMOJI_HEART_RED)
        EMOJI_SKIP = g("music_skip", EMOJI_SKIP)
        EMOJI_BACK = g("music_back", EMOJI_BACK)
        # DJ button keeps the Oversite logo set above — not mirrored.
        EMOJI_PAUSE = g("music_pause", EMOJI_PAUSE)
        EMOJI_PLAY = g("music_play", EMOJI_PLAY)
        _emojis_ready = True
        print(f"[Emoji] ready: {list(_app_emojis.keys())}")
    except Exception as e:
        print(f"[Emoji] ensure failed: {e}")


PROGRESS_IMG_WIDTH = 300
PROGRESS_FONT_SIZE = 16
PROGRESS_BAR_THICKNESS = 5
PROGRESS_BAR_SEGMENTS = 18
PROGRESS_UPDATE_SECONDS = 5


def _fmt_time(ms) -> str:
    m, s = divmod(int(ms or 0) // 1000, 60)
    return f"{m}:{s:02d}"


def build_progress_bar(position_ms, length_ms) -> str:
    if not length_ms:
        return ""
    frac = max(0.0, min(1.0, (position_ms or 0) / length_ms))
    idx = int(frac * (PROGRESS_BAR_SEGMENTS - 1))
    return "━" * idx + "●" + "─" * (PROGRESS_BAR_SEGMENTS - 1 - idx)


def _progress_line(track, position_ms, v2: bool = False) -> str:
    bar = build_progress_bar(position_ms, track.length or 0)
    times = f"{_fmt_time(position_ms)} / {_fmt_time(track.length or 0)}"
    if not bar:
        return times
    return f"{bar}\n-# {times}" if v2 else f"{bar}\n{times}"


def _render_progress_image(position_ms, length_ms):
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None
    try:
        if not length_ms:
            return None
        frac = max(0.0, min(1.0, (position_ms or 0) / length_ms))
        bar_h = PROGRESS_BAR_THICKNESS
        knob_h = bar_h * 2 + 1
        knob_w = max(6, bar_h + 2)
        W, H = PROGRESS_IMG_WIDTH, PROGRESS_FONT_SIZE + knob_h + 10
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        font = None
        for _fp in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
                    "/usr/share/fonts/TTF/DejaVuSans.ttf",
                    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"):
            try:
                font = ImageFont.truetype(_fp, PROGRESS_FONT_SIZE)
                break
            except Exception:
                continue
        if font is None:
            try:
                font = ImageFont.load_default(size=PROGRESS_FONT_SIZE)
            except Exception:
                font = ImageFont.load_default()
        left_txt, right_txt = _fmt_time(position_ms), _fmt_time(length_ms)
        text_color = (181, 184, 190, 255)
        d.text((0, 2), left_txt, font=font, fill=text_color)
        d.text((W - d.textlength(right_txt, font=font), 2), right_txt, font=font, fill=text_color)
        bx0, bx1 = 0, W - 1
        y0 = H - 6 - bar_h
        r = bar_h // 2
        d.rounded_rectangle([bx0, y0, bx1, y0 + bar_h], radius=r, fill=(66, 68, 75, 255))
        fill_x = bx0 + max(bar_h, int(frac * (bx1 - bx0)))
        d.rounded_rectangle([bx0, y0, fill_x, y0 + bar_h], radius=r, fill=(59, 130, 246, 255))
        kx = min(max(bx0 + int(frac * (bx1 - bx0)) - knob_w // 2, bx0), bx1 - knob_w)
        ky = y0 + bar_h // 2 - knob_h // 2
        d.rounded_rectangle([kx, ky, kx + knob_w, ky + knob_h], radius=max(2, knob_w // 2 - 1), fill=(214, 220, 232, 255))
        buf = io.BytesIO()
        img.save(buf, "PNG")
        buf.seek(0)
        return buf
    except Exception as e:
        print(f"[Music] Progress image render failed: {e}")
        return None


def _build_np_embed(track, artwork, position_ms, with_image: bool = False):
    artist = getattr(track, "author", None)
    if with_image:
        value = artist if artist else "​"
    else:
        value = f"{artist}\n{_progress_line(track, position_ms)}" if artist else _progress_line(track, position_ms)
    embed = discord.Embed(color=0x242429)
    embed.set_author(name="Now Playing")
    embed.add_field(name=track.title, value=value, inline=False)
    if artwork:
        embed.set_thumbnail(url=artwork)
    if with_image:
        embed.set_image(url="attachment://progress.png")
    return embed


def _npv2_emoji(pe):
    if isinstance(pe, str):
        return {"name": pe}
    try:
        out = {"id": str(pe.id), "name": pe.name}
        if getattr(pe, "animated", False):
            out["animated"] = True
        return out
    except Exception:
        return None


def _build_npv2_container(track, artwork, position_ms, with_image: bool = False):
    artist = getattr(track, "author", None)
    title_line = f"**{track.title} - {artist}**" if artist else f"**{track.title}**"
    section = {"type": 9, "components": [{"type": 10, "content": f"**Now playing**\n{title_line}"}]}
    if artwork:
        section["accessory"] = {"type": 11, "media": {"url": artwork}}
    if with_image:
        progress_components = [{"type": 12, "items": [{"media": {"url": "attachment://progress.png"}}]}]
    else:
        progress_components = [{"type": 10, "content": _progress_line(track, position_ms, v2=True)}]
    return {
        "type": 17,
        "components": [
            section, *progress_components,
            {"type": 14, "divider": True, "spacing": 1},
            {"type": 1, "components": [
                {"type": 2, "style": 2, "custom_id": "npv2_like", "emoji": _npv2_emoji(EMOJI_HEART)},
                {"type": 2, "style": 2, "custom_id": "npv2_back", "emoji": _npv2_emoji(EMOJI_BACK)},
                {"type": 2, "style": 2, "custom_id": "npv2_pause", "emoji": _npv2_emoji(EMOJI_PAUSE)},
                {"type": 2, "style": 2, "custom_id": "npv2_skip", "emoji": _npv2_emoji(EMOJI_SKIP)},
                {"type": 2, "style": 2, "custom_id": "npv2_dj", "emoji": _npv2_emoji(EMOJI_DJ_OFF)},
            ]},
            {"type": 1, "components": [
                {"type": 2, "style": 4, "custom_id": "npv2_disconnect", "label": MUSIC_X_LABEL},
            ]},
        ],
    }


class NowPlayingView(discord.ui.View):
    def __init__(self, guild_id):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        # Swap the unicode placeholders for this app's real emojis (resolved by
        # _ensure_app_emojis before the first card renders).
        try:
            self.like.emoji = EMOJI_HEART
            self.back.emoji = EMOJI_BACK
            self.pause.emoji = EMOJI_PAUSE
            self.skip.emoji = EMOJI_SKIP
            self.dj_toggle.emoji = EMOJI_DJ_OFF
        except Exception:
            pass

    @discord.ui.button(emoji=EMOJI_HEART, style=discord.ButtonStyle.secondary, row=0)
    async def like(self, interaction, button):
        vc = interaction.guild.voice_client
        if vc and vc.current:
            title = vc.current.title
            _artist = getattr(vc.current, "author", None)
            if _artist:
                title = f"{title} — {_artist}"
            liked_songs.setdefault(interaction.user.id, [])
            if title not in liked_songs[interaction.user.id]:
                liked_songs[interaction.user.id].append(title)
                asyncio.create_task(save_liked_song_to_supabase(interaction.user.id, title))
                _adjust_taste(interaction.guild.id, _artist, 3.0)
                await interaction.response.send_message(f"Added **{title}** to your favorites.", ephemeral=True, delete_after=5)
            else:
                liked_songs[interaction.user.id].remove(title)
                asyncio.create_task(delete_liked_song_from_supabase(interaction.user.id, title))
                _adjust_taste(interaction.guild.id, _artist, -1.0)
                await interaction.response.send_message(f"Removed **{title}** from your favorites.", ephemeral=True, delete_after=5)
        else:
            await interaction.response.send_message("Nothing playing.", ephemeral=True)

    @discord.ui.button(emoji=EMOJI_BACK, style=discord.ButtonStyle.secondary, row=0)
    async def back(self, interaction, button):
        vc = interaction.guild.voice_client
        hist = track_history.get(interaction.guild.id, [])
        if not vc or len(hist) < 2:
            await interaction.response.send_message("No previous song.", ephemeral=True)
            return
        prev = hist[-2]
        del hist[-2:]
        try:
            _adjust_taste(interaction.guild.id, getattr(prev, "author", None), 2.0)
            await vc.play(prev)
            await interaction.response.send_message("Playing previous song.", ephemeral=True, delete_after=3)
        except Exception as e:
            print(f"[Music] Back error: {e}")
            await interaction.response.send_message("Couldn't go back.", ephemeral=True)

    @discord.ui.button(emoji=EMOJI_PAUSE, style=discord.ButtonStyle.secondary, custom_id="music_pause_toggle", row=0)
    async def pause(self, interaction, button):
        vc = interaction.guild.voice_client
        if not vc:
            await interaction.response.send_message("Not connected.", ephemeral=True)
            return
        # Flip the icon and ack instantly, then apply the pause.
        new_paused = not vc.paused
        button.emoji = EMOJI_PLAY if new_paused else EMOJI_PAUSE
        await interaction.response.edit_message(view=self)
        try:
            await vc.pause(new_paused)
        except Exception:
            pass

    @discord.ui.button(emoji=EMOJI_SKIP, style=discord.ButtonStyle.secondary, row=0)
    async def skip(self, interaction, button):
        vc = interaction.guild.voice_client
        if not (vc and vc.playing):
            await interaction.response.send_message("Nothing playing.", ephemeral=True)
            return
        # Ack immediately (no spinner) — then skip. The queue top-up runs in the
        # background so it never delays the button.
        await interaction.response.defer()
        _taste_event_current(interaction.guild, -1.5)
        session = auto_music_sessions.get(interaction.guild.id)
        if session and len(vc.queue) < 3 and interaction.guild.id not in _dj_mode:
            asyncio.create_task(_skip_refill_bg(interaction.guild.id, session))
        await vc.skip(force=True)

    @discord.ui.button(emoji=EMOJI_DJ_OFF, style=discord.ButtonStyle.secondary, row=0)
    async def dj_toggle(self, interaction, button):
        guild = interaction.guild
        if not (DJ_ENABLED and DJ_PUBLIC_URL):
            return await interaction.response.send_message("DJ mode isn't set up on this bot yet.", ephemeral=True)
        if guild.id in _dj_mode:
            await interaction.response.send_message("Next set coming up.", ephemeral=True, delete_after=3)
            asyncio.create_task(_dj_start_set(guild))
        else:
            _dj_mode.add(guild.id)
            _dj_counters[guild.id] = 0
            await interaction.response.send_message("DJ mode on.", ephemeral=True, delete_after=3)
            asyncio.create_task(_dj_start_set(guild, first_activation=True))

    @discord.ui.button(label=MUSIC_X_LABEL, style=discord.ButtonStyle.danger, row=1)
    async def disconnect(self, interaction, button):
        if not is_dj(interaction.user):
            await interaction.response.send_message("You need a DJ role to disconnect.", ephemeral=True)
            return
        vc = interaction.guild.voice_client
        if vc and vc.channel:
            _cancel_progress(interaction.guild.id)
            try:
                await set_vc_status(vc.channel, None)
            except Exception:
                pass
            old_msg = now_playing_messages.pop(interaction.guild.id, None)
            if old_msg:
                try:
                    await old_msg.delete()
                except Exception:
                    pass
            await vc.disconnect()
        await interaction.response.send_message("Disconnected.", ephemeral=True, delete_after=3)


async def _skip_refill_bg(gid, session):
    """Top up the radio queue after a skip — in the background, so it never
    delays the Skip button. Guarded so refills can't stack up and flood the node."""
    if gid in _topup_busy:
        return
    _topup_busy.add(gid)
    try:
        guild = bot.get_guild(gid)
        vc = guild.voice_client if guild else None
        if not vc:
            return
        for t in await get_genre_playlist_tracks(session.get("genre", "pop"), count=8, guild_id=gid, source="skip-refill"):
            await vc.queue.put_wait(t)
    except Exception as e:
        print(f"[Skip] refill error: {e}")
    finally:
        _topup_busy.discard(gid)


def _cancel_progress(guild_id: int):
    t = _progress_tasks.pop(guild_id, None)
    if t:
        t.cancel()
    _np_artwork.pop(guild_id, None)


def _npv2_multipart(payload, buf):
    file = discord.File(buf, "progress.png")
    form = [
        {"name": "payload_json", "value": json.dumps(payload)},
        {"name": "files[0]", "value": file.fp, "filename": file.filename, "content_type": "application/octet-stream"},
    ]
    return form, [file]


async def _progress_updater(guild_id: int):
    try:
        while True:
            await asyncio.sleep(PROGRESS_UPDATE_SECONDS)
            guild = bot.get_guild(guild_id)
            if not guild:
                return
            vc = guild.voice_client
            msg = now_playing_messages.get(guild_id)
            if not vc or not getattr(vc, "current", None) or not msg:
                return
            if getattr(vc, "paused", False):
                continue
            track = vc.current
            position = int(getattr(vc, "position", 0) or 0)
            artwork = _np_artwork.get(guild_id)
            try:
                buf = await asyncio.to_thread(_render_progress_image, position, track.length or 0)
                if music_config.get("now_playing_v2"):
                    container = _build_npv2_container(track, artwork, position, with_image=buf is not None)
                    edit_payload = {"components": [container]}
                    route = discord.http.Route("PATCH", "/channels/{channel_id}/messages/{message_id}",
                                               channel_id=msg.channel.id, message_id=msg.id)
                    if buf:
                        edit_payload["attachments"] = [{"id": "0"}]
                        form, files = _npv2_multipart(edit_payload, buf)
                        await bot.http.request(route, form=form, files=files)
                    else:
                        await bot.http.request(route, json=edit_payload)
                else:
                    embed = _build_np_embed(track, artwork, position, with_image=buf is not None)
                    if buf:
                        await msg.edit(embed=embed, attachments=[discord.File(buf, "progress.png")])
                    else:
                        await msg.edit(embed=embed)
            except Exception as e:
                print(f"[Music] Progress update stopped: {e}")
                return
    except asyncio.CancelledError:
        pass


async def send_now_playing_v2(guild, track, channel, artwork, position_ms: int = 0):
    buf = await asyncio.to_thread(_render_progress_image, position_ms, track.length or 0)
    container = _build_npv2_container(track, artwork, position_ms, with_image=buf is not None)
    payload = {"flags": 32768, "components": [container]}
    route = discord.http.Route("POST", "/channels/{channel_id}/messages", channel_id=channel.id)
    if buf:
        form, files = _npv2_multipart(payload, buf)
        data = await bot.http.request(route, form=form, files=files)
    else:
        data = await bot.http.request(route, json=payload)
    try:
        return channel.get_partial_message(int(data["id"]))
    except Exception:
        try:
            return await channel.fetch_message(int(data["id"]))
        except Exception:
            return None


async def purge_old_now_playing(channel):
    try:
        async for m in channel.history(limit=25):
            if m.author.id != bot.user.id:
                continue
            is_card = any((e.author and e.author.name == "Now Playing") for e in m.embeds) or bool(getattr(m.flags, "value", 0) & 32768)
            if is_card:
                try:
                    await m.delete()
                    await asyncio.sleep(0.6)
                except Exception:
                    pass
    except Exception as e:
        print(f"[Music] Stale card sweep failed: {e}")


async def handle_npv2_button(interaction, cid):
    try:
        guild = interaction.guild
        if not guild:
            return
        vc = guild.voice_client
        if cid == "npv2_like":
            if not vc or not vc.current:
                return await interaction.response.send_message("Nothing playing.", ephemeral=True)
            title = vc.current.title
            _artist = getattr(vc.current, "author", None)
            if _artist:
                title = f"{title} — {_artist}"
            uid = interaction.user.id
            liked_songs.setdefault(uid, [])
            if title not in liked_songs[uid]:
                liked_songs[uid].append(title)
                asyncio.create_task(save_liked_song_to_supabase(uid, title))
                _adjust_taste(guild.id, _artist, 3.0)
                await interaction.response.send_message(f"Added **{title}** to your favorites.", ephemeral=True, delete_after=5)
            else:
                liked_songs[uid].remove(title)
                asyncio.create_task(delete_liked_song_from_supabase(uid, title))
                _adjust_taste(guild.id, _artist, -1.0)
                await interaction.response.send_message(f"Removed **{title}** from your favorites.", ephemeral=True, delete_after=5)
        elif cid == "npv2_dj":
            if not (DJ_ENABLED and DJ_PUBLIC_URL):
                return await interaction.response.send_message("DJ mode isn't set up on this bot yet.", ephemeral=True)
            if guild.id in _dj_mode:
                await interaction.response.send_message("Next set coming up.", ephemeral=True, delete_after=3)
                asyncio.create_task(_dj_start_set(guild))
            else:
                _dj_mode.add(guild.id)
                _dj_counters[guild.id] = 0
                await interaction.response.send_message("DJ mode on.", ephemeral=True, delete_after=3)
                asyncio.create_task(_dj_start_set(guild, first_activation=True))
        elif cid == "npv2_back":
            hist = track_history.get(guild.id, [])
            if not vc or len(hist) < 2:
                return await interaction.response.send_message("No previous song.", ephemeral=True)
            prev = hist[-2]
            del hist[-2:]
            try:
                _adjust_taste(guild.id, getattr(prev, "author", None), 2.0)
                await vc.play(prev)
                await interaction.response.send_message("Playing previous song.", ephemeral=True, delete_after=3)
            except Exception as e:
                print(f"[Music] Back error: {e}")
                await interaction.response.send_message("Couldn't go back.", ephemeral=True)
        elif cid == "npv2_skip":
            if not vc or not vc.playing:
                return await interaction.response.send_message("Nothing playing.", ephemeral=True)
            _taste_event_current(guild, -1.5)
            session = auto_music_sessions.get(guild.id)
            if session and len(vc.queue) < 3 and guild.id not in _dj_mode:
                try:
                    for t in await get_genre_playlist_tracks(session.get("genre", "pop"), count=20, guild_id=guild.id, source="skip-v2-refill"):
                        await vc.queue.put_wait(t)
                except Exception as _e:
                    print(f"[Skip] Refill error: {_e}")
            await vc.skip(force=True)
            await interaction.response.send_message("Skipped.", ephemeral=True, delete_after=3)
        elif cid == "npv2_pause":
            if not vc:
                return await interaction.response.send_message("Not connected.", ephemeral=True)
            await vc.pause(not vc.paused)
            await interaction.response.send_message("Paused." if vc.paused else "Resumed.", ephemeral=True, delete_after=3)
        elif cid == "npv2_disconnect":
            if not is_dj(interaction.user):
                return await interaction.response.send_message("You need a DJ role to disconnect.", ephemeral=True)
            if vc and vc.channel:
                _cancel_progress(guild.id)
                try:
                    await set_vc_status(vc.channel, None)
                except Exception:
                    pass
                old_msg = now_playing_messages.pop(guild.id, None)
                if old_msg:
                    try:
                        await old_msg.delete()
                    except Exception:
                        pass
                await vc.disconnect()
            await interaction.response.send_message("Disconnected.", ephemeral=True, delete_after=3)
    except Exception as e:
        print(f"[Music] V2 button error: {e}")


async def send_now_playing(guild, track, channel):
    try:
        await _ensure_app_emojis()
        artist_name = getattr(track, "author", "") or ""
        artwork = await get_spotify_artwork(track.title, artist_name)
        if not artwork:
            artwork = getattr(track, "artwork", None) or getattr(track, "thumbnail", None)
        if not artwork and getattr(track, "identifier", None):
            artwork = f"https://img.youtube.com/vi/{track.identifier}/maxresdefault.jpg"

        _pos0 = _resume_card_pos.pop(guild.id, None)
        if _pos0 is None:
            _pos0 = 0
            try:
                if guild.voice_client and getattr(guild.voice_client, "current", None) is track:
                    _pos0 = int(getattr(guild.voice_client, "position", 0) or 0)
            except Exception:
                _pos0 = 0
        _buf0 = await asyncio.to_thread(_render_progress_image, _pos0, track.length or 0)
        embed = _build_np_embed(track, artwork, _pos0, with_image=_buf0 is not None)

        if guild.voice_client and guild.voice_client.channel:
            try:
                author = getattr(track, "author", None)
                new_status = f"{track.title} - {author}" if author else track.title
                # Update whenever the SONG changes (keeps the VC status in sync
                # with the card, even on rapid skips) — not on a fixed timer.
                _last = getattr(bot, "_last_vc_status_text", {})
                if _last.get(guild.id) != new_status:
                    await set_vc_status(guild.voice_client.channel, new_status)
                    _last[guild.id] = new_status
                    bot._last_vc_status_text = _last
            except Exception:
                pass

        if guild.id not in now_playing_messages and channel.id not in _np_swept_channels:
            _np_swept_channels.add(channel.id)
            await purge_old_now_playing(channel)

        _cancel_progress(guild.id)
        old_msg = now_playing_messages.pop(guild.id, None)
        if old_msg:
            try:
                await old_msg.delete()
            except Exception:
                pass

        if music_config.get("now_playing_v2"):
            msg = await send_now_playing_v2(guild, track, channel, artwork, _pos0)
            if msg:
                now_playing_messages[guild.id] = msg
                _np_artwork[guild.id] = artwork
                _progress_tasks[guild.id] = asyncio.create_task(_progress_updater(guild.id))
            return

        if _buf0:
            msg = await channel.send(embed=embed, view=NowPlayingView(guild.id), file=discord.File(_buf0, "progress.png"))
        else:
            msg = await channel.send(embed=embed, view=NowPlayingView(guild.id))
        now_playing_messages[guild.id] = msg
        _np_artwork[guild.id] = artwork
        _progress_tasks[guild.id] = asyncio.create_task(_progress_updater(guild.id))
    except Exception as e:
        print(f"[Music] Now playing error: {e}")


# ---- AI DJ "Carla" ----
DJ_ENABLED = True
DJ_VOICE = "en-US-GuyNeural"
DJ_PUBLIC_URL = os.getenv("DJ_PUBLIC_URL", "").rstrip("/")
DJ_SET_SIZE = 5
DJ_VOICE_BOOST = 2.2
_dj_clip_dir = "/tmp/dj"
_dj_mode = set()
_dj_counters = {}
_dj_pending = {}
_dj_set = {}
_dj_sets_since_artist = {}
_dj_recent_artists = {}
_dj_recent_genres = {}
_dj_prev_volume = {}
auto_music_sessions = {}
_topup_busy = set()  # guilds currently fetching queue top-ups (prevents stacking searches that overload the node)

# ---- TTS ( /join ) ----
# When the bot is /join'd into a voice channel it reads that channel's built-in
# text chat aloud, mirroring the Discord-TTS-Bot behaviour: "{name} said {msg}",
# the name only when the speaker changes (or after a 60s gap), mentions resolved
# to names, emoji made readable, acronyms expanded, links/files summarised.
# Playback reuses the DJ ElevenLabs clip pipeline, so it OVERRIDES music.
_tts_channels = {}  # guild_id -> voice-channel id whose chat is being read
_tts_queue = {}     # guild_id -> list[Playable] waiting to be spoken
_tts_busy = {}      # guild_id -> True while a clip is playing
_tts_announce = {}  # guild_id -> (last_speaker_id, last_announce_unix)
_tts_nicks = {}     # "guild_id:user_id" -> TTS nickname override (from /set nick)
TTS_VOICE_ID = os.getenv("TTS_ELEVEN_VOICE_ID", "s3TPKV1kjDlVtZbl4Ksh")
TTS_SPEED = float(os.getenv("TTS_SPEED", "1.15"))  # ElevenLabs voice speed (0.7–1.2)
# Which voice to speak with. "gtts" = the Google Translate TTS voice the
# Discord-TTS-Bot uses by default; "eleven" = the ElevenLabs voice above.
TTS_ENGINE = os.getenv("TTS_ENGINE", "gtts").lower()
TTS_LANG = os.getenv("TTS_LANG", "en")
# TTS accent (mapped to the Google language code, e.g. co.uk -> en-GB).
# Always the BRITISH voice by default; com.au = Australian, com = US,
# ca = Canadian, ie = Irish, co.in = Indian via env or the dashboard block.
TTS_TLD = os.getenv("TTS_TLD", "co.uk")
# Playback speed (1.0 = normal). Sped up via an ffmpeg atempo filter — the raw
# Google voice reads slowly.
TTS_PLAYBACK_SPEED = float(os.getenv("TTS_PLAYBACK_SPEED", "1.0"))

# Live TTS settings, overridable from the dashboard "Text-to-Speech" block.
tts_config = {
    "engine": TTS_ENGINE,          # "gtts" | "eleven"
    "accent": TTS_TLD,             # gTTS host tld: co.uk, com, com.au, ca, ie, co.in
    "speed": TTS_PLAYBACK_SPEED,   # playback speed (timescale)
    "voice_id": TTS_VOICE_ID,      # ElevenLabs voice (when engine=eleven)
    "join_message": "",            # blank -> built-in default
    "leave_message": "",
}


async def _tts_set_speed(vc, speed):
    """Apply (or reset) a timescale speed filter on the player."""
    try:
        import wavelink as _wl
        filters = vc.filters
        filters.timescale.set(speed=max(0.5, float(speed)), pitch=1.0, rate=1.0)
        await vc.set_filters(filters)
    except Exception as e:
        print(f"[TTS] speed filter failed: {e}")


async def _tts_reset_speed(vc):
    try:
        import wavelink as _wl
        await vc.set_filters(_wl.Filters())
    except Exception:
        pass


def _gtts_chunks(text, limit=200):
    """Split text into <=~200-char pieces on word boundaries (Google TTS caps
    each request)."""
    words = (text or "").split(" ")
    chunks, cur = [], ""
    for w in words:
        if len(cur) + len(w) + 1 > limit:
            if cur:
                chunks.append(cur)
            cur = w[:limit]
        else:
            cur = (cur + " " + w).strip()
    if cur:
        chunks.append(cur)
    return chunks or [(text or "")[:limit]]


# TTS is played NATIVELY (discord.py VoiceClient + FFmpeg) with locally
# synthesized audio — the architecture of the reference TTS bot (Serenity +
# Songbird, no Lavalink): synthesize an mp3, play the bytes through the
# library's own voice stack. No external audio server to drop.
_TTS_TMP_DIR = os.path.join(tempfile.gettempdir(), "oversite_tts")

# Edge-TTS voices matched to the old gTTS "accent" setting, overridable via env.
_TTS_EDGE_VOICES = {
    "co.uk": "en-GB-SoniaNeural", "com": "en-US-JennyNeural",
    "com.au": "en-AU-NatashaNeural", "ca": "en-CA-ClaraNeural",
    "ie": "en-IE-EmilyNeural", "co.in": "en-IN-NeerjaNeural",
}
TTS_EDGE_VOICE = os.getenv("TTS_EDGE_VOICE", "")


def _tts_edge_voice():
    return TTS_EDGE_VOICE or _TTS_EDGE_VOICES.get(tts_config["accent"], "en-GB-SoniaNeural")


def _tts_edge_rate():
    """tts speed (e.g. 1.15) -> edge-tts rate string ('+15%')."""
    pct = int(round((float(tts_config["speed"]) - 1.0) * 100))
    return f"{'+' if pct >= 0 else ''}{pct}%"


# The accent is carried by the LANGUAGE CODE (tl=en-AU etc). The old host-TLD
# trick (translate.google.com.au) no longer changes the voice — com and com.au
# return byte-identical audio; tl=en-AU / en-GB genuinely differ.
_GTTS_ACCENT_LANG = {
    "com.au": "en-AU", "co.uk": "en-GB", "com": "en",
    "ca": "en-CA", "ie": "en-IE", "co.in": "en-IN",
}


async def _gtts_fetch(text, path):
    """Google Translate TTS — the reference TTS bot's voice (mode gTTS, no speed
    change), accent via tl=en-AU/en-GB/…. Chunks >200 chars are stitched."""
    import urllib.parse
    tl = _GTTS_ACCENT_LANG.get(tts_config["accent"], TTS_LANG)
    data = b""
    try:
        async with _http() as client:
            for ch in _gtts_chunks(text, 200):
                url = (f"https://translate.google.com/translate_tts?ie=UTF-8&client=tw-ob"
                       f"&tl={urllib.parse.quote(tl)}&q={urllib.parse.quote(ch)}")
                r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
                if r.status_code == 200 and r.content:
                    data += r.content
    except Exception as e:
        print(f"[TTS] gTTS fetch failed: {e}")
    if not data:
        return None
    try:
        with open(path, "wb") as f:
            f.write(data)
        return path
    except Exception as e:
        print(f"[TTS] gTTS save failed: {e}")
        return None


async def _tts_synth(text):
    """Synthesize `text` to a local mp3 and return its path (None on failure).
    Engine 'gtts' (default) = the Google Translate voice the reference TTS bot
    uses; 'eleven' = ElevenLabs; edge-tts is the fallback when the primary
    engine fails."""
    os.makedirs(_TTS_TMP_DIR, exist_ok=True)
    import uuid
    path = os.path.join(_TTS_TMP_DIR, f"{uuid.uuid4().hex}.mp3")

    if tts_config["engine"] == "eleven" and ELEVEN_API_KEY and tts_config["voice_id"]:
        try:
            voice_settings = {"stability": 0.5, "similarity_boost": 0.8,
                              "speed": max(0.7, min(1.2, float(TTS_SPEED)))}
            async with _http() as client:
                r = await client.post(
                    f"https://api.elevenlabs.io/v1/text-to-speech/{tts_config['voice_id']}",
                    headers={"xi-api-key": ELEVEN_API_KEY},
                    json={"text": text[:400], "model_id": "eleven_multilingual_v2",
                          "voice_settings": voice_settings},
                    timeout=30,
                )
            if r.status_code == 200 and r.content:
                with open(path, "wb") as f:
                    f.write(r.content)
                return path
            print(f"[TTS] elevenlabs HTTP {r.status_code}")
        except Exception as e:
            print(f"[TTS] elevenlabs failed: {e}")
    else:
        # Primary: the reference bot's voice — Google Translate TTS.
        if await _gtts_fetch(text[:600], path):
            return path

    # Fallback: edge-tts (free, reliable, accent-matched).
    try:
        import edge_tts
        await edge_tts.Communicate(text[:600], _tts_edge_voice(), rate=_tts_edge_rate()).save(path)
        if os.path.exists(path) and os.path.getsize(path) > 0:
            return path
    except Exception as e:
        print(f"[TTS] edge-tts failed: {e}")
    return None

_TTS_ACRONYMS = {
    "iirc": "if I recall correctly", "afaik": "as far as I know",
    "wdym": "what do you mean", "imo": "in my opinion", "brb": "be right back",
    "wym": "what you mean", "irl": "in real life", "jk": "just kidding",
    "btw": "by the way", ":)": "smiley face", "gtg": "got to go", "rn": "right now",
    ":(": "sad face", "ig": "i guess", "ppl": "people", "rly": "really",
    "cya": "see ya", "ik": "i know", "@": "at",
}


def _tts_attach_format(attachments):
    if len(attachments) >= 2:
        return "multiple files"
    if not attachments:
        return None
    ext = (attachments[0].filename.rsplit(".", 1)[-1] if "." in attachments[0].filename else "").lower()
    groups = {
        "an image file": {"bmp", "gif", "ico", "png", "psd", "svg", "jpg", "jpeg", "webp"},
        "an audio file": {"mid", "midi", "mp3", "ogg", "wav", "wma"},
        "a video file": {"avi", "mp4", "wmv", "m4v", "mpg", "mpeg", "mov"},
        "a compressed file": {"zip", "7z", "rar", "gz", "xz"},
        "a text file": {"doc", "docx", "txt", "odt", "rtf"},
        "a script file": {"bat", "sh", "jar", "py", "php"},
        "a program file": {"apk", "exe", "msi", "deb"},
        "a disk image": {"dmg", "iso", "img", "ima"},
    }
    for fmt, exts in groups.items():
        if ext in exts:
            return fmt
    return "a file"


def _tts_collapse_repeats(s, limit=5):
    """Collapse a run of the same character to at most `limit` (so 'aaaaaaa'
    doesn't become an eternity)."""
    out, prev, run = [], None, 0
    for ch in s:
        if ch == prev:
            run += 1
        else:
            prev, run = ch, 1
        if run <= limit:
            out.append(ch)
    return "".join(out)


def _tts_name_for(message):
    """TTS name: /set nick override, else the member's display name (server nick
    -> global name -> username)."""
    return _tts_nicks.get(f"{message.guild.id}:{message.author.id}") or message.author.display_name


def _tts_should_announce(gid, uid):
    """Say the name when the speaker changed, or 60s+ since they last spoke."""
    last = _tts_announce.get(gid)
    now = time.time()
    if last and last[0] == uid and (now - last[1]) < 60:
        return False
    _tts_announce[gid] = (uid, now)
    return True


def _tts_format(message):
    """Build the spoken line from a message (mirrors the reference bot)."""
    # Mentions/channels/roles -> readable names; lowercase like the reference.
    content = (message.clean_content or "")
    if len(content) >= 1500:
        return None
    content = content.lower()
    if content.strip() == "?":
        content = "what"
    # Custom emoji -> "emoji name" / "animated emoji name".
    content = re.sub(r"<(a)?:(\w+):\d+>",
                     lambda m: f"{'animated emoji' if m.group(1) else 'emoji'} {m.group(2)}", content)
    # Acronym expansion (English voice).
    content = " ".join(_TTS_ACRONYMS.get(w, w) for w in content.split(" "))
    # Links -> summarised, not read out.
    had_url = bool(re.search(r"https?://\S+", content))
    if had_url:
        content = re.sub(r"https?://\S+", "", content)
    content = re.sub(r"\s+", " ", content).strip()

    file_fmt = _tts_attach_format(message.attachments)
    say_name = _tts_should_announce(message.guild.id, message.author.id)
    name = _tts_name_for(message) if say_name else None

    if name:
        if content and had_url and file_fmt:
            text = f"{name} sent a link, attached {file_fmt}, and said {content}"
        elif content and had_url:
            text = f"{name} sent a link and said {content}"
        elif content and file_fmt:
            text = f"{name} sent {file_fmt} and said {content}"
        elif content:
            text = f"{name} said {content}"
        elif had_url and file_fmt:
            text = f"{name} sent a link and attached {file_fmt}"
        elif had_url:
            text = f"{name} sent a link"
        elif file_fmt:
            text = f"{name} sent {file_fmt}"
        else:
            text = f"{name} sent a message"
    else:
        if content and had_url:
            text = f"{content} with a link"
        elif content and file_fmt:
            text = f"{content} with {file_fmt}"
        elif content:
            text = content
        elif had_url:
            text = "a link"
        elif file_fmt:
            text = file_fmt
        else:
            text = ""
    text = _tts_collapse_repeats(text)
    # Skip messages that are empty or only symbols.
    if not text or all(c in " ?.)'!\":" for c in text):
        return None
    return text


_FFMPEG_EXE = None


def _ffmpeg_exe():
    """Path to an ffmpeg binary: the system one when present, else the static
    build bundled by the imageio-ffmpeg pip package (works on any host, no
    system packages needed)."""
    global _FFMPEG_EXE
    if _FFMPEG_EXE:
        return _FFMPEG_EXE
    import shutil
    p = shutil.which("ffmpeg")
    if not p:
        try:
            import imageio_ffmpeg
            p = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception as e:
            print(f"[TTS] no ffmpeg available: {e}")
            p = "ffmpeg"
    _FFMPEG_EXE = p
    print(f"[TTS] ffmpeg: {p}")
    return p


def _tts_ffmpeg_options(path):
    """FFmpeg options for a synthesized clip. edge-tts bakes the speed into the
    audio; the gTTS/ElevenLabs fallbacks get an atempo filter instead."""
    speed = max(0.5, min(2.0, float(tts_config["speed"])))
    # Heuristic: edge-tts output already carries the rate — only the fallback
    # engines need speeding up. gTTS files come from the stitched fetch.
    if tts_config["engine"] == "eleven" or abs(speed - 1.0) < 0.01:
        return None
    return f'-filter:a "atempo={speed:.2f}"'


async def _tts_play_next(vc):
    """Play the next queued TTS clip (a local mp3 path) natively, or go idle."""
    gid = vc.guild.id
    q = _tts_queue.get(gid) or []
    if not q:
        _tts_busy[gid] = False
        return
    path = q.pop(0)
    _tts_busy[gid] = True

    def _after(err):
        if err:
            print(f"[TTS] playback error: {err}")
        try:
            os.remove(path)
        except Exception:
            pass
        try:
            asyncio.run_coroutine_threadsafe(_tts_play_next(vc), bot.loop)
        except Exception as e:
            print(f"[TTS] next-clip schedule failed: {e}")

    try:
        opts = _tts_ffmpeg_options(path)
        exe = _ffmpeg_exe()
        src = (discord.FFmpegOpusAudio(path, executable=exe, options=opts) if opts
               else discord.FFmpegOpusAudio(path, executable=exe))
        vc.play(src, after=_after)
    except Exception as e:
        print(f"[TTS] play failed: {e}")
        try:
            os.remove(path)
        except Exception:
            pass
        await _tts_play_next(vc)


async def _tts_handle(message):
    """A message was sent in a joined VC's chat — synthesize locally and speak it
    through the native voice connection."""
    gid = message.guild.id
    vc = message.guild.voice_client
    if not vc or not vc.is_connected():
        _tts_channels.pop(gid, None)
        return
    text = _tts_format(message)
    if not text:
        return
    path = await _tts_synth(text)
    if not path:
        return
    _tts_queue.setdefault(gid, []).append(path)
    if not _tts_busy.get(gid) and not vc.is_playing():
        await _tts_play_next(vc)


async def _load_tts_nicks():
    cfg = await _bot_config_get("tts-nicks")
    n = (cfg or {}).get("nicks")
    if isinstance(n, dict):
        _tts_nicks.clear()
        _tts_nicks.update({str(k): str(v) for k, v in n.items()})
        print(f"[TTS] restored {len(_tts_nicks)} nickname(s)")


async def _save_tts_nicks():
    try:
        await _bot_config_upsert("tts-nicks", {"nicks": _tts_nicks})
    except Exception as e:
        print(f"[TTS] nick save failed: {e}")

ELEVEN_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
ELEVEN_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "")

DJ_VIBES = {
    "rnbhiphop": "R&B and hip hop", "dance": "pump-up dance tracks", "pop": "pop hits",
    "country": "country", "rockalternative": "rock and alternative", "latin": "Latin heat",
}
DJ_INTRO_LINES = [
    "Hey, DJ Carla here — you're listening to Oversite Radio. Next up, some {vibe}.",
    "Welcome to Oversite Radio! It's DJ Carla, starting us off with some {vibe}.",
    "You're locked in to Oversite Radio with DJ Carla. First up — a little {vibe}.",
]
DJ_SWITCH_LINES = [
    "Alright — here comes some {vibe}.",
    "Time for a change of pace. Next up: {vibe}.",
    "DJ Carla back with you — let's slide into some {vibe}.",
    "New vibe incoming. Rolling into {vibe} now.",
]


async def _dj_make_clip(text: str, voice_id: str | None = None, speed: float | None = None) -> str | None:
    if not DJ_PUBLIC_URL:
        return None
    vid = voice_id or ELEVEN_VOICE_ID
    voice_settings = {"stability": 0.5, "similarity_boost": 0.8}
    if speed:
        voice_settings["speed"] = max(0.7, min(1.2, float(speed)))  # ElevenLabs range
    try:
        import uuid
        os.makedirs(_dj_clip_dir, exist_ok=True)
        name = f"{uuid.uuid4().hex}.mp3"
        path = os.path.join(_dj_clip_dir, name)
        made = False
        if ELEVEN_API_KEY and vid:
            try:
                async with _http() as client:
                    r = await client.post(
                        f"https://api.elevenlabs.io/v1/text-to-speech/{vid}",
                        headers={"xi-api-key": ELEVEN_API_KEY, "Content-Type": "application/json"},
                        json={"text": text, "model_id": "eleven_turbo_v2_5",
                              "voice_settings": voice_settings}, timeout=20)
                    if r.status_code == 200:
                        with open(path, "wb") as f:
                            f.write(r.content)
                        made = True
            except Exception as ee:
                print(f"[DJ] ElevenLabs failed ({ee}) — Edge-TTS")
        if not made:
            import edge_tts
            await edge_tts.Communicate(text, DJ_VOICE).save(path)
        for f in sorted(os.listdir(_dj_clip_dir))[:-30]:
            try:
                os.remove(os.path.join(_dj_clip_dir, f))
            except Exception:
                pass
        return f"{DJ_PUBLIC_URL}/dj/{name}"
    except Exception as e:
        print(f"[DJ] Clip generation failed: {e}")
        return None


async def _dj_ai_line(vibe: str, first: bool):
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return None
    try:
        _style = random.choice(["high-energy and hyped", "smooth and laid-back", "playful and witty",
                                "warm late-night vibes", "quick and punchy", "cool and confident"])
        prompt = (f"You are DJ Carla, host of Oversite Radio, a Discord music station. "
                  f"Write ONE short spoken intro (max 28 words) for a set of {vibe}. "
                  + ("This is the start of the broadcast - welcome listeners to Oversite Radio. " if first
                     else "You are switching vibes mid-broadcast. ")
                  + f"Tone: {_style}. No emojis, no quotes. Output only the line.")
        async with _http() as client:
            r = await client.post("https://api.anthropic.com/v1/messages",
                headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={"model": "claude-haiku-4-5", "max_tokens": 80, "temperature": 1.0,
                      "messages": [{"role": "user", "content": prompt}]}, timeout=4)
            if r.status_code == 200:
                t = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text").strip()
                if 0 < len(t) < 300:
                    return t
    except Exception as e:
        print(f"[DJ-AI] line gen failed: {e}")
    return None


def _dj_pick_vibe(guild_id):
    import random as _r
    current = (_dj_set.get(guild_id) or {}).get("genre")
    choices = [g for g in DJ_VIBES if g != current] or list(DJ_VIBES)
    taste = music_taste.get(guild_id, {})
    def _w(g):
        pool = GENRE_SONGS.get(g, [])
        s = sum(v for a, v in taste.items() if any(str(e).lower().startswith(a) for e in pool[:30]))
        return max(1.0, 1.0 + s * 0.2)
    return _r.choices(choices, weights=[_w(g) for g in choices], k=1)[0]


async def _dj_ai_pick(guild):
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return (None, None)
    _dj_sets_since_artist[guild.id] = _dj_sets_since_artist.get(guild.id, 0) + 1
    allow_artist = _dj_sets_since_artist[guild.id] >= random.randint(6, 7)
    try:
        taste = music_taste.get(guild.id, {})
        loved = [a for a, s in sorted(taste.items(), key=lambda kv: -kv[1]) if s >= 3][:8]
        recent = _dj_recent_artists.get(guild.id, [])
        recent_g = _dj_recent_genres.get(guild.id, [])
        import json as _json
        prompt = ("You are programming a Discord radio station. Pick what plays next. "
                  "Options: a genre from this list: " + ", ".join(k for k in DJ_VIBES if k not in recent_g) + ". "
                  + (("Or an artist set by a specific popular artist related to what this room loves: "
                      + (", ".join(loved) if loved else "unknown") + ". ") if allow_artist else "Pick a genre only. ")
                  + 'Respond with ONLY JSON: {"type":"genre","value":"<genre key>"} or {"type":"artist","value":"<artist name>"}')
        async with _http() as client:
            r = await client.post("https://api.anthropic.com/v1/messages",
                headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={"model": "claude-haiku-4-5", "max_tokens": 60, "temperature": 1.0,
                      "messages": [{"role": "user", "content": prompt}]}, timeout=4)
            if r.status_code == 200:
                t = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text").strip()
                t = t.replace("```json", "").replace("```", "").strip()
                d = _json.loads(t)
                if d.get("type") == "genre" and d.get("value") in DJ_VIBES and d.get("value") not in recent_g:
                    return ("genre", d["value"])
                if allow_artist and d.get("type") == "artist" and isinstance(d.get("value"), str) and 0 < len(d["value"]) < 60 and d["value"].lower() not in [a.lower() for a in recent]:
                    _dj_sets_since_artist[guild.id] = 0
                    _dj_recent_artists.setdefault(guild.id, []).append(d["value"])
                    _dj_recent_artists[guild.id] = _dj_recent_artists[guild.id][-5:]
                    return ("artist", d["value"])
    except Exception as e:
        print(f"[DJ-AI] pick failed: {e}")
    return (None, None)


async def _dj_fetch_artist_set(guild, artist):
    tracks = []
    try:
        import wavelink as _wl
        names = list(await fetch_artist_songs(artist))
        random.shuffle(names)
        for n in names[:DJ_SET_SIZE + 3]:
            if len(tracks) >= DJ_SET_SIZE:
                break
            try:
                res, _sp = await search_any(n)
                if res and not isinstance(res, _wl.Playlist):
                    tracks.append(res[0])
            except Exception:
                pass
    except Exception as e:
        print(f"[DJ-AI] artist set failed: {e}")
    if len(tracks) >= 3:
        return tracks
    return await get_genre_playlist_tracks(_dj_pick_vibe(guild.id), count=DJ_SET_SIZE, guild_id=guild.id, source="dj-set-fallback")


async def _dj_start_set(guild, first_activation: bool = False):
    try:
        vc = guild.voice_client
        if not vc:
            return
        pick_type, pick_val = await _dj_ai_pick(guild)
        if pick_type == "artist":
            genre = _dj_pick_vibe(guild.id)
            _dj_set[guild.id] = {"genre": genre}
            spoken = f"{pick_val}"
            fetch_task = asyncio.create_task(_dj_fetch_artist_set(guild, pick_val))
        else:
            genre = pick_val if pick_type == "genre" else _dj_pick_vibe(guild.id)
            for _ in range(6):
                if genre not in _dj_recent_genres.get(guild.id, []):
                    break
                genre = _dj_pick_vibe(guild.id)
            _dj_set[guild.id] = {"genre": genre}
            spoken = DJ_VIBES.get(genre, genre)
            _dj_recent_genres.setdefault(guild.id, []).append(genre)
            _dj_recent_genres[guild.id] = _dj_recent_genres[guild.id][-3:]
            fetch_task = asyncio.create_task(get_genre_playlist_tracks(genre, count=DJ_SET_SIZE, guild_id=guild.id, source="dj-set"))
        pool = DJ_INTRO_LINES if first_activation else DJ_SWITCH_LINES
        line = await _dj_ai_line(spoken, first_activation) or random.choice(pool).format(vibe=spoken)
        clip = await _dj_make_clip(line)
        if clip:
            # The clip is a locally generated mp3 — play the file directly
            # through the native player (uri keeps the /dj/ URL so
            # _is_dj_clip still recognizes it).
            clip_path = os.path.join(_dj_clip_dir, clip.rsplit("/", 1)[-1])
            if os.path.exists(clip_path):
                ct = NativeTrack(title="DJ Carla", uri=clip, stream_url=clip)
                ct.local_path = clip_path
                try:
                    vc.queue.clear()
                except Exception:
                    pass
                _dj_pending[guild.id] = {"fetch": fetch_task}
                try:
                    _prev = int(getattr(vc, "volume", 100) or 100)
                    _dj_prev_volume[guild.id] = _prev
                    await vc.set_volume(min(300, int(_prev * DJ_VOICE_BOOST)))
                except Exception:
                    pass
                await vc.play(ct)
                print(f"[DJ] Speaking: {line}")
                return
        tracks = await fetch_task
        if tracks:
            try:
                vc.queue.clear()
            except Exception:
                pass
            for t in tracks[1:]:
                await vc.queue.put_wait(t)
            await vc.play(tracks[0])
    except Exception as e:
        print(f"[DJ] Set start error: {e}")


def _is_dj_clip(track) -> bool:
    u = getattr(track, "uri", "") or ""
    return "/dj/" in u and (not DJ_PUBLIC_URL or u.startswith(DJ_PUBLIC_URL))


async def _dj_serve():
    """Tiny web server so Lavalink can fetch the generated DJ clips."""
    if not DJ_PUBLIC_URL:
        return
    try:
        from aiohttp import web
        app = web.Application()
        os.makedirs(_dj_clip_dir, exist_ok=True)
        app.router.add_static("/dj/", _dj_clip_dir)
        runner = web.AppRunner(app)
        await runner.setup()
        port = int(os.getenv("PORT", 8080))
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()
        print(f"[DJ] Clip server listening on port {port}")
    except Exception as e:
        print(f"[DJ] Clip server failed to start: {e}")


# ---- Wavelink connect (+ optional YT OAuth registration) ----
async def setup_wavelink():
    global music_available
    if wavelink is None:
        print("[Music] wavelink missing — music disabled")
        return
    if not (LAVALINK_URI and LAVALINK_PASSWORD):
        print("[Music] LAVALINK_URI/PASSWORD not set — music disabled")
        return
    try:
        # Failover support: LAVALINK_URI / LAVALINK_PASSWORD may each be a
        # comma-separated list. One password + many URIs = same password for all.
        uris = [u.strip() for u in LAVALINK_URI.split(",") if u.strip()]
        pws = [p.strip() for p in LAVALINK_PASSWORD.split(",")]
        nodes = [wavelink.Node(uri=u, password=(pws[i] if i < len(pws) and pws[i] else pws[0]))
                 for i, u in enumerate(uris)]
        await wavelink.Pool.connect(nodes=nodes, client=bot, cache_capacity=100)
        music_available = True
        print(f"[Music] connecting to Lavalink node(s): {', '.join(uris)}")
        # Only hand our private YouTube OAuth token to a node WE trust —
        # never to a public/community node (its operator would receive it).
        _trust_node = os.getenv("LAVALINK_TRUSTED", "true").lower() == "true"
        if YOUTUBE_OAUTH_REFRESH_TOKEN and _trust_node:
            try:
                _url = f"{uris[0].rstrip('/')}/youtube"
                async with aiohttp.ClientSession() as _s:
                    async with _s.post(_url, headers={"Authorization": LAVALINK_PASSWORD, "Content-Type": "application/json"},
                                       json={"refreshToken": YOUTUBE_OAUTH_REFRESH_TOKEN},
                                       timeout=aiohttp.ClientTimeout(total=10)) as _r:
                        print(f"[OAuth] Registered YT refresh token — status {_r.status}")
            except Exception as _oe:
                print(f"[OAuth] Register failed: {_oe}")
    except Exception as e:
        print(f"[Music] Lavalink connect failed: {e}")


def _wl_nodes():
    try:
        nodes = wavelink.Pool.nodes
        return list(nodes.values()) if isinstance(nodes, dict) else list(nodes)
    except Exception:
        return []


def _wl_has_connected_node():
    """True if at least one Lavalink node is currently CONNECTED."""
    for n in _wl_nodes():
        try:
            st = getattr(n, "status", None)
            if st is not None and "CONNECTED" in str(getattr(st, "name", st)).upper():
                return True
        except Exception:
            pass
    return False


async def _wl_reconnect():
    """Re-establish the Lavalink connection when the pool has no live node. Free
    public nodes drop often (502 / closed), and without this TTS and music can't
    join. Closes any dead nodes so their identifiers are free, then reconnects."""
    if wavelink is None or not (LAVALINK_URI and LAVALINK_PASSWORD):
        return False
    for n in _wl_nodes():
        try:
            await n.close()
        except Exception:
            pass
    try:
        uris = [u.strip() for u in LAVALINK_URI.split(",") if u.strip()]
        pws = [p.strip() for p in LAVALINK_PASSWORD.split(",")]
        nodes = [wavelink.Node(uri=u, password=(pws[i] if i < len(pws) and pws[i] else pws[0]))
                 for i, u in enumerate(uris)]
        await wavelink.Pool.connect(nodes=nodes, client=bot, cache_capacity=100)
        print("[Music] reconnect attempt issued")
        return True
    except Exception as e:
        print(f"[Music] reconnect failed: {e}")
        return False


async def _wl_ensure_ready(timeout=6.0):
    """Make sure a Lavalink node is connected, reconnecting on demand. Returns
    True if one is live within `timeout` seconds."""
    if _wl_has_connected_node():
        return True
    await _wl_reconnect()
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _wl_has_connected_node():
            return True
        await asyncio.sleep(0.4)
    return _wl_has_connected_node()


@tasks.loop(seconds=45)
async def wavelink_watchdog():
    """Keep a Lavalink node alive so /join (TTS) and music stay working even when
    the public node drops out mid-session."""
    if wavelink is None or not (LAVALINK_URI and LAVALINK_PASSWORD):
        return
    if not _wl_has_connected_node():
        print("[Music] watchdog: no connected node — reconnecting")
        await _wl_reconnect()


@wavelink_watchdog.before_loop
async def _wl_watchdog_before():
    await bot.wait_until_ready()


# Chain node connect + DJ clip server into setup_hook without clobbering existing.
_prev_setup_hook = bot.setup_hook


async def _music_setup_hook():
    try:
        if _prev_setup_hook:
            await _prev_setup_hook()
    finally:
        # Lavalink is no longer used — music + TTS both play natively
        # (yt-dlp/edge-tts + FFmpeg through the bot's own voice connection).
        asyncio.create_task(_dj_serve())


bot.setup_hook = _music_setup_hook


@bot.event
async def on_wavelink_node_ready(payload):
    try:
        print(f"[Music] Lavalink node ready: {getattr(payload.node, 'identifier', '?')}")
    except Exception:
        pass
    asyncio.create_task(_probe_music_sources())
    global _music_restore_done
    if not _music_restore_done:
        _music_restore_done = True
        asyncio.create_task(_restore_music_state())


# ---- Source probe + cascade ----
# Different Lavalink nodes have different sources alive (YouTube blocked here,
# SoundCloud there...). Instead of hardcoding one, we PROBE the connected node
# at startup to learn which search prefixes actually return tracks, then every
# search cascades through the working ones in order. `/musicdebug` re-runs the
# probe on demand so problems are diagnosable from inside Discord.
# Preference order (Aug 2026 reality): Deezer is the most reliable from
# datacenter hosts; SoundCloud is spotty (edge blocks + an open HLS-404 bug);
# YouTube only works on nodes whose operator actively maintains it.
_SOURCE_CANDIDATES = ["dzsearch", "scsearch", "ytmsearch", "ytsearch"]
_music_sources = []      # prefixes that returned results on this node, in order
_probe_summary = "not run yet"
_RADIO_TEST_URL = "https://ice1.somafm.com/groovesalad-128-mp3"
_http_source_ok = False


async def _probe_music_sources():
    """Ask the connected node what actually works. Cheap, read-only searches."""
    global _music_sources, _probe_summary, _http_source_ok
    if wavelink is None:
        return
    results = []
    working = []
    for prefix in _SOURCE_CANDIDATES:
        try:
            res = await wavelink.Playable.search(f"{prefix}:counting stars onerepublic", source=None)
            n = len(res.tracks) if isinstance(res, wavelink.Playlist) else len(res or [])
            results.append(f"{prefix}={n}")
            if n > 0:
                working.append(prefix)
        except Exception as e:
            results.append(f"{prefix}=ERR({str(e)[:40]})")
    try:
        r = await wavelink.Playable.search(_RADIO_TEST_URL, source=None)
        _http_source_ok = bool(r)
    except Exception:
        _http_source_ok = False
    results.append(f"http={'ok' if _http_source_ok else 'no'}")
    _music_sources = working
    _probe_summary = " | ".join(results)
    print(f"[MusicProbe] {_probe_summary}")
    print(f"[MusicProbe] usable search sources: {working if working else 'NONE — this node cannot search anything'}")


# ===================== Native music engine (yt-dlp + FFmpeg) =====================
# Music is played NATIVELY — the same architecture as TTS: resolve the audio
# ourselves (yt-dlp), play the stream through the bot's own voice connection
# (FFmpeg -> opus). No Lavalink node in the path, nothing external to break.
# The player mimics the wavelink API surface this file already uses
# (queue.put_wait/get/clear, current, playing, paused, position, play, skip,
# pause, set_volume) and dispatches the same wavelink_track_start/track_end
# events, so the Now Playing cards, DJ mode, and queue logic run unchanged.
try:
    import yt_dlp as _ytdlp
except Exception:
    _ytdlp = None


class NativeTrack:
    def __init__(self, *, title, author="", length=0, uri="", artwork=None,
                 stream_url=None, is_stream=False, user_agent=""):
        self.title = title or "Unknown"
        self.author = author or ""
        self.length = int(length or 0)   # ms
        self.uri = uri or ""
        self.artwork = artwork
        self.stream_url = stream_url
        self.is_stream = bool(is_stream)
        self.user_agent = user_agent or ""
        self.view_count = 0
        self.protocol = ""
        self.local_path = None
        self.identifier = self.uri
        self.source = "native"

    def __repr__(self):
        return f"<NativeTrack {self.title!r}>"


_YTDLP_BASE = {
    "format": "bestaudio[protocol^=http][acodec=opus]/bestaudio[protocol^=http]/bestaudio/best",
    "quiet": True, "no_warnings": True, "noprogress": True,
    "nocheckcertificate": True, "socket_timeout": 15,
    "skip_download": True, "ignoreerrors": True,
    "extractor_args": {"youtube": {"player_client": ["android", "web", "tv"]}},
}

# Optional: YouTube cookies dodge the datacenter bot-check for good. Set
# YTDLP_COOKIES_B64 (base64 of a Netscape cookies.txt export) on the host.
def _ytdlp_cookiefile():
    b64 = os.getenv("YTDLP_COOKIES_B64", "")
    if not b64:
        return None
    path = os.path.join(tempfile.gettempdir(), "yt_cookies.txt")
    if not os.path.exists(path):
        try:
            import base64
            with open(path, "wb") as f:
                f.write(base64.b64decode(b64))
        except Exception as e:
            print(f"[Music] cookie decode failed: {e}")
            return None
    return path


_ck = _ytdlp_cookiefile()
if _ck:
    _YTDLP_BASE["cookiefile"] = _ck
    print("[Music] YouTube cookies loaded")


def _clean_song_query(title, author=""):
    """'Artist - Song [Official Video] | 4K' -> 'Artist - Song' for cross-source
    lookups. Only appends the uploader when the title lacks an artist ("Song"
    from a Topic upload) — lyrics/promo channel names would pollute the search."""
    t = re.sub(r"[\[(][^\])]*[\])]", " ", title or "")
    t = t.split("|")[0]
    t = re.sub(r"\s+", " ", t).strip()
    if " - " in t:
        return t  # already "Artist - Song"
    a = (author or "").strip()
    if a.lower().endswith(" - topic"):
        a = a[: -len(" - Topic")].strip()  # Topic uploader IS the artist
    elif re.search(r"lyric|clouds|records|promo|charts|music\b", a.lower()):
        a = ""  # lyrics/promo channel — not the artist
    return f"{t} {a}".strip()


def _artist_tokens(track):
    a = (track.author or "").lower().replace(" - topic", "").strip()
    if not a and " - " in (track.title or ""):
        a = track.title.lower().split(" - ", 1)[0].strip()
    return [w for w in re.split(r"[^a-z0-9]+", a) if len(w) > 1]


def _sc_match_ok(track, cand):
    """Is this SoundCloud candidate ACTUALLY the same song? Duration must be
    close to the original and most title words must appear — otherwise playing
    it would be a different song entirely."""
    if track.length and cand.length:
        if abs(cand.length - track.length) > max(15000, int(track.length * 0.18)):
            return False
    # An altered version (remix/sped up/live...) is NOT the same song — reject
    # unless the original title itself carries the keyword.
    _ALT = ("remix", "sped up", "spedup", "slowed", "reverb", "8d", "nightcore",
            "bass boost", "mashup", "cover", "live", "instrumental", "karaoke",
            "acoustic", "chipmunk", "pitched", "freestyle")
    tl = (track.title or "").lower()
    cl = (cand.title or "").lower()
    if any(k in cl and k not in tl for k in _ALT):
        return False
    base = _clean_song_query(track.title, track.author).lower()
    words = {w for w in re.split(r"[^a-z0-9]+", base) if len(w) > 2}
    if not words:
        return True
    hay = f"{cand.title} {cand.author or ''}".lower()
    hits = sum(1 for w in words if w in hay)
    return hits >= max(1, int(len(words) * 0.6))


async def _resolve_stream(track):
    """Fill track.stream_url: try its own URL (YouTube), and when that's blocked
    by the bot-check, pull the SAME SONG's audio from SoundCloud instead —
    verified by duration + title match, preferring the artist's own account.
    No trustworthy match -> no stream (skip), never a random other song."""
    res = await _ytdlp_extract(track.uri or f"ytsearch1:{track.title} {track.author}")
    if not res:
        q = _clean_song_query(track.title, track.author)
        print(f"[Music] YouTube stream blocked — trying SoundCloud for {q!r}")
        cands = await _ytdlp_extract(f"scsearch5:{q}")
        good = [c for c in (cands or []) if _sc_match_ok(track, c)]
        if good:
            # The official artist's own upload first, then the best match.
            arts = _artist_tokens(track)
            official = [c for c in good
                        if arts and all(a in (c.author or "").lower() for a in arts)]
            pick = (best_track(official, q) if official else None) or best_track(good, q) or good[0]
            res = [pick]
        elif cands:
            print(f"[Music] no trustworthy SoundCloud match for {q!r} — skipping rather than playing the wrong song")
    if res:
        track.stream_url = res[0].stream_url
        track.user_agent = res[0].user_agent or track.user_agent
        track.protocol = res[0].protocol or track.protocol
        track.artwork = track.artwork or res[0].artwork
        if res[0].length and not track.length:
            track.length = res[0].length
    return bool(track.stream_url)


def _nt_from_info(info):
    if not isinstance(info, dict):
        return None
    t = NativeTrack(
        title=info.get("title"),
        author=info.get("uploader") or info.get("channel") or info.get("artist") or "",
        length=int((info.get("duration") or 0) * 1000),
        uri=info.get("webpage_url") or info.get("original_url") or info.get("url") or "",
        artwork=info.get("thumbnail"),
        stream_url=info.get("url"),
        is_stream=bool(info.get("is_live")),
        user_agent=(info.get("http_headers") or {}).get("User-Agent", ""),
    )
    t.view_count = int(info.get("view_count") or 0)
    t.protocol = str(info.get("protocol") or "")
    return t


def _ytdlp_extract_sync(target, playlist_limit=25):
    opts = dict(_YTDLP_BASE)
    if ":" not in target.split("//", 1)[0] or target.startswith(("http://", "https://")):
        opts["noplaylist"] = False
        opts["playlistend"] = playlist_limit
    with _ytdlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(target, download=False)
    if isinstance(info, dict) and info.get("_type") == "playlist":
        out = []
        for e in (info.get("entries") or []):
            t = _nt_from_info(e)
            if t and t.stream_url:
                out.append(t)
        return out
    t = _nt_from_info(info)
    return [t] if (t and t.stream_url) else []


async def _ytdlp_extract(target, playlist_limit=25):
    if _ytdlp is None:
        return []
    try:
        return await asyncio.to_thread(_ytdlp_extract_sync, target, playlist_limit)
    except Exception as e:
        print(f"[Music] yt-dlp failed for {str(target)[:80]!r}: {e}")
        return []


def _ytdlp_search_flat_sync(target):
    """ONE request for a whole search page (no per-video extraction) — titles,
    uploaders, durations and view counts, ~5x faster than full extraction.
    stream URLs resolve lazily when a track is actually played."""
    opts = dict(_YTDLP_BASE)
    opts["extract_flat"] = True
    with _ytdlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(target, download=False)
    out = []
    for e in ((info or {}).get("entries") or []):
        if not isinstance(e, dict):
            continue
        t = NativeTrack(
            title=e.get("title"),
            author=e.get("uploader") or e.get("channel") or "",
            length=int((e.get("duration") or 0) * 1000),
            uri=e.get("url") or e.get("webpage_url") or "",
            artwork=(e.get("thumbnails") or [{}])[-1].get("url") if e.get("thumbnails") else None,
        )
        t.view_count = int(e.get("view_count") or 0)
        if t.uri:
            out.append(t)
    return out


async def _ytdlp_search_flat(target):
    if _ytdlp is None:
        return []
    try:
        return await asyncio.to_thread(_ytdlp_search_flat_sync, target)
    except Exception as e:
        print(f"[Music] flat search failed for {str(target)[:60]!r}: {e}")
        return []


class NativeQueue:
    def __init__(self):
        self._items = []

    async def put_wait(self, item):
        self._items.append(item)

    def put(self, item):
        self._items.append(item)

    def get(self):
        return self._items.pop(0)

    def clear(self):
        self._items.clear()

    def shuffle(self):
        random.shuffle(self._items)

    def __len__(self):
        return len(self._items)

    def __bool__(self):
        return bool(self._items)

    def __iter__(self):
        return iter(list(self._items))


class _NativePayload:
    """Shim matching the wavelink event payload attrs the handlers read."""
    def __init__(self, player, track, reason=None):
        self.player = player
        self.track = track
        self.original = track
        self.reason = reason


_FFMPEG_BEFORE_STREAM = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -nostdin"

_MUSIC_TMP_DIR = os.path.join(tempfile.gettempdir(), "oversite_music")
_MUSIC_MAX_BYTES = 80 * 1024 * 1024  # refuse absurdly large downloads


async def _music_download(track):
    """Download a track's audio to a temp file (reference-bot style: fetch the
    bytes, play locally). Refreshes an expired stream URL once. Returns the
    local path or None."""
    if track.local_path and os.path.exists(track.local_path):
        return track.local_path
    os.makedirs(_MUSIC_TMP_DIR, exist_ok=True)
    try:
        files = sorted(os.listdir(_MUSIC_TMP_DIR))
        for old_f in files[:-20]:
            os.remove(os.path.join(_MUSIC_TMP_DIR, old_f))
    except Exception:
        pass
    import uuid
    path = os.path.join(_MUSIC_TMP_DIR, f"{uuid.uuid4().hex}.audio")
    for attempt in (1, 2):
        url = track.stream_url
        if not url:
            return None
        headers = {"User-Agent": track.user_agent or "Mozilla/5.0"}
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=120) as client:
                async with client.stream("GET", url, headers=headers) as r:
                    if r.status_code != 200:
                        raise RuntimeError(f"HTTP {r.status_code}")
                    size = 0
                    with open(path, "wb") as f:
                        async for chunk in r.aiter_bytes(1 << 16):
                            size += len(chunk)
                            if size > _MUSIC_MAX_BYTES:
                                raise RuntimeError("too large")
                            f.write(chunk)
            if size > 0:
                with open(path, "rb") as f:
                    head = f.read(16)
                if head.startswith(b"#EXTM3U"):
                    # HLS playlist slipped through — mark it so play() streams it.
                    track.protocol = "m3u8"
                    os.remove(path)
                    return None
                return path
        except Exception as e:
            print(f"[Music] download failed (try {attempt}): {e}")
            if attempt == 1:
                # Stream URL likely expired — re-resolve once and retry.
                track.stream_url = None
                await _resolve_stream(track)
    try:
        os.remove(path)
    except Exception:
        pass
    return None



class NativePlayer(discord.VoiceClient):
    """Wavelink-shaped native player: yt-dlp resolves, FFmpeg streams."""

    def __init__(self, client, channel):
        super().__init__(client, channel)
        self.queue = NativeQueue()
        self.current = None
        self.volume = int(music_config.get("volume") or 100)
        self._gen = 0
        self._started = 0.0
        self._pause_total = 0.0
        self._paused_at = None

    @property
    def playing(self):
        return self.is_playing() or self.is_paused()

    @property
    def paused(self):
        return self.is_paused()

    @property
    def position(self):
        """Track position in ms (what the progress bar reads)."""
        if not self.current:
            return 0
        now = self._paused_at if self._paused_at is not None else time.monotonic()
        return max(0, int((now - self._started - self._pause_total) * 1000))

    async def play(self, track, **_kw):
        if track is None:
            return
        start_ms = int(_kw.get("start") or 0)
        self._gen += 1
        gen = self._gen
        old = self.current
        if self.is_playing() or self.is_paused():
            try:
                discord.VoiceClient.stop(self)
            except Exception:
                pass
            if old is not None:
                bot.dispatch("wavelink_track_end", _NativePayload(self, old, "replaced"))
        # Resolve (or refresh) the stream URL — flat-search tracks resolve here,
        # and prefetched tracks already carry a local file. Falls back to
        # SoundCloud when YouTube bot-checks the host.
        if not track.stream_url and not track.local_path:
            await _resolve_stream(track)
        if not track.stream_url and not track.local_path:
            self.current = None
            bot.dispatch("wavelink_track_end", _NativePayload(self, track, "loadFailed"))
            return
        vol = max(0, min(200, int(self.volume or 100)))
        opts = "-vn" + (f" -filter:a volume={vol / 100:.2f}" if vol != 100 else "")
        local_path = None
        try:
            if track.is_stream or "m3u8" in (track.protocol or "") or "hls" in (track.protocol or ""):
                # Live radio + HLS playlists: stream through ffmpeg directly.
                before = _FFMPEG_BEFORE_STREAM
                if track.user_agent:
                    before += f' -user_agent "{track.user_agent.replace(chr(34), "")}"'
                src = discord.FFmpegOpusAudio(track.stream_url, executable=_ffmpeg_exe(),
                                              before_options=before, options=opts)
            else:
                # Normal track: DOWNLOAD the audio (reference-bot style), then
                # play the local file — the proven-stable path on this host.
                local_path = await _music_download(track)
                if not local_path:
                    self.current = None
                    bot.dispatch("wavelink_track_end", _NativePayload(self, track, "loadFailed"))
                    return
                seek = f"-ss {start_ms / 1000:.3f}" if start_ms > 0 else None
                src = discord.FFmpegOpusAudio(local_path, executable=_ffmpeg_exe(),
                                              before_options=seek, options=opts)
        except Exception as e:
            print(f"[Music] source build failed: {e}")
            self.current = None
            bot.dispatch("wavelink_track_end", _NativePayload(self, track, "loadFailed"))
            return
        self.current = track
        # Position clock accounts for a resumed start offset.
        self._started = time.monotonic() - (start_ms / 1000.0)
        self._pause_total = 0.0
        self._paused_at = None

        def _after(err, g=gen, t=track, lp=local_path):
            if err:
                print(f"[Music] playback error: {err}")
            if lp:
                try:
                    os.remove(lp)
                except Exception:
                    pass
            if g != self._gen:
                return  # replaced by a newer play() — its end event already fired
            def _fire():
                self.current = None
                bot.dispatch("wavelink_track_end", _NativePayload(self, t, "finished"))
            try:
                bot.loop.call_soon_threadsafe(_fire)
            except Exception:
                pass

        discord.VoiceClient.play(self, src, after=_after)
        bot.dispatch("wavelink_track_start", _NativePayload(self, track))
        asyncio.create_task(self._prefetch_next())

    async def _prefetch_next(self):
        """Resolve + download the next queued track while this one plays, so
        skipping starts the next song immediately."""
        try:
            nxt = next(iter(self.queue), None)
            if nxt is None or nxt.is_stream or nxt.local_path:
                return
            if not nxt.stream_url:
                await _resolve_stream(nxt)
            if nxt.stream_url and "m3u8" not in (nxt.protocol or "") and "hls" not in (nxt.protocol or ""):
                nxt.local_path = await _music_download(nxt)
        except Exception as e:
            print(f"[Music] prefetch failed: {e}")

    async def skip(self, force=True):
        # Stopping fires the after-callback -> track_end("finished") -> the
        # existing end handler advances the queue.
        try:
            discord.VoiceClient.stop(self)
        except Exception:
            pass

    async def pause(self, state):
        if state and self.is_playing():
            discord.VoiceClient.pause(self)
            self._paused_at = time.monotonic()
        elif not state and self.is_paused():
            discord.VoiceClient.resume(self)
            if self._paused_at is not None:
                self._pause_total += time.monotonic() - self._paused_at
                self._paused_at = None

    async def set_volume(self, value):
        # Applied from the next track (opus passthrough can't be re-scaled live).
        self.volume = max(0, min(200, int(value)))


async def search_any(query: str, exclude=None):
    """Resolve a query/URL to native tracks via yt-dlp. Returns (results, prefix)."""
    if _ytdlp is None:
        return None, None
    q = (query or "").strip()
    if q.lower().startswith(("http://", "https://")):
        res = await _ytdlp_extract(q)
        return (res or None), "direct"
    for prefix in ("ytsearch", "scsearch"):
        if exclude and prefix in exclude:
            continue
        res = await _ytdlp_search_flat(f"{prefix}8:{q}")
        if res:
            return res, prefix
    return None, None


def _track_source_prefix(track) -> str:
    """Best-effort mapping of a Playable back to its search prefix."""
    u = ((getattr(track, "uri", "") or "") + " " + (getattr(track, "source", "") or "")).lower()
    if "soundcloud" in u:
        return "scsearch"
    if "deezer" in u:
        return "dzsearch"
    if "youtube" in u or "youtu.be" in u:
        return "ytsearch"
    return ""


# ---- Resume after redeploy ----------------------------------------------------
async def _resolve_one_track(uri):
    """Re-resolve a saved track URI back to a playable, best-effort."""
    try:
        res, _ = await search_any(uri)
    except Exception:
        return None
    if not res:
        return None
    if isinstance(res, wavelink.Playlist):
        return res.tracks[0] if res.tracks else None
    if isinstance(res, list):
        return res[0] if res else None
    return res


async def _snapshot_music_state():
    """Current playback for every guild that's actually playing, as plain JSON."""
    guilds = {}
    for guild in bot.guilds:
        vc = guild.voice_client
        if not vc or not getattr(vc, "channel", None) or not getattr(vc, "current", None):
            continue
        track = vc.current
        if _is_dj_clip(track):
            continue  # don't resume mid-DJ-voice-clip
        uri = getattr(track, "uri", None) or getattr(track, "identifier", None)
        if not uri:
            continue
        queue = []
        try:
            for t in list(vc.queue)[:20]:
                tu = getattr(t, "uri", None) or getattr(t, "identifier", None)
                if tu:
                    queue.append(tu)
        except Exception:
            pass
        session = auto_music_sessions.get(guild.id) or {}
        guilds[str(guild.id)] = {
            "voice_channel_id": str(vc.channel.id),
            "text_channel_id": str(play_channels.get(guild.id) or ""),
            "track_uri": uri,
            "track_title": getattr(track, "title", "") or "",
            "position_ms": int(getattr(vc, "position", 0) or 0),
            "paused": bool(getattr(vc, "paused", False)),
            "volume": int(getattr(vc, "volume", 50) or 50),
            "queue": queue,
            "genre": session.get("genre") if session else None,
            "dj_mode": guild.id in _dj_mode,
        }
    return guilds


_music_state_idle = False  # True once we've written the "nothing playing" state


@tasks.loop(seconds=15)
async def persist_music_state():
    """Save live playback so a redeploy can resume it. Skips until the boot-time
    restore has run, so it never overwrites the snapshot with an empty state.
    While playing it writes every tick (to keep the position fresh); when idle it
    writes the empty state just once, not on a loop. Kept infrequent so it doesn't
    add write/vacuum churn to the shared database."""
    global _music_state_idle
    if not _music_state_ready:
        return
    try:
        state = await _snapshot_music_state()
        if not state:
            if _music_state_idle:
                return
            _music_state_idle = True
        else:
            _music_state_idle = False
        await _bot_config_upsert("runtime-music-state", {"guilds": state})
    except Exception as e:
        print(f"[Music] persist state error: {e}")


async def _resume_one_guild(gid, st):
    guild = bot.get_guild(gid)
    if not guild:
        return
    vch = guild.get_channel(int(st.get("voice_channel_id") or 0))
    if not isinstance(vch, (discord.VoiceChannel, discord.StageChannel)):
        return
    track = await _resolve_one_track(st.get("track_uri"))
    if not track:
        print(f"[Music] resume: couldn't re-resolve '{st.get('track_title','?')}' in {gid}")
        return
    vc = await _ensure_wl_player(guild, vch)
    tch_id = st.get("text_channel_id")
    if tch_id:
        try:
            play_channels[gid] = int(tch_id)
        except Exception:
            pass
    # Tell the Now Playing card to render from the resumed position.
    _resume_card_pos[gid] = int(st.get("position_ms") or 0)
    try:
        await vc.set_volume(int(st.get("volume") or 50))
    except Exception:
        pass
    if st.get("genre"):
        auto_music_sessions[gid] = {"genre": st["genre"]}
    if st.get("dj_mode"):
        _dj_mode.add(gid)
    # Restore the queue (best-effort) before playing so /skip has somewhere to go.
    for qu in (st.get("queue") or [])[:20]:
        qt = await _resolve_one_track(qu)
        if qt:
            try:
                await vc.queue.put_wait(qt)
            except Exception:
                pass
    await vc.play(track, start=int(st.get("position_ms") or 0))
    if st.get("paused"):
        try:
            await vc.pause(True)
        except Exception:
            pass
    print(f"[Music] resumed '{st.get('track_title','?')}' in guild {gid} @ {st.get('position_ms')}ms")


async def _restore_music_state():
    """On boot (node ready), rejoin and resume whatever was playing before."""
    global _music_state_ready
    try:
        await asyncio.sleep(1.5)  # brief settle for guild/channel cache
        cfg = await _bot_config_get("runtime-music-state")
        guilds = (cfg or {}).get("guilds") or {}
        for gid, st in guilds.items():
            try:
                await _resume_one_guild(int(gid), st)
            except Exception as e:
                print(f"[Music] resume guild {gid} failed: {e}")
    except Exception as e:
        print(f"[Music] restore error: {e}")
    finally:
        _music_state_ready = True  # persistence may begin now


@bot.tree.command(name="musicdebug", description="Tests which music sources are working right now.")
async def musicdebug_cmd(interaction: discord.Interaction):
    if not _is_admin(interaction.user):
        await interaction.response.send_message(embed=error_embed("No permission", "Admins only."), ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    yt = await _ytdlp_search_flat("ytsearch3:counting stars onerepublic")
    sc = await _ytdlp_search_flat("scsearch3:counting stars onerepublic")
    yt_stream = False
    if yt:
        probe = NativeTrack(title=yt[0].title, author=yt[0].author, uri=yt[0].uri)
        res = await _ytdlp_extract(probe.uri)
        yt_stream = bool(res and res[0].stream_url)
    lines = [
        "**Engine:** native (yt-dlp + FFmpeg — no Lavalink)",
        f"**ffmpeg:** `{_ffmpeg_exe()}`",
        f"**YouTube search:** {len(yt) if yt else 0} result(s)",
        f"**YouTube streams:** {'working' if yt_stream else 'BLOCKED (bot-check) — SoundCloud fallback in use'}",
        f"**SoundCloud search:** {len(sc) if sc else 0} result(s)",
        f"**YouTube cookies:** {'loaded' if _YTDLP_BASE.get('cookiefile') else 'not set (YTDLP_COOKIES_B64)'}",
    ]
    await interaction.followup.send(embed=info_embed("Music Diagnostics", "\n".join(lines)), ephemeral=True)


@bot.tree.command(name="join", description="Joins your voice channel and reads the chat out loud.")
async def join_cmd(interaction: discord.Interaction):
    if not (interaction.user.voice and interaction.user.voice.channel):
        await interaction.response.send_message(
            embed=error_embed("Join a voice channel first", "Hop into a VC, then run `/join`."), ephemeral=True)
        return
    ch = interaction.user.voice.channel
    await interaction.response.defer(ephemeral=True)
    gid = interaction.guild.id
    # TTS plays NATIVELY (no Lavalink) — synthesize locally, play through the
    # bot's own voice connection. Nothing external to be "down".
    vc = interaction.guild.voice_client
    try:
        # A leftover music player (NativePlayer/wavelink) has its own play()
        # semantics — TTS needs a PLAIN VoiceClient, so replace anything else.
        if vc and type(vc) is not discord.VoiceClient:
            try:
                await vc.disconnect(force=True)
            except Exception:
                pass
            vc = None
        if vc and vc.channel and vc.channel.id != ch.id:
            await vc.move_to(ch)
        elif not vc:
            vc = await ch.connect(self_deaf=True)
    except Exception as e:
        await interaction.followup.send(embed=error_embed("Couldn't join", str(e)[:200]), ephemeral=True)
        return
    # Override music: stop playback, drop its queue and any auto/DJ session.
    try:
        vc.stop()
    except Exception:
        pass
    auto_music_sessions.pop(gid, None)
    _dj_mode.discard(gid)
    _cancel_progress(gid)
    _tts_channels[gid] = ch.id
    _tts_queue[gid] = []
    _tts_busy[gid] = False
    _tts_announce.pop(gid, None)
    body = (tts_config.get("join_message") or "").strip()
    if body:
        body = body.replace("{channel}", ch.mention).replace("{user}", interaction.user.mention)
    else:
        body = (f"I'm in {ch.mention}. I'll read its chat aloud — **“name said message”**, and I skip "
                f"the name while the same person keeps talking. Run `/leave` to stop.")
    await interaction.followup.send(embed=success_embed("Joined — TTS on", body), ephemeral=True)


@bot.tree.command(name="leave", description="Leaves the voice channel and stops reading chat.")
async def leave_cmd(interaction: discord.Interaction):
    gid = interaction.guild.id
    was_tts = _tts_channels.pop(gid, None) is not None
    _tts_queue.pop(gid, None)
    _tts_busy.pop(gid, None)
    _tts_announce.pop(gid, None)
    vc = interaction.guild.voice_client
    if not vc:
        await interaction.response.send_message(embed=error_embed("Not connected", "I'm not in a voice channel."), ephemeral=True)
        return
    auto_music_sessions.pop(gid, None)
    _dj_mode.discard(gid)
    try:
        await vc.disconnect()
    except Exception:
        pass
    leave_body = (tts_config.get("leave_message") or "").strip() or "Disconnected from voice."
    await interaction.response.send_message(embed=success_embed("Left", leave_body), ephemeral=True)


_set_group = app_commands.Group(name="set", description="Server settings.")


@_set_group.command(name="nick", description="Sets what the bot calls someone when it reads chat.")
@app_commands.describe(user="Whose name to change. You if you leave it.", nickname="The name to read out. Leave empty to reset it.")
async def set_nick(interaction: discord.Interaction, user: discord.Member = None, nickname: str = ""):
    target = user or interaction.user
    # Anyone may rename themselves; renaming others needs Manage Nicknames.
    if target.id != interaction.user.id and not interaction.user.guild_permissions.manage_nicknames:
        await interaction.response.send_message(
            embed=error_embed("No permission", "You need **Manage Nicknames** to set other people's names."), ephemeral=True)
        return
    new = nickname.strip()
    if len(new) > 100:
        await interaction.response.send_message(embed=error_embed("Too long", "Keep it under 100 characters."), ephemeral=True)
        return
    if "<" in new and ">" in new:
        await interaction.response.send_message(embed=error_embed("No mentions", "Names can't contain mentions or emotes."), ephemeral=True)
        return
    key = f"{interaction.guild.id}:{target.id}"
    if new:
        _tts_nicks[key] = new
        msg = f"The bot will now call {target.mention} **{new}** when reading messages."
    else:
        _tts_nicks.pop(key, None)
        msg = f"Reset {target.mention}'s name back to their normal display name."
    await _save_tts_nicks()
    await interaction.response.send_message(embed=success_embed("Name set", msg), ephemeral=True)


bot.tree.add_command(_set_group)


# ---- Commands ----
@bot.tree.command(name="play", description="Plays a song in your voice channel.")
@app_commands.describe(query="A song name, a link, a genre, or favorites.")
async def music_play(interaction: discord.Interaction, query: str):
    try:
        await interaction.response.defer(ephemeral=True)
    except Exception:
        return
    if _ytdlp is None:
        await interaction.followup.send(embed=error_embed("Music unavailable", "Music isn't configured."))
        return
    # Starting music cleanly exits TTS mode (they're mutually exclusive).
    _tts_channels.pop(interaction.guild.id, None)
    _tts_queue.pop(interaction.guild.id, None)
    _tts_busy.pop(interaction.guild.id, None)
    _tts_announce.pop(interaction.guild.id, None)
    if interaction.guild.voice_client:
        await _tts_reset_speed(interaction.guild.voice_client)  # drop the TTS speed-up
    if not (music_config.get("enabled") or music_available):
        await interaction.followup.send(embed=error_embed("Music is off", "Enable the Music Add-On in the dashboard first."))
        return
    if not interaction.user.voice:
        await interaction.followup.send(embed=error_embed("Not in voice", "Join a voice channel first."))
        return
    if not music_config.get("everyone_can_queue", True) and not is_dj(interaction.user):
        await interaction.followup.send(embed=error_embed("DJ only", "Only a DJ can add songs right now."))
        return
    vc = await _ensure_wl_player(interaction.guild, interaction.user.voice.channel)

    _was_dj = interaction.guild.id in _dj_mode
    if _was_dj:
        _dj_mode.discard(interaction.guild.id)
        _dj_set.pop(interaction.guild.id, None)
        _dj_pending.pop(interaction.guild.id, None)
        try:
            vc.queue.clear()
        except Exception:
            pass

    _q = query.strip().lower()
    play_channels[interaction.guild.id] = interaction.channel.id

    # /play favorites
    if _q in ("favorites", "favourites", "liked", "liked songs", "my favorites"):
        songs = list(liked_songs.get(interaction.user.id, []))
        if not songs:
            await interaction.followup.send(embed=error_embed("No favorites yet", "Heart songs on the Now Playing card first."))
            return
        import random as _r
        _r.shuffle(songs)
        songs = songs[:20]
        queued = 0
        for s in songs:
            try:
                results, _sp = await search_any(s)
                if results and not isinstance(results, wavelink.Playlist):
                    await vc.queue.put_wait(results[0])
                    queued += 1
                    if not vc.playing and vc.queue:
                        await vc.play(vc.queue.get())
            except Exception:
                pass
        await interaction.followup.send(embed=success_embed("Favorites queued", f"Added **{queued}** liked songs."))
        return

    # /play <genre>
    if _q in GENRE_SONGS:
        tracks = await get_genre_playlist_tracks(_q, count=15, guild_id=interaction.guild.id, source="play-genre")
        if not tracks:
            await interaction.followup.send(embed=error_embed("Genre unavailable", f"Couldn't load **{_q}** right now."))
            return
        queued = 0
        for t in tracks:
            try:
                await vc.queue.put_wait(t)
                queued += 1
            except Exception:
                pass
        if not vc.playing and vc.queue:
            await vc.play(vc.queue.get())
        elif _was_dj and vc.playing:
            await vc.skip(force=True)
        await interaction.followup.send(embed=success_embed("Genre mix queued", f"Added **{queued}** hot **{_q}** songs."))
        return

    # Spotify links
    if "spotify.com" in query:
        sp = await spotify_get_tracks(query)
        if not sp:
            await interaction.followup.send(embed=error_embed("Spotify error", "Couldn't read that Spotify link."))
            return
        if len(sp) == 1:
            query = f"{sp[0]['name']} {sp[0]['artist']}"
        else:
            first = None
            for st in sp[:1]:
                yt, _sp = await search_any(f"{st['name']} {st['artist']}")
                if yt and not isinstance(yt, wavelink.Playlist):
                    first = best_track(yt, f"{st['name']} {st['artist']}") or yt[0]
            if not first:
                await interaction.followup.send(embed=error_embed("Not found", "Couldn't find those tracks."))
                return
            was_playing = vc.playing
            await vc.queue.put_wait(first)
            if not was_playing:
                await vc.play(vc.queue.get())
            await interaction.followup.send(embed=success_embed("Spotify loading", f"Loading **{len(sp)}** tracks in the background."))
            async def _queue_rest():
                for st in sp[1:]:
                    try:
                        yt, _sp = await search_any(f"{st['name']} {st['artist']}")
                        if yt and not isinstance(yt, wavelink.Playlist):
                            await vc.queue.put_wait(best_track(yt, f"{st['name']} {st['artist']}") or yt[0])
                        await asyncio.sleep(0.5)
                    except Exception:
                        pass
            asyncio.create_task(_queue_rest())
            return

    tracks, _used_src = await search_any(query)
    if not tracks:
        await interaction.followup.send(embed=error_embed(
            "Not found",
            "Couldn't find that song on any working source. An admin can run `/musicdebug` to see what's wrong."))
        return
    if isinstance(tracks, wavelink.Playlist):
        tracks = tracks.tracks
    # Pick the version people actually play: filter to results that MATCH the
    # query as well as the best textual match (keeps the real artist, drops
    # rips/covers), then take the most played among them.
    top = best_track(tracks, query) or tracks[0]
    try:
        q_words = set(query.lower().split())
        def _overlap(t):
            words = set(t.title.lower().split()) | set((t.author or "").lower().split())
            return len(q_words & words)
        base = _overlap(top)
        peers = [t for t in tracks if _overlap(t) >= max(1, base) and getattr(t, "view_count", 0) > 0]
        # Music videos add intros/skits before the song. Official videos are
        # usually titled plainly, so hunt the EXPLICIT audio version first
        # (Topic uploads / Official Audio / Lyrics / Visualizer), then anything
        # not labeled as a video, then — only if nothing else — a music video.
        _VIDEO_KWS = ("official video", "official music video", "music video",
                      "official hd video", "official 4k", "(video", "[video", "m/v")
        _AUDIO_KWS = ("official audio", "(audio", "[audio", "lyric", "audio)", "visualizer")
        # Versions that change the song — filtered out unless the query asks.
        _ALTERED_KWS = ("bass boost", "sped up", "spedup", "slowed", "reverb", "8d",
                        "nightcore", "daycore", "remix", "mashup", "cover", "live",
                        "instrumental", "karaoke", "acoustic", "loop",
                        "extended", "pitched", "chipmunk")
        _ql = query.lower()
        def _is_altered(t):
            tl = t.title.lower()
            return any(k in tl and k not in _ql for k in _ALTERED_KWS)
        def _is_video(t):
            tl = t.title.lower()
            return any(k in tl for k in _VIDEO_KWS)
        def _is_audio(t):
            tl = t.title.lower()
            au = (t.author or "").lower()
            return au.endswith(" - topic") or any(k in tl for k in _AUDIO_KWS)
        unaltered = [t for t in peers if not _is_altered(t)] or peers
        audio_first = [t for t in unaltered if _is_audio(t)]
        non_video = [t for t in unaltered if not _is_video(t)]
        pool = audio_first or non_video or unaltered
        track = max(pool, key=lambda t: t.view_count) if pool else top
    except Exception:
        track = top
    _adjust_taste(interaction.guild.id, getattr(track, "author", None), 3.0)
    was_playing = vc.playing
    await vc.queue.put_wait(track)
    if not was_playing:
        await vc.play(vc.queue.get())
        await interaction.followup.send(embed=success_embed("Playing", f"**{track.title}**"))
    elif _was_dj:
        await vc.skip(force=True)
        await interaction.followup.send(embed=success_embed("Playing", f"**{track.title}**"))
    else:
        await interaction.followup.send(embed=success_embed("Added to queue", f"**{track.title}**"))


@bot.tree.command(name="skip", description="Skips the current song.")
async def music_skip(interaction: discord.Interaction):
    if not is_dj(interaction.user):
        await interaction.response.send_message(embed=error_embed("No permission", "You need a DJ role to skip."), ephemeral=True)
        return
    vc = get_player(interaction.guild)
    if not vc or not vc.playing:
        await interaction.response.send_message(embed=error_embed("Nothing playing"), ephemeral=True)
        return
    _taste_event_current(interaction.guild, -1.5)
    await vc.skip(force=True)
    await interaction.response.send_message(embed=success_embed("Skipped"), ephemeral=True, delete_after=3)


@bot.tree.command(name="stop", description="Stops the music and leaves the channel.")
async def music_stop(interaction: discord.Interaction):
    if not is_dj(interaction.user):
        await interaction.response.send_message(embed=error_embed("No permission", "You need a DJ role to stop."), ephemeral=True)
        return
    vc = get_player(interaction.guild)
    if not vc:
        await interaction.response.send_message(embed=error_embed("Not connected"), ephemeral=True)
        return
    auto_music_sessions.pop(interaction.guild.id, None)
    _dj_mode.discard(interaction.guild.id)
    _cancel_progress(interaction.guild.id)
    old = now_playing_messages.pop(interaction.guild.id, None)
    if old:
        try:
            await old.delete()
        except Exception:
            pass
    try:
        await set_vc_status(vc.channel, None)
    except Exception:
        pass
    await vc.disconnect()
    await interaction.response.send_message(embed=success_embed("Stopped"))


@bot.tree.command(name="pause", description="Pauses or resumes the music.")
async def music_pause(interaction: discord.Interaction):
    vc = get_player(interaction.guild)
    if not vc:
        await interaction.response.send_message(embed=error_embed("Not connected"), ephemeral=True)
        return
    await vc.pause(not vc.paused)
    await interaction.response.send_message(embed=success_embed("Paused" if vc.paused else "Resumed"), ephemeral=True)


@bot.tree.command(name="resume", description="Resumes the music.")
async def music_resume(interaction: discord.Interaction):
    vc = get_player(interaction.guild)
    if not vc:
        await interaction.response.send_message(embed=error_embed("Not connected"), ephemeral=True)
        return
    if vc.paused:
        await vc.pause(False)
    await interaction.response.send_message(embed=success_embed("Resumed"), ephemeral=True)


@bot.tree.command(name="queue", description="Shows what's coming up next.")
async def music_queue(interaction: discord.Interaction):
    vc = get_player(interaction.guild)
    if not vc:
        await interaction.response.send_message(embed=error_embed("Nothing playing"), ephemeral=True)
        return
    embed = info_embed("🎵 Music Queue")
    if vc.current:
        embed.add_field(name="Now Playing", value=f"**{vc.current.title}**", inline=False)
    if vc.queue:
        embed.add_field(name="Up Next", value="\n".join([f"`{i+1}.` {t.title}" for i, t in enumerate(list(vc.queue)[:10])]), inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="volume", description="Sets the volume from 0 to 100.")
@app_commands.describe(volume="Volume from 0 to 100.")
async def music_volume(interaction: discord.Interaction, volume: app_commands.Range[int, 0, 100]):
    if not is_dj(interaction.user):
        await interaction.response.send_message(embed=error_embed("No permission", "You need a DJ role."), ephemeral=True)
        return
    vc = get_player(interaction.guild)
    if not vc:
        await interaction.response.send_message(embed=error_embed("Not connected"), ephemeral=True)
        return
    await vc.set_volume(int(volume))
    await interaction.response.send_message(embed=success_embed("Volume", f"Set to `{volume}%`"), ephemeral=True)


@bot.tree.command(name="nowplaying", description="Shows what's playing right now.")
async def music_nowplaying(interaction: discord.Interaction):
    vc = get_player(interaction.guild)
    if not vc or not vc.current:
        await interaction.response.send_message(embed=error_embed("Nothing playing"), ephemeral=True)
        return
    await interaction.response.send_message(embed=info_embed("🎵 Now Playing", f"**{vc.current.title}**"), ephemeral=True)


@bot.tree.command(name="favorites", description="Shows the songs you've liked.")
async def favorites_command(interaction: discord.Interaction):
    songs = liked_songs.get(interaction.user.id, [])
    if not songs:
        await interaction.response.send_message(embed=info_embed("Your Liked Songs", "You haven't liked any songs yet. Heart one while it's playing!"), ephemeral=True)
        return
    embed = info_embed("Your Liked Songs", "\n".join([f"`{i+1}.` {s}" for i, s in enumerate(songs[-20:])]))
    embed.set_thumbnail(url=interaction.user.display_avatar.url)
    embed.set_footer(text=f"{len(songs)} song(s) liked")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="setmusic", description="Starts a nonstop radio for a genre.")
@app_commands.describe(genre="A genre, like Country, Hip Hop, Rock, Pop, or All.")
async def setmusic_command(interaction: discord.Interaction, genre: str):
    if not _is_admin(interaction.user):
        await interaction.response.send_message(embed=error_embed("No permission", "Admins only."), ephemeral=True)
        return
    await interaction.response.defer()
    if not interaction.user.voice:
        await interaction.followup.send(embed=error_embed("Not in voice", "Join a voice channel first."))
        return
    vc = await _ensure_wl_player(interaction.guild, interaction.user.voice.channel)
    try:
        vc.queue.clear()
    except Exception:
        pass
    if vc.playing:
        await vc.skip(force=True)
    auto_music_sessions[interaction.guild.id] = {"genre": genre, "channel_id": interaction.channel.id}
    play_channels[interaction.guild.id] = interaction.channel.id
    tracks = await get_genre_playlist_tracks(genre, count=20, guild_id=interaction.guild.id, source="setmusic")
    if not tracks:
        await interaction.followup.send(embed=error_embed("Not found", f"Couldn't load **{genre}**."))
        return
    for t in tracks[1:]:
        await vc.queue.put_wait(t)
    await vc.play(tracks[0])
    await interaction.followup.send(embed=success_embed(f"{genre.title()} Radio Started", f"Now playing **{tracks[0].title}**"))


@bot.tree.command(name="stopmusic", description="Stops the auto radio.")
async def stopmusic_command(interaction: discord.Interaction):
    if not _is_admin(interaction.user):
        await interaction.response.send_message(embed=error_embed("No permission", "Admins only."), ephemeral=True)
        return
    vc = get_player(interaction.guild)
    if not vc:
        await interaction.response.send_message(embed=error_embed("Not connected"), ephemeral=True)
        return
    auto_music_sessions.pop(interaction.guild.id, None)
    _dj_mode.discard(interaction.guild.id)
    try:
        vc.queue.clear()
    except Exception:
        pass
    await vc.disconnect()
    await interaction.response.send_message(embed=success_embed("Stopped"))


# Direct internet-radio streams (SomaFM). These are plain HTTP MP3 streams that
# no IP-reputation wall ever blocks — so /radio ALWAYS has audio, even when every
# search source on the node is down.
_RADIO_STREAMS = [
    (("lofi", "lo-fi", "chill", "study", "ambient", "relax"), "SomaFM Groove Salad", "https://ice1.somafm.com/groovesalad-128-mp3"),
    (("indie",), "SomaFM Indie Pop Rocks", "https://ice1.somafm.com/indiepop-128-mp3"),
    (("metal", "heavy"), "SomaFM Metal Detector", "https://ice1.somafm.com/metal-128-mp3"),
    (("rock", "alt", "alternative", "punk"), "SomaFM BAGeL Radio", "https://ice1.somafm.com/bagel-128-mp3"),
    (("hip", "rap", "trap", "soul", "rnb"), "SomaFM Fluid", "https://ice1.somafm.com/fluid-128-mp3"),
    (("edm", "electronic", "house", "techno", "dance", "dubstep", "bass"), "SomaFM Beat Blender", "https://ice1.somafm.com/beatblender-128-mp3"),
    (("jazz", "lounge"), "SomaFM Sonic Universe", "https://ice1.somafm.com/sonicuniverse-128-mp3"),
    (("country", "americana", "folk", "blues"), "SomaFM Boot Liquor", "https://ice1.somafm.com/bootliquor-128-mp3"),
    (("80s", "eighties", "retro", "synth"), "SomaFM Underground 80s", "https://ice1.somafm.com/u80s-128-mp3"),
    (("70s", "seventies"), "SomaFM Left Coast 70s", "https://ice1.somafm.com/seventies-128-mp3"),
    (("pop", "top", "hits"), "SomaFM PopTron", "https://ice1.somafm.com/poptron-128-mp3"),
]
_RADIO_DEFAULT = ("SomaFM Groove Salad", "https://ice1.somafm.com/groovesalad-128-mp3")


def _radio_stream_for(genre):
    g = (genre or "").lower().strip()
    for keys, name, url in _RADIO_STREAMS:
        if any(k in g for k in keys):
            return name, url
    return _RADIO_DEFAULT


@bot.tree.command(name="radio", description="Starts a 24/7 radio stream in your voice channel.")
@app_commands.describe(genre="A genre, like lofi, rock, country, jazz, edm, or pop.")
async def radio_cmd(interaction: discord.Interaction, genre: str = ""):
    if not is_dj(interaction.user):
        await interaction.response.send_message(embed=error_embed("DJ only", "You need a DJ role (or admin) for the radio."), ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    if not interaction.user.voice:
        await interaction.followup.send(embed=error_embed("Not in voice", "Join a voice channel first."))
        return
    vc = await _ensure_wl_player(interaction.guild, interaction.user.voice.channel)
    station, stream_url = _radio_stream_for(genre or auto_radio_config.get("genre") or "lofi")
    # Direct internet-radio stream, played natively through FFmpeg.
    track = NativeTrack(title=station, uri=stream_url, stream_url=stream_url, is_stream=True)
    auto_music_sessions.pop(interaction.guild.id, None)
    _dj_mode.discard(interaction.guild.id)
    play_channels[interaction.guild.id] = interaction.channel.id
    try:
        vc.queue.clear()
    except Exception:
        pass
    vol = max(1, min(100, int(music_config.get("default_volume") or 50)))
    await vc.play(track, volume=vol)
    await interaction.followup.send(embed=success_embed("Radio on", f"Now streaming **{station}**. Use `/stop` to end it."))


@bot.tree.command(name="votegenre", description="Changes the auto radio genre.")
@app_commands.describe(genre="Genre to switch to")
async def votegenre_command(interaction: discord.Interaction, genre: str):
    if not auto_radio_config.get("allow_vote", True):
        await interaction.response.send_message(embed=error_embed("Voting disabled"), ephemeral=True)
        return
    session = auto_music_sessions.get(interaction.guild.id)
    if not session:
        await interaction.response.send_message(embed=error_embed("No radio", "Auto radio isn't playing."), ephemeral=True)
        return
    vc = interaction.guild.voice_client
    if vc:
        try:
            vc.queue.clear()
        except Exception:
            pass
        auto_music_sessions[interaction.guild.id]["genre"] = genre
        tracks = await get_genre_playlist_tracks(genre, count=15, guild_id=interaction.guild.id, source="votegenre")
        for t in tracks:
            await vc.queue.put_wait(t)
        if vc.playing:
            await vc.skip(force=True)
    await interaction.response.send_message(embed=success_embed("Genre changed", f"Auto radio switched to **{genre}**."))


# ---- Wavelink events ----
_track_failure_counts = {}
_track_last_failure_time = {}
_sc_fallback_counts = {}  # per-guild SoundCloud-fallback attempts (loop guard)


@bot.event
async def on_wavelink_track_start(payload):
    try:
        player = payload.player
        if not player or not player.guild:
            return
        track = payload.track
        guild = player.guild
        if guild.id in _tts_channels:
            return  # TTS mode — no music Now Playing card
        import time as _time
        if _time.time() - _track_last_failure_time.get(guild.id, 0) > 10:
            _track_failure_counts[guild.id] = 0
        _sc_fallback_counts[guild.id] = 0  # a track actually started — clear the loop guard
        _decay_taste(guild.id)

        if _is_dj_clip(track):
            try:
                _pt = _progress_tasks.pop(guild.id, None)
                if _pt:
                    _pt.cancel()
                if guild.voice_client and guild.voice_client.channel:
                    await set_vc_status(guild.voice_client.channel, "DJ Carla")
                    try:
                        _l = getattr(bot, "_last_vc_status_text", {})
                        _l[guild.id] = "DJ Carla"
                        bot._last_vc_status_text = _l
                    except Exception:
                        pass
            except Exception:
                pass
            return

        hist = track_history.setdefault(guild.id, [])
        if not hist or getattr(hist[-1], "identifier", None) != getattr(track, "identifier", None):
            hist.append(track)
            if len(hist) > 15:
                del hist[0]

        if repeat_enabled.get(guild.id, False):
            return

        text_channel = None
        ch_id = play_channels.get(guild.id)
        if ch_id:
            text_channel = guild.get_channel(ch_id)
        if not text_channel and guild.voice_client and guild.voice_client.channel:
            text_channel = guild.voice_client.channel
        if text_channel:
            await send_now_playing(guild, track, text_channel)

        session = auto_music_sessions.get(guild.id)
        if session and len(player.queue) < 3 and guild.id not in _topup_busy:
            _topup_busy.add(guild.id)
            try:
                more = await get_genre_playlist_tracks(session.get("genre", "pop"), count=8, guild_id=guild.id, source="low-queue-topup")
                for t in (more or []):
                    try:
                        if not any(kw in t.title.lower() for kw in KARAOKE_KEYWORDS):
                            await player.queue.put_wait(t)
                    except Exception:
                        pass
            finally:
                _topup_busy.discard(guild.id)
    except Exception as e:
        print(f"[Music] Track start error: {e}")


@bot.event
async def on_wavelink_track_exception(payload):
    try:
        if not payload.player or not payload.player.guild:
            return
        guild = payload.player.guild
        player = payload.player
        track = getattr(payload, "track", None)

        # Resilience: when a source chokes on a track, retry the SAME song on the
        # next WORKING source (per the boot probe). Hard loop guards: the retry
        # must come from a genuinely different source than the one that failed,
        # and attempts are capped per guild until a track successfully starts.
        try:
            failed_prefix = _track_source_prefix(track)
            fb_used = _sc_fallback_counts.get(guild.id, 0)
            if track and fb_used < 2:
                q = f"{getattr(track, 'author', '') or ''} {track.title}".strip()
                alts, used_prefix = await search_any(q, exclude={failed_prefix} if failed_prefix else None)
                if alts and not isinstance(alts, wavelink.Playlist):
                    alt = None
                    for a in alts:
                        if _track_source_prefix(a) != failed_prefix:
                            alt = a
                            break
                    if alt is not None:
                        _sc_fallback_counts[guild.id] = fb_used + 1
                        print(f"[Music] {failed_prefix or 'source'} failed — fallback via {used_prefix}: {alt.title}")
                        await player.play(alt)
                        return  # recovered on a different source
        except Exception as fe:
            print(f"[Music] source fallback error: {fe}")

        import time as _t
        _track_last_failure_time[guild.id] = _t.time()
        _track_failure_counts[guild.id] = _track_failure_counts.get(guild.id, 0) + 1
        print(f"[Music] Track exception in {guild.name} — #{_track_failure_counts[guild.id]}: {getattr(payload, 'exception', '')}")
        if _track_failure_counts[guild.id] >= 5:
            player = payload.player
            try:
                player.queue.clear()
            except Exception:
                pass
            auto_music_sessions.pop(guild.id, None)
            _track_failure_counts[guild.id] = 0
    except Exception as e:
        print(f"[Music] TrackException handler error: {e}")


@bot.event
async def on_wavelink_track_end(payload):
    try:
        if str(getattr(payload, "reason", "")).lower() == "replaced":
            return
        if not payload.player or not payload.player.guild:
            return
        guild = payload.player.guild
        player = payload.player

        # TTS mode owns the player — advance the spoken queue, skip music logic.
        if guild.id in _tts_channels:
            await _tts_play_next(player)
            return

        if repeat_enabled.get(guild.id, False) and payload.track:
            try:
                repeat_enabled[guild.id] = False
                await player.play(payload.track)
            except Exception as re:
                print(f"[Music] Repeat error: {re}")
            return

        # DJ clip finished -> play the set it introduced
        if _is_dj_clip(getattr(payload, "track", None)):
            real = _dj_pending.pop(guild.id, None)
            _prev = _dj_prev_volume.pop(guild.id, None)
            if _prev is not None:
                try:
                    await player.set_volume(_prev)
                except Exception:
                    pass
            if real and "fetch" in real:
                try:
                    tracks = await real["fetch"]
                except Exception:
                    tracks = []
                if tracks:
                    for t in tracks[1:]:
                        await player.queue.put_wait(t)
                    await player.play(tracks[0])
                elif player.queue:
                    await player.play(player.queue.get())
                return
            if player.queue:
                await player.play(player.queue.get())
            return

        if player.queue:
            await player.play(player.queue.get())
            return

        # Empty queue: DJ mode rolls into next set
        if guild.id in _dj_mode and DJ_PUBLIC_URL:
            asyncio.create_task(_dj_start_set(guild))
            return

        # Empty queue: refill genre radio
        session = auto_music_sessions.get(guild.id)
        if session:
            tracks = await get_genre_playlist_tracks(session.get("genre", "pop"), count=30, guild_id=guild.id, source="empty-queue-reload")
            for t in tracks:
                await player.queue.put_wait(t)
            if player.queue:
                await player.play(player.queue.get())
            return

        # Truly empty: stop the progress updates + clear the VC status, but KEEP
        # the Now Playing card in place — it stays until the next song's card
        # replaces it (posting a new card deletes the old one).
        _cancel_progress(guild.id)
        if guild.voice_client and guild.voice_client.channel:
            try:
                await set_vc_status(guild.voice_client.channel, None)
            except Exception:
                pass
    except Exception as e:
        print(f"[Music] Track end error: {e}")


@bot.event
async def on_voice_state_update(member, before, after):
    """Auto-leave + cleanup when the bot is alone or gets disconnected."""
    try:
        if member.bot and member.id == bot.user.id and before.channel and not after.channel:
            # A quick reconnect (or a redeploy's phantom leave) can fire this right
            # as a new session starts. Wait a beat and bail if the bot is already
            # back in voice, so we don't delete the fresh card / clear its status.
            await asyncio.sleep(1.5)
            if member.guild.voice_client:
                return
            _cancel_progress(member.guild.id)
            try:
                await set_vc_status(before.channel, None)
            except Exception:
                pass
            old = now_playing_messages.pop(member.guild.id, None)
            if old:
                try:
                    await old.delete()
                except Exception:
                    pass
            # Clear the status-throttle timestamp + sweep guard so the NEXT /play
            # always re-shows the card and re-sets the channel status.
            try:
                getattr(bot, "_last_vc_status", {}).pop(member.guild.id, None)
            except Exception:
                pass
            _np_swept_channels.discard(before.channel.id)
            auto_music_sessions.pop(member.guild.id, None)
            _dj_mode.discard(member.guild.id)
            _dj_set.pop(member.guild.id, None)
            _dj_pending.pop(member.guild.id, None)
            # Clear TTS state too, so a re-join starts fresh.
            _tts_channels.pop(member.guild.id, None)
            _tts_queue.pop(member.guild.id, None)
            _tts_busy.pop(member.guild.id, None)
            _tts_announce.pop(member.guild.id, None)
            return
        # Auto-leave an empty channel even while just doing TTS (no music session).
        if member.bot or not (music_config.get("auto_leave", True) or member.guild.id in _tts_channels):
            return
        guild = member.guild
        vc = guild.voice_client if guild else None
        if not (vc and vc.channel):
            return
        if not [m for m in vc.channel.members if not m.bot]:
            if member.guild.id in auto_music_sessions and auto_radio_config.get("auto_start"):
                return
            try:
                await set_vc_status(vc.channel, None)
            except Exception:
                pass
            try:
                await vc.disconnect()
            except Exception:
                pass
    except Exception as e:
        print(f"[Music] voice-state hook error: {e}")


async def apply_music_config(config: dict):
    """Re-render the Now Playing card when the dashboard toggles the UI style."""
    for guild in bot.guilds:
        try:
            vc = guild.voice_client
            if not vc or not getattr(vc, "current", None):
                continue
            old_msg = now_playing_messages.get(guild.id)
            if old_msg is not None:
                try:
                    if (discord.utils.utcnow() - old_msg.created_at).total_seconds() < 30:
                        continue
                except Exception:
                    pass
            channel = getattr(old_msg, "channel", None)
            if channel is None:
                ch_id = play_channels.get(guild.id)
                channel = guild.get_channel(ch_id) if ch_id else None
            if channel is None and vc.channel:
                channel = vc.channel
            if channel:
                await send_now_playing(guild, vc.current, channel)
        except Exception as e:
            print(f"[Music] UI re-render error in {guild.name}: {e}")


async def apply_bot_identity():
    if not (SUPABASE_URL and BOT_ORDER_ID):
        return
    try:
        async with _http() as client:
            r = await client.get(
                f"{SUPABASE_URL}/rest/v1/bot_orders?id=eq.{BOT_ORDER_ID}&select=bot_name",
                headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}, timeout=10,
            )
            data = r.json()
            if not data or not isinstance(data, list) or not data[0].get("bot_name"):
                return
            target = data[0]["bot_name"]
    except Exception as e:
        print(f"[Identity] fetch failed: {e}")
        return
    if bot.user and bot.user.name == target:
        return
    try:
        await bot.user.edit(username=target)
        print(f"[Identity] username set to {target}")
    except discord.HTTPException as e:
        print(f"[Identity] failed: {getattr(e, 'status', '')}")
    except Exception as e:
        print(f"[Identity] error: {e}")


_last_bio = None
_about_me_diag = False
# Consecutive fetch-failure streak, so a Supabase blip doesn't print a line
# every 20s tick for its whole duration (it once spammed 26 lines in 25 min).
_am_fails = 0


async def apply_about_me():
    """Push the dashboard's About Me to Discord as the application description
    via PATCH /applications/@me (authorised with the bot's own token). Discord
    supports this now, so there's no manual portal step. Only re-sends when the
    text actually changes."""
    global _last_bio, _about_me_diag, _am_fails
    if not (SUPABASE_URL and BOT_ORDER_ID and TOKEN):
        if not _about_me_diag:
            print(f"[AboutMe] skipped — SUPABASE_URL:{bool(SUPABASE_URL)} BOT_ORDER_ID:{bool(BOT_ORDER_ID)} TOKEN:{bool(TOKEN)}")
            _about_me_diag = True
        return
    try:
        async with _http() as client:
            r = await client.get(
                f"{SUPABASE_URL}/rest/v1/bot_orders?id=eq.{BOT_ORDER_ID}&select=bot_bio",
                headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}, timeout=10,
            )
            status = r.status_code
            data = r.json()
        if not isinstance(data, list) or not data:
            if not _about_me_diag:
                print(f"[AboutMe] fetch returned no row — HTTP {status} body={str(data)[:200]}")
                _about_me_diag = True
            return
        bio = data[0].get("bot_bio")
        # One-time diagnostic so we can see exactly what the bot reads.
        if not _about_me_diag:
            print(f"[AboutMe] fetch OK — HTTP {status}, bot_bio={'<empty>' if not bio else repr(bio[:60])}")
            _about_me_diag = True
    except Exception as e:
        # Name the exception type (some, like read timeouts, stringify empty)
        # and only log the first failure of a streak + every 15th after, so an
        # upstream blip is one line instead of a line every 20 seconds.
        _am_fails += 1
        if _am_fails == 1 or _am_fails % 15 == 0:
            print(f"[AboutMe] fetch failed ({_am_fails}x): {type(e).__name__}: {e}")
        return
    if _am_fails:
        print(f"[AboutMe] fetch recovered after {_am_fails} failure(s)")
        _am_fails = 0
    if bio is None or bio == "" or bio == _last_bio:
        return
    try:
        async with _http() as client:
            resp = await client.patch(
                "https://discord.com/api/v10/applications/@me",
                headers={"Authorization": f"Bot {TOKEN}", "Content-Type": "application/json"},
                json={"description": str(bio or "")[:400]},
                timeout=10,
            )
        if resp.status_code in (200, 201):
            # Only mark as applied AFTER Discord accepts it, so a failed attempt
            # retries on the next loop instead of being marked done.
            _last_bio = bio
            print("[AboutMe] application description updated")
        else:
            print(f"[AboutMe] update failed: HTTP {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        print(f"[AboutMe] update error: {e}")


@tasks.loop(hours=2)
async def sync_identity():
    await apply_bot_identity()
    await apply_about_me()


@sync_identity.before_loop
async def before_sync_identity():
    await bot.wait_until_ready()


async def _shutdown():
    print("[Shutdown] shutting down")
    # Music FIRST: snapshot the exact playback position before anything else —
    # if the host kills us mid-shutdown, the resume still lands on the right
    # second instead of an up-to-15s-stale loop snapshot.
    if _music_state_ready:
        try:
            state = await _snapshot_music_state()
            await asyncio.wait_for(_bot_config_upsert("runtime-music-state", {"guilds": state}), timeout=6)
            print(f"[Shutdown] music state saved ({len(state)} guild(s)) at exact position")
        except Exception as e:
            print(f"[Shutdown] music flush error: {e}")
    # Flush every active giveaway (entrants + state) BEFORE we exit, so a redeploy
    # never drops anyone — the boot restore puts them all back.
    try:
        pending = list(active_giveaways.items())
        if pending:
            await asyncio.wait_for(
                asyncio.gather(*[_gw_save_state(gid, g) for gid, g in pending], return_exceptions=True),
                timeout=8,
            )
            print(f"[Shutdown] flushed {len(pending)} giveaway(s) to storage")
    except Exception as e:
        print(f"[Shutdown] giveaway flush error: {e}")
    # Flush ad inventory + queue + pending + invite tracker so a redeploy never
    # forgets what people bought or what's waiting to post. Skip if the boot read
    # failed — flushing the empty in-memory copy would wipe the stored inventory.
    if _ads_loaded:
        try:
            n = _ads_inventory_count()
            ok = await asyncio.wait_for(_ads_flush_now(), timeout=14)
            print(f"[Shutdown] ad snapshot saved: {ok} ({n} member inventories) — will be restored on next boot")
        except Exception as e:
            print(f"[Shutdown] ads flush error: {e}")
    else:
        print("[Shutdown] skipped ad flush — data never loaded this session (inventory preserved)")
    try:
        await asyncio.wait_for(_bot_config_upsert("invite-tracker-data", {"guilds": invite_tracker}), timeout=8)
    except Exception as e:
        print(f"[Shutdown] invite flush error: {e}")
    if _econ_loaded:
        try:
            await asyncio.wait_for(_econ_flush_now(), timeout=8)
            print("[Shutdown] economy balances saved")
        except Exception as e:
            print(f"[Shutdown] economy flush error: {e}")
    if SUPABASE_URL and BOT_ORDER_ID:
        try:
            async with _http() as client:
                await client.patch(
                    f"{SUPABASE_URL}/rest/v1/bot_runtime_status?bot_id=eq.{BOT_ORDER_ID}",
                    headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json", "Prefer": "return=minimal"},
                    json={"status": "offline"}, timeout=5,
                )
        except Exception:
            pass
    try:
        await bot.change_presence(status=discord.Status.invisible)
    except Exception:
        pass
    for loop in (send_heartbeat, poll_configs, record_metrics_loop, poll_roblox_apply,
                 poll_about_me, econ_autosave, ticket_inactivity_tick):
        try:
            loop.cancel()
        except Exception:
            pass
    await bot.close()


def handle_sigterm(sig, frame):
    print(f"[Shutdown] signal {sig}")
    asyncio.create_task(_shutdown())


signal.signal(signal.SIGTERM, handle_sigterm)
signal.signal(signal.SIGINT, handle_sigterm)


async def claim_shutdown_command():
    if not (SUPABASE_URL and BOT_ORDER_ID):
        return None
    try:
        url = (
            f"{SUPABASE_URL}/rest/v1/bot_commands?bot_id=eq.{BOT_ORDER_ID}&action=eq.shutdown"
            f"&status=eq.pending&created_at=gte.{BOT_START_TIME}&order=created_at.desc&select=id&limit=1"
        )
        async with _http() as client:
            r = await client.get(url, headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}, timeout=15)
            data = r.json()
            if data and isinstance(data, list):
                return data[0]
    except httpx.TransportError:
        pass  # transient network blip (timeout/connection) — retried next cycle
    except Exception as e:
        print(f"[Shutdown] claim error: {e!r}")
    return None


@tasks.loop(seconds=3)
async def poll_shutdown():
    cmd = await claim_shutdown_command()
    if cmd:
        print("[Shutdown] command received")
        await _shutdown()


@poll_shutdown.before_loop
async def before_poll_shutdown():
    await bot.wait_until_ready()


def _run():
    # uvloop: drop-in libuv event loop, measurably faster for IO-heavy bots.
    # Guarded — if it's ever missing or broken we run on stock asyncio.
    try:
        import uvloop
        uvloop.install()
        print("[Boot] uvloop event loop active")
    except Exception:
        pass
    # Cap discord.py's gateway reconnect backoff. On repeated gateway 503s the
    # default exponential backoff climbs to 13-16 minutes between retries, which
    # leaves the bot offline long after Discord has recovered (observed in prod).
    # Cap each reconnect wait at 60s so a transient gateway hiccup self-heals
    # within a minute instead of a quarter hour. Discord rate limits (429) are
    # handled separately, so this never hammers the API.
    try:
        import discord.backoff as _dbackoff
        _orig_delay = _dbackoff.ExponentialBackoff.delay

        def _capped_delay(self, _orig=_orig_delay):
            try:
                return min(float(_orig(self)), 60.0)
            except Exception:
                return 60.0

        _dbackoff.ExponentialBackoff.delay = _capped_delay
        print("[Boot] reconnect backoff capped at 60s")
    except Exception as _e:
        print(f"[Boot] backoff cap not applied: {_e}")
    try:
        bot.run(TOKEN)
    except discord.errors.HTTPException as e:
        if getattr(e, "status", None) == 429:
            import time
            import sys
            print("[Boot] rate-limit ban — sleeping 15 minutes")
            time.sleep(900)
            os.execv(sys.executable, [sys.executable] + sys.argv)
        raise


if __name__ == "__main__":
    _run()
