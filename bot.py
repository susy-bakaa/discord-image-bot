import os, re, json
import random
import asyncio
import hashlib, zipfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from discord.ext import tasks

# ----- Config -----
TOKEN = os.getenv("DISCORD_TOKEN")
IMAGES_DIR = Path(os.getenv("IMAGES_DIR", "images"))
DAILY_DB = Path(os.getenv("DAILY_DB", "daily.json"))
IMAGES_DB = Path(os.getenv("IMAGES_DB", "images_db.json"))
USAGE_DB  = Path(os.getenv("USAGE_DB",  "usage.json"))

# Export config (set these in your env/service)
EXPORT_DIR = Path(os.getenv("EXPORT_DIR", ""))
EXPORT_BASE_URL = os.getenv("EXPORT_BASE_URL", "")
EXPORT_RETENTION_DAYS = int(os.getenv("EXPORT_RETENTION_DAYS", "7"))

# Back-compat: accept either single GUILD_ID or multi GUILD_IDS
_single = os.getenv("GUILD_ID", "").strip()
_multi  = os.getenv("GUILD_IDS", "").strip()

_allowed = []
if _single:
    _allowed.append(int(_single))
if _multi:
    _allowed.extend(int(x) for x in re.split(r"[,\s]+", _multi) if x)

ALLOWED_GUILD_IDS: set[int] = set(_allowed)
MY_GUILDS = [discord.Object(id=g) for g in sorted(ALLOWED_GUILD_IDS)]

ADMIN_USER_IDS = {int(x) for x in os.getenv("ADMIN_USER_IDS", "").replace(" ", "").split(",") if x}
CONFIG_GUILD_ID = int(os.getenv("CONFIG_GUILD_ID", "0"))
CONFIG_GUILD = discord.Object(id=CONFIG_GUILD_ID) if CONFIG_GUILD_ID else None

SYNCED = False
MAX_DAILY_RANDOM = int(os.getenv("MAX_DAILY_RANDOM", "3"))
RARITIES = ["Common", "Uncommon", "Rare", "Mythical", "Exquisite"]
ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp4", ".webm", ".mov"}

# Reset rolls at midnight UTC by default (consistent for everyone)
def today_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

# ----- Helpers -----
def load_images() -> list[Path]:
    if not IMAGES_DIR.exists():
        raise RuntimeError(f"Images dir not found: {IMAGES_DIR.resolve()}")
    imgs = sorted(p for p in IMAGES_DIR.rglob("*") if p.suffix.lower() in ALLOWED_EXT)
    if not imgs:
        raise RuntimeError(f"No images found in {IMAGES_DIR.resolve()}")
    return imgs

def load_daily_db() -> dict:
    if DAILY_DB.exists():
        try:
            return json.loads(DAILY_DB.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}

def _load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return default

def _save_json(path: Path, obj):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=2), encoding="utf-8")
    tmp.replace(path)

def save_daily_db(db: dict) -> None:
    tmp = DAILY_DB.with_suffix(".tmp")
    tmp.write_text(json.dumps(db, indent=2), encoding="utf-8")
    tmp.replace(DAILY_DB)

def _sha256_bytes(b: bytes) -> str:
    h = hashlib.sha256()
    h.update(b)
    return h.hexdigest()

def pick_or_get_today(images: Optional[list[Path]] = None) -> Path:
    images = images or list_pool_images()
    if not images:
        raise RuntimeError("No available (non-blacklisted) images.")
    db = load_daily_db()
    key = today_key()
    p = db.get(key)
    if p and Path(p).exists() and not get_meta(Path(p)).get("blacklisted", False):
        return Path(p)
    choice = random.SystemRandom().choice(images)
    db[key] = str(choice.resolve())
    save_daily_db(db)
    return choice

async def send_image(interaction: discord.Interaction, path: Path, title: str):
    try:
        await interaction.response.defer(thinking=False)
    except discord.InteractionResponded:
        pass
    file = discord.File(str(path), filename=path.name)
    await interaction.followup.send(content=title, file=file)

# --- Image metadata
def _images_db():
    db = _load_json(IMAGES_DB, {"images": {}, "hashes": {}})  # <-- ensure hashes index exists
    if "images" not in db: db["images"] = {}
    if "hashes" not in db: db["hashes"] = {}
    return db

