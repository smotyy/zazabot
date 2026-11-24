from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import random
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks

try:  # optional: load a local .env file when python-dotenv is installed
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


# ============================================================================
# Config
# ============================================================================
BRAND = "Zaza Scripts"
COLOR = discord.Colour(0x10B981)

TICKET_CATEGORY = "1574917466134675577"  # category where ticket channels are created
LOG_CHANNEL = "1574917555834069042"  # channel that receives logs and transcripts
REVIEWS_CHANNEL = "1574188434619113544"
STAFF_ROLES = ("1574187317009186887", "1574187355114311792")  # roles that can see every ticket
CUSTOMER_ROLE = "1574187393110507520"
CLOSE_DELAY_SECONDS = 5

MIN_GIVEAWAY = timedelta(seconds=10)
MAX_GIVEAWAY = timedelta(days=30)

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID") or 0)
DATA_FILE = Path(__file__).with_name("data.json")


@dataclass(frozen=True)
class TicketType:
    key: str
    label: str
    prompt: str  # label of the form field (Discord limit: 45 characters)
    placeholder: str


TICKET_TYPES: dict[str, TicketType] = {
    t.key: t
    for t in (
        TicketType(
            "purchase",
            "Purchase",
            "What would you like to buy?",
            "Script or service name, and anything we should know.",
        ),
        TicketType(
            "custom",
            "Custom Order",
            "Describe the work you need",
            "What should it do? Framework, features, deadline, budget.",
        ),
        TicketType(
            "support",
            "Support",
            "How can we help?",
            "Which script, what is happening, any errors you see.",
        ),
    )
}


@dataclass(frozen=True)
class Section:
    """A channel that /post can write to."""

    key: str
    channel: str
    has_price: bool = False
    button: str | None = None  # label of the button under each post
    ticket: str | None = None  # ticket type opened by that button


SECTIONS: dict[str, Section] = {
    s.key: s
    for s in (
        Section("rules", "rules"),
        Section("faq", "faq"),
        Section("products", "products", has_price=True, button="Buy", ticket="purchase"),
        Section("services", "services", has_price=True, button="Order", ticket="custom"),
        Section("pricing", "pricing", has_price=True),
        Section("showcase", "showcase"),
        Section("changelog", "changelog"),
    )
}

log = logging.getLogger("zaza-bot")


# ============================================================================
# Helpers
# ============================================================================
def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def make_embed(
    title: str | None = None, description: str | None = None, *, timestamp: bool = False
) -> discord.Embed:
    """Embed with the brand color and footer."""
    embed = discord.Embed(title=title, description=description, colour=COLOR)
    if timestamp:
        embed.timestamp = utcnow()
    embed.set_footer(text=BRAND)
    return embed


async def reply(interaction: discord.Interaction, message: str) -> None:
    """Send a private (ephemeral) reply."""
    await interaction.response.send_message(message, ephemeral=True)


async def send_error(interaction: discord.Interaction) -> None:
    message = "Something went wrong. Please try again or contact the owner."
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await reply(interaction, message)


class SafeView(discord.ui.View):
    """View that tells the user when something fails instead of staying silent."""

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        log.error("View error", exc_info=error)
        await send_error(interaction)


class SafeModal(discord.ui.Modal):
    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.error("Modal error", exc_info=error)
        await send_error(interaction)


def add_field(modal: discord.ui.Modal, label: str, **options) -> discord.ui.TextInput:
    """Add a labelled text field to a modal and return the input."""
    text_input = discord.ui.TextInput(**options)
    modal.add_item(discord.ui.Label(text=label, component=text_input))
    return text_input


def same_name(a: str, b: str) -> bool:
    return a.casefold() == b.casefold()


def find_category(guild: discord.Guild) -> discord.CategoryChannel | None:
    return next((c for c in guild.categories if same_name(c.name, TICKET_CATEGORY)), None)


def find_text_channel(guild: discord.Guild, name: str) -> discord.TextChannel | None:
    return next((c for c in guild.text_channels if same_name(c.name, name)), None)


def find_role(guild: discord.Guild, name: str) -> discord.Role | None:
    return next((r for r in guild.roles if same_name(r.name, name)), None)


def has_role(member: discord.Member, name: str) -> bool:
    return any(same_name(r.name, name) for r in member.roles)