def get_meta(p: Path) -> dict:
    db = _images_db()
    key = str(p.resolve())
    rec = db["images"].get(key)
    if rec is None:
        rec = {"rarity": "Common", "blacklisted": False}
        db["images"][key] = rec
        _save_json(IMAGES_DB, db)
    return rec

def set_meta(p: Path, *, rarity: Optional[str] = None, blacklisted: Optional[bool] = None, sha256: Optional[str] = None) -> dict:
    db = _images_db()
    key = str(p.resolve())
    rec = db["images"].get(key) or {"rarity": "Common", "blacklisted": False}
    if rarity is not None:
        if rarity not in RARITIES:
            raise ValueError(f"Invalid rarity: {rarity}")
        rec["rarity"] = rarity
    if blacklisted is not None:
        rec["blacklisted"] = bool(blacklisted)
    if sha256 is not None:
        rec["sha256"] = sha256
        db["hashes"][sha256] = key  # index for duplicate detection
    db["images"][key] = rec
    _save_json(IMAGES_DB, db)
    return rec

def list_all_images() -> list[Path]:
    return sorted(p for p in IMAGES_DIR.rglob("*") if p.suffix.lower() in ALLOWED_EXT)

def list_pool_images() -> list[Path]:
    # not blacklisted and exists
    imgs = []
    for p in list_all_images():
        if not p.exists(): continue
        if get_meta(p).get("blacklisted"): continue
        imgs.append(p)
    return imgs

# --- User use restrictions
def _usage_db():
    return _load_json(USAGE_DB, {})  # { "YYYY-MM-DD": { "user_id": int } }

def get_user_uses(user_id: int) -> int:
    db = _usage_db()
    return int(db.get(today_key(), {}).get(str(user_id), 0))

def inc_user_uses(user_id: int) -> int:
    db = _usage_db()
    day = today_key()
    daymap = db.setdefault(day, {})
    new_count = int(daymap.get(str(user_id), 0)) + 1
    daymap[str(user_id)] = new_count
    # prune old days (keep last 7)
    if len(db) > 7:
        for k in sorted(db.keys())[:-7]:
            db.pop(k, None)
    _save_json(USAGE_DB, db)
    return new_count

# --- Presence rotation -------------------------------------------------
def _presence_variants():
    # try to get image count; fall back to "?" if images missing
    try:
        img_count = len(load_images())
    except Exception:
        img_count = "?"

    return [
        discord.Activity(type=discord.ActivityType.watching,   name=f"{img_count} pictures"),
        discord.Activity(type=discord.ActivityType.listening,  name="/daily and /random"),
        discord.Activity(type=discord.ActivityType.playing,    name="Daily reset at 0.00 UTC"),
    ]
    
@tasks.loop(minutes=2)
async def rotate_presence():
    variants = _presence_variants()
    idx = getattr(rotate_presence, "idx", 0)
    await bot.change_presence(status=discord.Status.online, activity=variants[idx % len(variants)])
    rotate_presence.idx = idx + 1
# -----------------------------------------------------------------------

# ----- Bot setup -----
intents = discord.Intents.default()  # slash cmds don't need message-content intent
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    # Leave any non-whitelisted guilds
    if ALLOWED_GUILD_IDS:
        for g in list(bot.guilds):
            if g.id not in ALLOWED_GUILD_IDS:
                print(f"Leaving unauthorized guild: {g.name} ({g.id})")
                await g.leave()

    # Start rotating statuses
    if not rotate_presence.is_running():
        await bot.change_presence(status=discord.Status.online, activity=_presence_variants()[0])
    rotate_presence.start()

    # Start export purging task
    if not purge_exports.is_running():
        purge_exports.start()

    # Sync commands
    global SYNCED
    if not SYNCED:
        if MY_GUILDS:
            for gobj in MY_GUILDS:
                # IMPORTANT:
                # - For config guild: DO NOT clear. Copy globals + sync (keeps dev cmds).
                # - For other guilds: copy globals + sync (as before).
                if CONFIG_GUILD and gobj.id == CONFIG_GUILD.id:
                    bot.tree.copy_global_to(guild=gobj)
                    cmds = await bot.tree.sync(guild=gobj)
                else:
                    bot.tree.copy_global_to(guild=gobj)
                    cmds = await bot.tree.sync(guild=gobj)
                print(f"Synced {len(cmds)} cmds to guild {gobj.id}: {[c.name for c in cmds]}")
            print(f"Synced to {len(MY_GUILDS)} guild(s).")
        else:
            cmds = await bot.tree.sync()
            print(f"Synced {len(cmds)} global cmds: {[c.name for c in cmds]}")
        SYNCED = True

    print(f"Logged in as {bot.user} (ID: {bot.user.id})")

@bot.event
async def on_guild_join(guild: discord.Guild):
    if ALLOWED_GUILD_IDS and guild.id not in ALLOWED_GUILD_IDS:
        print(f"Leaving unauthorized guild on join: {guild.name} ({guild.id})")
        await guild.leave()
        return
    # If it is allowed, make sure commands are synced there too
    gobj = discord.Object(id=guild.id)
    # For config guild: DO NOT clear. Copy globals + sync so it has both standard and dev cmds.
    if CONFIG_GUILD and guild.id == CONFIG_GUILD.id:
        bot.tree.copy_global_to(guild=gobj)  # <-- added
        cmds = await bot.tree.sync(guild=gobj)
    else:
        bot.tree.copy_global_to(guild=gobj)
        cmds = await bot.tree.sync(guild=gobj)
    print(f"Synced {len(cmds)} cmds to newly allowed guild {guild.id}")

@tasks.loop(hours=1)
async def purge_exports():
    try:
        if not EXPORT_DIR or not EXPORT_DIR.exists():
            return
        cutoff = datetime.now(timezone.utc) - timedelta(days=EXPORT_RETENTION_DAYS)
        for p in EXPORT_DIR.glob("export-*.zip"):
            mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
            if mtime < cutoff:
                try:
                    p.unlink()
                    print(f"purge_exports: deleted old {p.name}")
                except Exception as e:
                    print(f"purge_exports: failed to delete {p}: {e}")
    except Exception as e:
        print(f"purge_exports error: {e}")

# --- Public commands @app_commands.guilds(*MY_GUILDS)
@bot.tree.command(name="daily", description="Send today's picture (same for everyone).")
async def daily_cmd(interaction: discord.Interaction):
    try:
        path = pick_or_get_today(list_pool_images())
        r = get_meta(path)["rarity"]
        await send_image(interaction, path, f"📅 Today's picture ({today_key()} UTC)\n✨ Rarity: **{r}**")
    except Exception as e:
        await interaction.response.send_message(f"Error: {e}", ephemeral=True)

@bot.tree.command(name="random", description="Send a random picture from the set.")
async def random_cmd(interaction: discord.Interaction):
    try:
        used = get_user_uses(interaction.user.id)
        if used >= MAX_DAILY_RANDOM:
            await interaction.response.send_message(
                f"❌ You have used all of your {MAX_DAILY_RANDOM} pulls today.\n🕓 Daily reset is at 00:00 UTC.",
                ephemeral=True
            )
            return

        images = list_pool_images()
        if not images:
            await interaction.response.send_message("No available images.", ephemeral=True)
            return

        path = random.SystemRandom().choice(images)
        meta = get_meta(path)
        count = inc_user_uses(interaction.user.id)
        left = max(0, MAX_DAILY_RANDOM - count)
        title = f"🎲 Random picture\n**{meta['rarity']} Pull** • {left}/{MAX_DAILY_RANDOM} pulls left today"
        await send_image(interaction, path, title)
    except Exception as e:
        await interaction.response.send_message(f"Error: {e}", ephemeral=True)

# --- Developer commands
def _is_admin(inter: discord.Interaction) -> bool:
    if ADMIN_USER_IDS and inter.user.id not in ADMIN_USER_IDS:
        return False
    if CONFIG_GUILD_ID and inter.guild_id != CONFIG_GUILD_ID:
        return False
    return True

ADMIN_CURRENT: dict[int, Path] = {}

@bot.tree.command(name="cfg_next", description="Show next image to configure.", guild=CONFIG_GUILD)
async def cfg_next(interaction: discord.Interaction):
    if not _is_admin(interaction):
        await interaction.response.send_message("Not allowed.", ephemeral=True)
        return
    imgs = list_all_images()
    if not imgs:
        await interaction.response.send_message("No images.", ephemeral=True)
        return
    # simple per-admin round-robin
    idx = getattr(cfg_next, "idx", 0)
    p = imgs[idx % len(imgs)]
    cfg_next.idx = idx + 1
    ADMIN_CURRENT[interaction.user.id] = p
    m = get_meta(p)
    await send_image(
        interaction,
        p,
        f"Config: **{p.name}**\nrarity: **{m['rarity']}** • blacklisted: **{m['blacklisted']}**"
    )
    