def staff_roles(guild: discord.Guild) -> list[discord.Role]:
    return [r for r in guild.roles if any(same_name(r.name, n) for n in STAFF_ROLES)]


def is_staff(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    return bool(set(member.roles) & set(staff_roles(member.guild)))


async def send_log(guild: discord.Guild, embed: discord.Embed, file: discord.File | None = None) -> None:
    channel = find_text_channel(guild, LOG_CHANNEL)
    if channel is not None:
        await channel.send(embed=embed, **({"file": file} if file else {}))


# ============================================================================
# Tickets: data (stored in the channel topic, so no database is needed)
# ============================================================================
TOPIC_PATTERN = re.compile(r"owner=(?P<owner>\d+) \| type=(?P<type>\w+)")


@dataclass(frozen=True)
class TicketInfo:
    owner_id: int
    ticket_type: TicketType


def make_topic(owner_id: int, ticket_type: TicketType) -> str:
    return f"ticket | owner={owner_id} | type={ticket_type.key}"


def read_ticket(channel: discord.abc.GuildChannel | None) -> TicketInfo | None:
    match = TOPIC_PATTERN.search(getattr(channel, "topic", None) or "")
    if match is None:
        return None
    ticket_type = TICKET_TYPES.get(match["type"])
    return TicketInfo(int(match["owner"]), ticket_type) if ticket_type else None


def find_open_ticket(guild: discord.Guild, user_id: int) -> discord.TextChannel | None:
    category = find_category(guild)
    if category is None:
        return None
    for channel in category.text_channels:
        info = read_ticket(channel)
        if info and info.owner_id == user_id:
            return channel
    return None


async def build_transcript(channel: discord.TextChannel) -> str:
    lines = [
        f"Transcript of #{channel.name} - {BRAND}",
        f"Generated {utcnow():%Y-%m-%d %H:%M} UTC",
        "-" * 60,
    ]
    async for message in channel.history(limit=None, oldest_first=True):
        text = message.content
        for embed in message.embeds:
            parts = [embed.title, embed.description, *(f"{f.name}: {f.value}" for f in embed.fields)]
            text += " [embed] " + " | ".join(p for p in parts if p)
        lines.append(f"[{message.created_at:%Y-%m-%d %H:%M}] {message.author}: {text}".rstrip())
        lines.extend(f"    attachment: {a.url}" for a in message.attachments)
    return "\n".join(lines)


def transcript_file(text: str, channel_name: str) -> discord.File:
    return discord.File(io.BytesIO(text.encode("utf-8")), filename=f"{channel_name}.txt")


# ============================================================================
# Tickets: opening
# ============================================================================
async def open_ticket_form(interaction: discord.Interaction, key: str, prefill: str = "") -> None:
    """Show the ticket form, unless the user already has an open ticket."""
    if existing := find_open_ticket(interaction.guild, interaction.user.id):
        await reply(interaction, f"You already have an open ticket: {existing.mention}")
    else:
        await interaction.response.send_modal(TicketModal(TICKET_TYPES[key], prefill))


class TicketModal(SafeModal):
    def __init__(self, ticket_type: TicketType, prefill: str = "") -> None:
        super().__init__(title=f"{ticket_type.label} ticket")
        self.ticket_type = ticket_type
        self.details = add_field(
            self,
            ticket_type.prompt,
            placeholder=ticket_type.placeholder,
            style=discord.TextStyle.paragraph,
            default=prefill[:1000] or None,
            max_length=1000,
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await create_ticket(interaction, self.ticket_type, str(self.details))


def ticket_overwrites(
    guild: discord.Guild, user: discord.Member, roles: list[discord.Role]
) -> dict[discord.abc.Snowflake, discord.PermissionOverwrite]:
    """Only the ticket owner, the staff roles and the bot can see the channel."""
    chat = dict(view_channel=True, send_messages=True, read_message_history=True,
                attach_files=True, embed_links=True)
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        user: discord.PermissionOverwrite(**chat),
        guild.me: discord.PermissionOverwrite(**chat, manage_channels=True),
    }
    overwrites.update({role: discord.PermissionOverwrite(**chat, manage_messages=True) for role in roles})
    return overwrites


async def create_ticket(interaction: discord.Interaction, ticket_type: TicketType, details: str) -> None:
    guild, user = interaction.guild, interaction.user

    category = find_category(guild)
    if category is None:
        await reply(interaction, f"The '{TICKET_CATEGORY}' category was not found. Please contact the owner.")
        return
    if existing := find_open_ticket(guild, user.id):
        await reply(interaction, f"You already have an open ticket: {existing.mention}")
        return

    await interaction.response.defer(ephemeral=True)
    roles = staff_roles(guild)
    slug = re.sub(r"[^a-z0-9]", "", user.name.lower())[:20] or str(user.id)

    try:
        channel = await guild.create_text_channel(
            name=f"{ticket_type.key}-{slug}",
            category=category,
            topic=make_topic(user.id, ticket_type),
            overwrites=ticket_overwrites(guild, user, roles),
            reason=f"{ticket_type.label} ticket opened by {user}",
        )
    except discord.HTTPException:
        log.exception("Could not create a ticket channel")
        await interaction.followup.send("I could not create the ticket. Please contact the owner.", ephemeral=True)
        return

    embed = make_embed(
        f"{ticket_type.label} ticket",
        "Thanks for reaching out. A member of the team will be with you shortly.",
        timestamp=True,
    )
    embed.add_field(name=ticket_type.prompt, value=details[:1024], inline=False)
    embed.add_field(name="Opened by", value=user.mention)
    mentions = " ".join([user.mention, *(r.mention for r in roles)])
    await channel.send(mentions, embed=embed, view=TicketControls())

    opened = make_embed("Ticket opened", timestamp=True)
    opened.add_field(name="Channel", value=channel.mention)
    opened.add_field(name="Type", value=ticket_type.label)
    opened.add_field(name="Opened by", value=user.mention)
    await send_log(guild, opened)

    await interaction.followup.send(f"Your ticket has been created: {channel.mention}", ephemeral=True)


# ============================================================================
# Tickets: closing
# ============================================================================
async def close_ticket(channel: discord.TextChannel, closed_by: discord.abc.User) -> None:
    guild = channel.guild
    info = read_ticket(channel)
    transcript = await build_transcript(channel)

    await channel.send(embed=make_embed(description=f"This ticket will be closed in {CLOSE_DELAY_SECONDS} seconds."))

    closed = make_embed("Ticket closed", timestamp=True)
    closed.add_field(name="Ticket", value=f"#{channel.name}")
    closed.add_field(name="Type", value=info.ticket_type.label if info else "Unknown")
    closed.add_field(name="Opened by", value=f"<@{info.owner_id}>" if info else "Unknown")
    closed.add_field(name="Closed by", value=closed_by.mention)
    await send_log(guild, closed, transcript_file(transcript, channel.name))

    owner = guild.get_member(info.owner_id) if info else None
    if owner:
        try:
            await owner.send(
                f"Your ticket on **{guild.name}** was closed. Here is a copy of the conversation.",
                file=transcript_file(transcript, channel.name),
            )
        except discord.HTTPException:
            pass  # the member has DMs closed

    await asyncio.sleep(CLOSE_DELAY_SECONDS)
    await channel.delete(reason=f"Ticket closed by {closed_by}")


class ConfirmClose(SafeView):
    def __init__(self) -> None:
        super().__init__(timeout=30)

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="Closing ticket...", view=None)
        await close_ticket(interaction.channel, interaction.user)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="Cancelled.", view=None)