def _all_names():
    return [p.name for p in list_all_images()]

async def _ac_names(interaction: discord.Interaction, current: str):
    names = _all_names()
    q = current.lower().strip()
    if q:
        names = [n for n in names if q in n.lower()]
    return [app_commands.Choice(name=n, value=n) for n in names[:25]]
    
@bot.tree.command(name="cfg_select", description="Select an image by filename to configure.", guild=CONFIG_GUILD)
@app_commands.autocomplete(name=_ac_names)
async def cfg_select(interaction: discord.Interaction, name: str):
    if not _is_admin(interaction):
        await interaction.response.send_message("Not allowed.", ephemeral=True); return

    # pick the first exact (case-insensitive), else first contains
    imgs = list_all_images()
    exact = [p for p in imgs if p.name.lower() == name.lower()]
    cand = exact[0] if exact else next((p for p in imgs if name.lower() in p.name.lower()), None)

    if not cand:
        await interaction.response.send_message("No match.", ephemeral=True); return

    ADMIN_CURRENT[interaction.user.id] = cand
    m = get_meta(cand)
    await send_image(
        interaction,
        cand,
        f"Config: **{cand.name}**\nrarity: **{m['rarity']}** • blacklisted: **{m['blacklisted']}**"
    )

@bot.tree.command(name="cfg_set_rarity", description="Set rarity for the current image.", guild=CONFIG_GUILD)
@app_commands.choices(rarity=[app_commands.Choice(name=r, value=r) for r in RARITIES])
async def cfg_set_rarity(interaction: discord.Interaction, rarity: app_commands.Choice[str]):
    if not _is_admin(interaction):
        await interaction.response.send_message("Not allowed.", ephemeral=True)
        return
    p = ADMIN_CURRENT.get(interaction.user.id)
    if not p:
        await interaction.response.send_message("Use /cfg_next first.", ephemeral=True)
        return
    set_meta(p, rarity=rarity.value)
    await interaction.response.send_message(f"✅ Set **{p.name}** → **{rarity.value}**", ephemeral=True)

@bot.tree.command(name="cfg_toggle_blacklist", description="Toggle blacklist for the current image.", guild=CONFIG_GUILD)
async def cfg_toggle_blacklist(interaction: discord.Interaction):
    if not _is_admin(interaction):
        await interaction.response.send_message("Not allowed.", ephemeral=True)
        return
    p = ADMIN_CURRENT.get(interaction.user.id)
    if not p:
        await interaction.response.send_message("Use /cfg_next first.", ephemeral=True)
        return
    cur = get_meta(p)["blacklisted"]
    new = not cur
    set_meta(p, blacklisted=new)
    await interaction.response.send_message(
        f"✅ **{p.name}** blacklisted: **{new}**",
        ephemeral=True
    )

@bot.tree.command(name="cfg_upload", description="Upload a media file into the bot's folder.", guild=CONFIG_GUILD)
@app_commands.describe(file="Attach an image/video", rarity="Optional rarity")
@app_commands.choices(rarity=[app_commands.Choice(name=r, value=r) for r in RARITIES])
async def cfg_upload(interaction: discord.Interaction, file: discord.Attachment, rarity: app_commands.Choice[str] | None = None):
    if not _is_admin(interaction):
        await interaction.response.send_message("Not allowed.", ephemeral=True); return
    try:
        await interaction.response.defer(ephemeral=True, thinking=True)

        fname = file.filename.lower()
        if not any(fname.endswith(ext) for ext in ALLOWED_EXT):
            await interaction.followup.send("Unsupported file type.", ephemeral=True); return
        if file.size > 8 * 1024 * 1024:
            await interaction.followup.send("File too large (>8 MB).", ephemeral=True); return

        # Read into memory for hashing
        raw = await file.read()
        sha = _sha256_bytes(raw)

        # Duplicate check via hash index
        db = _images_db()
        dup_path = db.get("hashes", {}).get(sha)
        if dup_path and Path(dup_path).exists():
            await interaction.followup.send(f"❌ Duplicate file detected. Already uploaded as **{Path(dup_path).name}**.", ephemeral=True)
            return

        # Ensure we can write to the images dir
        IMAGES_DIR.mkdir(parents=True, exist_ok=True)
        probe = IMAGES_DIR / "._writetest"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)

        # Sanitize and ensure unique filename
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", file.filename)
        dest = IMAGES_DIR / safe
        i = 1
        while dest.exists():
            stem, ext = os.path.splitext(safe)
            dest = IMAGES_DIR / f"{stem}_{i}{ext}"
            i += 1

        print(f"/cfg_upload saving to {dest} ({file.size} bytes)")
        dest.write_bytes(raw)

        # Register + rarity + sha256
        meta = set_meta(dest, rarity=(rarity.value if rarity else None), sha256=sha)

        await interaction.followup.send(
            f"✅ Uploaded: **{dest.name}**\nrarity: **{meta['rarity']}** • blacklisted: **{meta['blacklisted']}**",
            ephemeral=True
        )
    except Exception as e:
        # Log to journalctl and tell the user
        import traceback
        tb = "".join(traceback.format_exception_only(type(e), e)).strip()
        print(f"/cfg_upload failed: {tb}")
        try:
            await interaction.followup.send(f"❌ Upload failed: {e}", ephemeral=True)
        except Exception:
            pass

@bot.tree.command(name="cfg_export", description="Create a ZIP of all media and publish a link.", guild=CONFIG_GUILD)
async def cfg_export(interaction: discord.Interaction):
    if not _is_admin(interaction):
        await interaction.response.send_message("Not allowed.", ephemeral=True); return
    try:
        await interaction.response.defer(ephemeral=True, thinking=True)

        if not EXPORT_DIR or not EXPORT_BASE_URL:
            await interaction.followup.send("❌ EXPORT_DIR / EXPORT_BASE_URL not configured.", ephemeral=True); return

        if not EXPORT_DIR.exists():
            EXPORT_DIR.mkdir(parents=True, exist_ok=True)

        items = list_all_images()
        if not items:
            await interaction.followup.send("No media to export.", ephemeral=True); return

        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        zip_name = f"export-{ts}.zip"
        tmp_zip = (IMAGES_DIR / f".{zip_name}")  # temp in images dir
        final_zip = EXPORT_DIR / zip_name

        # Create zip
        with zipfile.ZipFile(tmp_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for p in items:
                # store relative to IMAGES_DIR
                arcname = p.relative_to(IMAGES_DIR)
                zf.write(p, arcname)

        # Move to export dir
        tmp_zip.replace(final_zip)

        url = f"{EXPORT_BASE_URL.rstrip('/')}/{zip_name}"
        await interaction.followup.send(f"✅ Export ready: {url}", ephemeral=True)
    except Exception as e:
        import traceback
        print("cfg_export failed:", "".join(traceback.format_exception_only(type(e), e)).strip())
        try:
            await interaction.followup.send(f"❌ Export failed: {e}", ephemeral=True)
        except Exception:
            pass


@bot.tree.command(name="cfg_rehash", description="Compute hashes for existing media so duplicates can be detected.", guild=CONFIG_GUILD)
async def cfg_rehash(interaction: discord.Interaction):
    if not _is_admin(interaction):
        await interaction.response.send_message("Not allowed.", ephemeral=True); return
    try:
        await interaction.response.defer(ephemeral=True, thinking=True)

        items = list_all_images()
        if not items:
            await interaction.followup.send("No media found.", ephemeral=True); return

        updated = 0
        dup_hits = 0

        # We’ll use your existing helpers and set_meta(), which also maintains the hash index.
        for p in items:
            rec = get_meta(p)
            if rec.get("sha256"):
                continue  # already hashed

            try:
                raw = p.read_bytes()
            except Exception as e:
                print(f"cfg_rehash: failed to read {p}: {e}")
                continue

            sha = _sha256_bytes(raw)

            # Count if another file already registered this hash
            db = _images_db()
            if db.get("hashes", {}).get(sha) and db["hashes"][sha] != str(p.resolve()):
                dup_hits += 1

            set_meta(p, sha256=sha)  # stores sha and updates the hash index
            updated += 1

        await interaction.followup.send(
            f"✅ Rehashed {updated} file(s). Found {dup_hits} duplicate signature(s) already present.",
            ephemeral=True
        )
    except Exception as e:
        import traceback
        print("cfg_rehash failed:", "".join(traceback.format_exception_only(type(e), e)).strip())
        try:
            await interaction.followup.send(f"❌ Rehash failed: {e}", ephemeral=True)
        except Exception:
            pass


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN env var.")
    bot.run(TOKEN)