class TicketControls(SafeView):
    """Buttons shown inside every ticket."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="Close ticket", style=discord.ButtonStyle.danger, custom_id="zaza:ticket:close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        info = read_ticket(interaction.channel)
        if info is None:
            await reply(interaction, "This is not a ticket channel.")
        elif interaction.user.id != info.owner_id and not is_staff(interaction.user):
            await reply(interaction, "Only staff or the ticket owner can close this ticket.")
        else:
            await interaction.response.send_message("Close this ticket?", view=ConfirmClose(), ephemeral=True)


class TicketPanel(SafeView):
    """The public panel with one button per ticket type."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="Purchase", style=discord.ButtonStyle.success, custom_id="zaza:ticket:purchase")
    async def purchase(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await open_ticket_form(interaction, "purchase")

    @discord.ui.button(label="Custom Order", style=discord.ButtonStyle.secondary, custom_id="zaza:ticket:custom")
    async def custom(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await open_ticket_form(interaction, "custom")

    @discord.ui.button(label="Support", style=discord.ButtonStyle.secondary, custom_id="zaza:ticket:support")
    async def support(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await open_ticket_form(interaction, "support")


# ============================================================================
# Posts: rules, faq, products, services, pricing, showcase, changelog
# ============================================================================
def build_post_embed(title: str, content: str, price: str = "", image: str = "") -> discord.Embed:
    embed = make_embed(title, content)
    if price:
        embed.add_field(name="Price", value=price, inline=False)
    if image.startswith(("http://", "https://")):
        embed.set_image(url=image)
    return embed


def section_for_channel(channel: discord.abc.GuildChannel) -> Section | None:
    return next((s for s in SECTIONS.values() if same_name(s.channel, channel.name)), None)


class PostView(SafeView):
    """The button under a product or service: opens a ticket with the title filled in."""

    def __init__(self, section: Section) -> None:
        super().__init__(timeout=None)
        self.section = section
        button = discord.ui.Button(
            label=section.button, style=discord.ButtonStyle.success, custom_id=f"zaza:post:{section.key}"
        )
        button.callback = self.on_click
        self.add_item(button)

    async def on_click(self, interaction: discord.Interaction) -> None:
        embeds = interaction.message.embeds
        title = (embeds[0].title or "") if embeds else ""
        await open_ticket_form(interaction, self.section.ticket, prefill=title)


class PostModal(SafeModal):
    """Form for a new post, or for editing an existing one when `message` is given."""

    def __init__(self, section: Section, message: discord.Message | None = None) -> None:
        super().__init__(title=f"{'Edit' if message else 'New'} {section.key} post")
        self.section = section
        self.message = message

        current = message.embeds[0] if message and message.embeds else None
        price = next((f.value for f in current.fields if f.name == "Price"), None) if current else None

        self.name_input = add_field(self, "Title", max_length=256, default=current.title if current else None)
        self.body_input = add_field(
            self,
            "Content",
            style=discord.TextStyle.paragraph,
            max_length=4000,
            default=current.description if current else None,
        )
        self.price_input = (
            add_field(self, "Price", max_length=100, required=False, placeholder="For example 15 EUR", default=price)
            if section.has_price
            else None
        )
        self.image_input = add_field(
            self,
            "Image link (optional)",
            max_length=500,
            required=False,
            default=current.image.url if current and current.image else None,
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        embed = build_post_embed(
            str(self.name_input),
            str(self.body_input),
            str(self.price_input) if self.price_input is not None else "",
            str(self.image_input),
        )
        if self.message:
            await self.message.edit(embed=embed)
            await reply(interaction, "Post updated.")
            return

        channel = find_text_channel(interaction.guild, self.section.channel)
        if channel is None:
            await reply(interaction, f"The #{self.section.channel} channel was not found.")
            return
        view = PostView(self.section) if self.section.button else None
        await channel.send(embed=embed, **({"view": view} if view else {}))
        await reply(interaction, f"Posted in {channel.mention}.")


async def fetch_own_post(interaction: discord.Interaction, text: str) -> discord.Message | None:
    """Find a message of the bot from a message link or from an ID in the current channel."""
    ids = re.findall(r"\d+", text)
    if not ids:
        return None
    channel_id = int(ids[-2]) if len(ids) >= 3 else interaction.channel_id
    channel = interaction.guild.get_channel(channel_id)
    if channel is None:
        return None
    try:
        message = await channel.fetch_message(int(ids[-1]))
    except discord.HTTPException:
        return None
    return message if message.author.id == interaction.client.user.id and message.embeds else None


# ============================================================================
# Reviews
# ============================================================================
class ReviewModal(SafeModal):
    def __init__(self) -> None:
        super().__init__(title="Leave a review")
        self.rating = add_field(self, "Rating (1 to 5)", max_length=1, placeholder="5")
        self.product = add_field(self, "Product or service", max_length=100, required=False)
        self.comment = add_field(self, "Your review", style=discord.TextStyle.paragraph, max_length=1000)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        rating = str(self.rating).strip()
        stars = int(rating) if rating in {"1", "2", "3", "4", "5"} else 0
        if not stars:
            await reply(interaction, "The rating must be a number from 1 to 5.")
            return
        channel = find_text_channel(interaction.guild, REVIEWS_CHANNEL)
        if channel is None:
            await reply(interaction, f"The #{REVIEWS_CHANNEL} channel was not found.")
            return

        embed = make_embed("\u2605" * stars + "\u2606" * (5 - stars), str(self.comment))
        embed.set_author(name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url)
        if str(self.product):
            embed.add_field(name="Product", value=str(self.product))
        await channel.send(embed=embed)
        await reply(interaction, "Thank you for your review!")


# ============================================================================
# Giveaways
# ============================================================================
@dataclass
class Giveaway:
    message_id: int
    channel_id: int
    prize: str
    winners: int
    host_id: int
    ends_at: float  # unix timestamp
    participants: list[int] = field(default_factory=list)
    ended: bool = False
    winner_ids: list[int] = field(default_factory=list)


class GiveawayStore:
    """Giveaways saved in a small JSON file, so they survive restarts."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.items: dict[int, Giveaway] = {}
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.items = {int(k): Giveaway(**v) for k, v in raw.get("giveaways", {}).items()}

    def get(self, message_id: int) -> Giveaway | None:
        return self.items.get(message_id)

    def add(self, giveaway: Giveaway) -> None:
        self.items[giveaway.message_id] = giveaway
        self.save()

    def due(self, now_ts: float) -> list[Giveaway]:
        return [g for g in self.items.values() if not g.ended and g.ends_at <= now_ts]

    def save(self) -> None:
        data = {"giveaways": {str(k): asdict(v) for k, v in self.items.items()}}
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(temp, self.path)  # atomic, never leaves a half-written file


giveaways = GiveawayStore(DATA_FILE)

DURATION_PATTERN = re.compile(r"(\d+)\s*([smhdw])")
DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration(text: str) -> timedelta | None:
    """Turn '30m', '2d' or '1d12h' into a timedelta."""
    text = text.strip().lower()
    parts = DURATION_PATTERN.findall(text)
    if not parts or DURATION_PATTERN.sub("", text).strip():
        return None
    return timedelta(seconds=sum(int(n) * DURATION_UNITS[unit] for n, unit in parts))


def pick_winners(pool: list[int], count: int) -> list[int]:
    return random.sample(pool, k=min(count, len(pool)))


def mention_all(user_ids: list[int]) -> str:
    return ", ".join(f"<@{i}>" for i in user_ids)


def giveaway_embed(giveaway: Giveaway) -> discord.Embed:
    if giveaway.ended:
        embed = make_embed("Giveaway ended", f"**{giveaway.prize}**")
        embed.add_field(name="Winners", value=mention_all(giveaway.winner_ids) or "No valid entries", inline=False)
    else:
        ends = datetime.fromtimestamp(giveaway.ends_at, timezone.utc)
        embed = make_embed("Giveaway", f"**{giveaway.prize}**\n\nPress the button below to enter.")
        embed.add_field(name="Ends", value=discord.utils.format_dt(ends, "R"))
        embed.add_field(name="Winners", value=str(giveaway.winners))
    embed.add_field(name="Hosted by", value=f"<@{giveaway.host_id}>")
    return embed


class GiveawayView(SafeView):
    def __init__(self, disabled: bool = False) -> None:
        super().__init__(timeout=None)
        self.enter.disabled = disabled

    @discord.ui.button(label="Enter", style=discord.ButtonStyle.success, custom_id="zaza:giveaway:enter")
    async def enter(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        giveaway = giveaways.get(interaction.message.id)
        if giveaway is None or giveaway.ended:
            await reply(interaction, "This giveaway has ended.")
            return
        user_id = interaction.user.id
        if user_id in giveaway.participants:
            giveaway.participants.remove(user_id)
            message = "You left the giveaway."
        else:
            giveaway.participants.append(user_id)
            message = "You are in. Good luck!"
        giveaways.save()
        await reply(interaction, f"{message} ({len(giveaway.participants)} entries)")


async def publish_giveaway(client: discord.Client, giveaway: Giveaway, announcement: str) -> None:
    """Update the giveaway message and announce the result in the channel."""
    channel = client.get_channel(giveaway.channel_id)
    if channel is None:
        return
    message = None
    try:
        message = await channel.fetch_message(giveaway.message_id)
        await message.edit(embed=giveaway_embed(giveaway), view=GiveawayView(disabled=True))
    except discord.NotFound:
        pass  # the giveaway message was deleted
    await channel.send(announcement, reference=message) if message else await channel.send(announcement)


async def end_giveaway(client: discord.Client, giveaway: Giveaway) -> None:
    giveaway.ended = True
    giveaway.winner_ids = pick_winners(giveaway.participants, giveaway.winners)
    giveaways.save()
    if giveaway.winner_ids:
        text = f"Congratulations {mention_all(giveaway.winner_ids)}! You won **{giveaway.prize}**."
    else:
        text = f"Nobody entered the giveaway for **{giveaway.prize}**."
    await publish_giveaway(client, giveaway, text)


async def find_giveaway(interaction: discord.Interaction, text: str) -> Giveaway | None:
    ids = re.findall(r"\d+", text)
    giveaway = giveaways.get(int(ids[-1])) if ids else None
    if giveaway is None:
        await reply(interaction, "Giveaway not found. Pass the message ID or the message link.")
    return giveaway


# ============================================================================
# Bot
# ============================================================================
class ZazaBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True  # required to write transcripts
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            activity=discord.Activity(type=discord.ActivityType.watching, name=BRAND),
        )

    async def setup_hook(self) -> None:
        # Persistent views keep old panels, tickets, posts and giveaways working after restarts.
        self.add_view(TicketPanel())
        self.add_view(TicketControls())
        self.add_view(GiveawayView())
        for section in SECTIONS.values():
            if section.button:
                self.add_view(PostView(section))

        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        self.check_giveaways.start()

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s)", self.user, self.user.id)

    @tasks.loop(seconds=15)
    async def check_giveaways(self) -> None:
        for giveaway in giveaways.due(time.time()):
            try:
                await end_giveaway(self, giveaway)
            except Exception:
                log.exception("Could not end giveaway %s", giveaway.message_id)

    @check_giveaways.before_loop
    async def wait_until_ready_first(self) -> None:
        await self.wait_until_ready()


bot = ZazaBot()


@bot.tree.error
async def on_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    log.error("Command error", exc_info=error)
    await send_error(interaction)


# ============================================================================
# Commands
# ============================================================================
admin_only = app_commands.default_permissions(administrator=True)


@bot.tree.command(name="panel", description="Post the ticket panel in this channel")
@admin_only
@app_commands.guild_only()
async def panel(interaction: discord.Interaction) -> None:
    embed = make_embed(
        "Open a ticket",
        "Need a script, a custom build, or help with something you purchased? "
        "Choose a category below and our team will get back to you.\n\n"
        "**Purchase**\nBuy a script or service.\n\n"
        "**Custom Order**\nRequest custom development.\n\n"
        "**Support**\nGet help with a script you own.",
    )
    await interaction.channel.send(embed=embed, view=TicketPanel())
    await reply(interaction, "Panel posted.")


@bot.tree.command(name="post", description="Post an embed in the rules, faq, products, services... channels")
@app_commands.describe(section="Which channel to post in")
@app_commands.choices(section=[app_commands.Choice(name=s.key, value=s.key) for s in SECTIONS.values()])
@admin_only
@app_commands.guild_only()
async def post(interaction: discord.Interaction, section: app_commands.Choice[str]) -> None:
    await interaction.response.send_modal(PostModal(SECTIONS[section.value]))


@bot.tree.command(name="edit", description="Edit a post that the bot made with /post")
@app_commands.describe(message="Message link (or ID, if it is in this channel)")
@admin_only
@app_commands.guild_only()
async def edit(interaction: discord.Interaction, message: str) -> None:
    target = await fetch_own_post(interaction, message)
    section = section_for_channel(target.channel) if target else None
    if section is None:
        await reply(interaction, "I could not find a post made with /post. Pass its message link.")
        return
    await interaction.response.send_modal(PostModal(section, target))


@bot.tree.command(name="review", description="Leave a review for a script or service you bought")
@app_commands.guild_only()
async def review(interaction: discord.Interaction) -> None:
    if not (has_role(interaction.user, CUSTOMER_ROLE) or is_staff(interaction.user)):
        await reply(interaction, "Only customers can leave reviews.")
        return
    await interaction.response.send_modal(ReviewModal())


# ---- Customer role ----------------------------------------------------------
customer = app_commands.Group(
    name="customer",
    description="Manage the Customer role",
    default_permissions=discord.Permissions(administrator=True),
    guild_only=True,
)


async def set_customer_role(interaction: discord.Interaction, member: discord.Member, *, give: bool) -> None:
    role = find_role(interaction.guild, CUSTOMER_ROLE)
    if role is None:
        await reply(interaction, f"The '{CUSTOMER_ROLE}' role was not found.")
        return
    try:
        if give:
            await member.add_roles(role, reason=f"Customer role added by {interaction.user}")
        else:
            await member.remove_roles(role, reason=f"Customer role removed by {interaction.user}")
    except discord.Forbidden:
        await reply(interaction, f"I can't manage that role. Move my role above '{role.name}'.")
        return
    await reply(interaction, f"{'Added' if give else 'Removed'} {role.name} for {member.mention}.")


@customer.command(name="add", description="Give the Customer role to a member")
async def customer_add(interaction: discord.Interaction, member: discord.Member) -> None:
    await set_customer_role(interaction, member, give=True)


@customer.command(name="remove", description="Remove the Customer role from a member")
async def customer_remove(interaction: discord.Interaction, member: discord.Member) -> None:
    await set_customer_role(interaction, member, give=False)


bot.tree.add_command(customer)


# ---- Giveaways --------------------------------------------------------------
giveaway_group = app_commands.Group(
    name="giveaway",
    description="Manage giveaways",
    default_permissions=discord.Permissions(administrator=True),
    guild_only=True,
)


@giveaway_group.command(name="start", description="Start a giveaway in this channel")
@app_commands.describe(
    prize="What is being given away",
    duration="For example 30m, 12h, 2d or 1d12h",
    winners="Number of winners",
)
async def giveaway_start(
    interaction: discord.Interaction,
    prize: app_commands.Range[str, 1, 200],
    duration: str,
    winners: app_commands.Range[int, 1, 20] = 1,
) -> None:
    delta = parse_duration(duration)
    if delta is None or not MIN_GIVEAWAY <= delta <= MAX_GIVEAWAY:
        await reply(interaction, "Use a duration like 30m, 12h or 2d (from 10 seconds to 30 days).")
        return
    giveaway = Giveaway(
        message_id=0,
        channel_id=interaction.channel_id,
        prize=prize,
        winners=winners,
        host_id=interaction.user.id,
        ends_at=(utcnow() + delta).timestamp(),
    )
    message = await interaction.channel.send(embed=giveaway_embed(giveaway), view=GiveawayView())
    giveaway.message_id = message.id
    giveaways.add(giveaway)
    await reply(interaction, f"Giveaway started: {message.jump_url}")


@giveaway_group.command(name="end", description="End a giveaway now")
@app_commands.describe(message="Message link or ID of the giveaway")
async def giveaway_end(interaction: discord.Interaction, message: str) -> None:
    giveaway = await find_giveaway(interaction, message)
    if giveaway is None:
        return
    if giveaway.ended:
        await reply(interaction, "That giveaway has already ended.")
        return
    await interaction.response.defer(ephemeral=True)
    await end_giveaway(interaction.client, giveaway)
    await interaction.followup.send("Giveaway ended.", ephemeral=True)


@giveaway_group.command(name="reroll", description="Pick new winners for an ended giveaway")
@app_commands.describe(message="Message link or ID of the giveaway")
async def giveaway_reroll(interaction: discord.Interaction, message: str) -> None:
    giveaway = await find_giveaway(interaction, message)
    if giveaway is None:
        return
    if not giveaway.ended:
        await reply(interaction, "That giveaway is still running.")
        return
    pool = [u for u in giveaway.participants if u not in giveaway.winner_ids]
    if not pool:
        await reply(interaction, "There are no other entries to pick from.")
        return
    await interaction.response.defer(ephemeral=True)
    giveaway.winner_ids = pick_winners(pool, giveaway.winners)
    giveaways.save()
    text = f"New winner for **{giveaway.prize}**: {mention_all(giveaway.winner_ids)}. Congratulations!"
    await publish_giveaway(interaction.client, giveaway, text)
    await interaction.followup.send("Rerolled.", ephemeral=True)


bot.tree.add_command(giveaway_group)


# ============================================================================
# Entry point
# ============================================================================
def main() -> None:
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN is missing. Set it as an environment variable or in a .env file.")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    bot.run(TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
