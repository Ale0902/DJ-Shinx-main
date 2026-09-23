import discord
from discord.ext import commands, tasks
import asyncio
import contextlib
import datetime
import logging
import re
from zoneinfo import ZoneInfo
import responses
import botFunctions as bf
import channel_config
import game_news
import llmask
import memory_db
import sports
import f1
import os
from dotenv import load_dotenv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

EASTERN = ZoneInfo("America/New_York")

# Exact cyan, used as every embed's left-hand accent color for a consistent look.
EMBED_COLOR = discord.Color.from_rgb(0, 255, 255)

# Matches a direct link to an image file (as opposed to a page that merely
# contains one) -- used to display /chat's image_search results inline in
# the embed itself rather than just as a clickable text link.
IMAGE_URL_RE = re.compile(r'https?://\S+\.(?:jpg|jpeg|png|gif|webp)\b', re.IGNORECASE)

# Sites Discord natively unfurls into a rich preview (video thumbnail +
# player for YouTube, full card for a tweet/X post) -- but only when the
# URL is in a plain message's raw content, never when it's just text inside
# an embed's description (same reason /vini below is sent as plain text
# instead of an embed). /chat's citations are a dynamic URL the model
# picks, not a fixed one, so instead of leaving it as dead text inside the
# embed, it's also re-sent as its own plain message so Discord unfurls it.
RICH_PREVIEW_URL_RE = re.compile(
    r'https?://(?:www\.)?(?:youtube\.com/watch\S*|youtu\.be/\S+|(?:twitter|x)\.com/\w+/status/\d+\S*)',
    re.IGNORECASE,
)

# Same "re-post as plain content so Discord actually unfurls it" idea as
# RICH_PREVIEW_URL_RE, but unscoped to specific sites -- game_news.py's
# announcement links are always a Nintendo Life or PlayStation Blog article
# (never YouTube/Twitter), and any well-formed news article has its own
# Open Graph preview card, so there's no need to special-case a domain list
# the way the native YouTube/tweet embeds above require.
ANNOUNCEMENT_URL_RE = re.compile(r'https?://\S+')

MAX_EMBED_DESC = 4096  # Discord's hard cap on an embed description


def _with_question(question_line: str, body: str) -> str:
    """Joins /chat's echoed question and its status/answer into a single
    embed description, so the two show as one box instead of two separate
    messages. Truncates the question (never the body) if the combination
    would exceed Discord's embed description cap -- the body is what
    actually matters; the question is just context above it."""
    available = MAX_EMBED_DESC - len(body) - 2  # 2 for the blank line between them
    if available <= 0:
        return body
    if len(question_line) > available:
        question_line = question_line[:available - 1].rstrip() + "…"
    return f"{question_line}\n\n{body}"


def _embed(text: str) -> discord.Embed:
    """Wraps plain text in a cyan-bordered embed -- the standard shape for
    every bot response. Existing sports.py/llmask.py output is already
    sized for Discord's 2000-char plain-message limit, well under an
    embed description's 4096-char cap, so no re-chunking is needed.
    If the text contains a direct image URL (e.g. /chat citing an
    image_search result), it's shown as an actual picture inside the
    embed instead of just a plain link."""
    e = discord.Embed(description=text, color=EMBED_COLOR)
    image_match = IMAGE_URL_RE.search(text)
    if image_match:
        e.set_image(url=image_match.group(0))
    return e

# Commands that work normally but are left out of /help entirely -- easter
# eggs that only show up if you already know about them.
HIDDEN_COMMANDS = {'vini'}

# Which section of /help each command is listed under. Anything not listed
# here falls into an "Other" section so a forgotten new command still shows
# up instead of silently vanishing from the list.
COMMAND_CATEGORIES = {
    'nfl': 'Sports',
    'nfllive': 'Sports',
    'nflresults': 'Sports',
    'nflstandings': 'Sports',
    'cfb': 'Sports',
    'mlb': 'Sports',
    'soccer': 'Sports',
    'livesoccer': 'Sports',
    'soccerresults': 'Sports',
    'prem': 'Sports',
    'laliga': 'Sports',
    'ucl': 'Sports',
    'f1': 'Sports',
    'f1standings': 'Sports',
    'recsong': 'Music',
    'top5songs': 'Music',
    'hello': 'Fun',
    'rolld6': 'Fun',
    'rolld20': 'Fun',
    'ping': 'Fun',
    'coin_flip': 'Fun',
    '8ball': 'Fun',
    'mcstatus': 'Other',
    'setchannel': 'Other',
    'chat': 'AI',
    'forget': 'AI',
    'forgetme': 'AI',
}
CATEGORY_ORDER = ['Sports', 'Music', 'Fun', 'AI', 'Other']

# Folder that contains this script, so file paths work on Windows and Linux
BASE = os.path.dirname(os.path.abspath(__file__))

ENV_PATH = os.path.join(BASE, 'code.env')
load_dotenv(dotenv_path=ENV_PATH)
TOKEN = os.getenv('DISCORD_TOKEN')

if not TOKEN:
    raise RuntimeError(f"DISCORD_TOKEN not found. Make sure it is set in {os.path.abspath(ENV_PATH)}")


class PaginatorView(discord.ui.View):
    """Shows a list of (title, page_text) pages one at a time, flipped
    through with buttons -- used by any command that'd otherwise need a
    separate command per page (e.g. /soccer per competition, /nflstandings
    per view)."""

    def __init__(self, pages, author_id, command_name):
        super().__init__(timeout=180)
        self.pages = pages  # list of (title, page_text)
        self.index = 0
        self.author_id = author_id
        self.command_name = command_name
        self.message = None
        self._update_buttons()

    def _update_buttons(self):
        self.previous_button.disabled = self.index == 0
        self.next_button.disabled = self.index == len(self.pages) - 1

    def embed(self) -> discord.Embed:
        title, body = self.pages[self.index]
        e = _embed(body)
        e.set_footer(text=f"Page {self.index + 1}/{len(self.pages)} — {title}")
        return e

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                f"Only the person who ran /{self.command_name} can flip pages.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index -= 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index += 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class _SetChannelFeatureSelect(discord.ui.Select):
    """Step 1 of /setchannel: pick which feature to configure."""

    def __init__(self, parent_view: "SetChannelView"):
        options = [
            discord.SelectOption(label=label, value=key)
            for key, label in channel_config.FEATURES.items()
        ]
        super().__init__(placeholder="Step 1: choose a feature to configure...", options=options)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        self.parent_view.feature = self.values[0]
        label = channel_config.FEATURES[self.values[0]]
        self.parent_view.clear_items()
        self.parent_view.add_item(_SetChannelChannelSelect(self.parent_view))
        await interaction.response.edit_message(
            embed=_embed(f"**{label}** selected. Now pick a channel:"),
            view=self.parent_view,
        )


class _SetChannelChannelSelect(discord.ui.ChannelSelect):
    """Step 2 of /setchannel: pick the destination channel via Discord's
    own native channel picker -- no typing or ID-copying required."""

    def __init__(self, parent_view: "SetChannelView"):
        super().__init__(
            placeholder="Step 2: choose a channel...",
            channel_types=[discord.ChannelType.text],
        )
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        channel = self.values[0]
        # interaction.guild is a cache lookup and can come back None; guild_id
        # comes straight off the interaction payload and is always present.
        channel_config.set_channel(interaction.guild_id, self.parent_view.feature, channel.id)
        label = channel_config.FEATURES[self.parent_view.feature]
        self.parent_view.clear_items()
        await interaction.response.edit_message(
            embed=_embed(f"✅ **{label}** will now post in {channel.mention}."),
            view=self.parent_view,
        )
        self.parent_view.stop()


class SetChannelView(discord.ui.View):
    """Backs /setchannel's guided flow: pick a feature from a dropdown,
    then a channel from a native Discord picker, one step at a time --
    instead of typing feature and channel as command arguments."""

    def __init__(self, author_id):
        super().__init__(timeout=120)
        self.author_id = author_id
        self.feature: str | None = None
        self.message: discord.Message | None = None
        self.add_item(_SetChannelFeatureSelect(self))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only the person who ran /setchannel can use this.", ephemeral=True
            )
            return False
        return True

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


def run_discord_bot():
    intents = discord.Intents.default()
    intents.message_content = True
    client = commands.Bot(command_prefix=['!', '/'], intents=intents, help_command=None)

 #=========================--COMMANDS (slash + ! text)--=====================#
 # hybrid_command registers each one as both a /slash command and a
 # !text command from a single function, instead of maintaining two copies.

    @client.hybrid_command(name="hello", description="Says hello")
    async def hello(ctx: commands.Context):
        await ctx.send(embed=_embed("Hello!"))

    @client.hybrid_command(name="recsong", description="Recommends a random song from the server's list")
    async def recsong(ctx: commands.Context):
        await ctx.defer()
        result = await asyncio.to_thread(bf.recsongs)
        await ctx.send(embed=_embed(result))

    @client.hybrid_command(name="top5songs", description="Top 5 songs on iTunes charts!")
    async def top5(ctx: commands.Context):
        await ctx.defer()
        result = await asyncio.to_thread(bf.topsongs)
        await ctx.send(embed=_embed(result))

    @client.hybrid_command(name='rolld6', description='Rolls a D6 dice')
    async def rolld6(ctx: commands.Context):
        await ctx.send(embed=_embed(responses.rolld6()))

    @client.hybrid_command(name='rolld20', description='Rolls a D20 dice')
    async def rolld20(ctx: commands.Context):
        await ctx.send(embed=_embed(responses.rolld20()))

    @client.hybrid_command(name='ping', description='pingpong')
    async def ping(ctx: commands.Context):
        await ctx.send(embed=_embed(responses.ping()))

    @client.hybrid_command(name="coin_flip", description="Flip a coin!")
    async def coinflip(ctx: commands.Context):
        await ctx.send(embed=_embed(responses.coinflip()))

    @client.hybrid_command(name="vini", description="Posts a Vini Jr tweet")
    async def vini(ctx: commands.Context):
        # Deliberately plain text, not an embed -- Discord only auto-unfurls
        # a link into its own rich preview when it's raw message content,
        # not when it's tucked inside an embed description.
        await ctx.send("https://x.com/vinijr/status/1851023004496789695?s=20")

    @client.hybrid_command(name="8ball", description="Shakes an eight ball")
    @discord.app_commands.describe(question="What do you want to ask the eight ball?")
    async def eightball(ctx: commands.Context, *, question: str = None):
        if question:
            await ctx.send(embed=_embed(f"**From {ctx.author.display_name}:** {question}"))
        await ctx.send(embed=_embed(bf.eightball()))

    @client.hybrid_command(name="nfl", description="This week's NFL games and live scores")
    async def nfl(ctx: commands.Context):
        await ctx.defer()
        messages = await asyncio.to_thread(sports.nfl_synopsis)
        for message in messages:
            await ctx.send(embed=_embed(message))

    @client.hybrid_command(name="nfllive", description="Only NFL games currently in progress")
    async def nfllive(ctx: commands.Context):
        await ctx.defer()
        messages = await asyncio.to_thread(sports.nfl_live_matches)
        for message in messages:
            await ctx.send(embed=_embed(message))

    @client.hybrid_command(name="nflresults", description="This week's finished NFL games and final scores")
    async def nflresults(ctx: commands.Context):
        await ctx.defer()
        messages = await asyncio.to_thread(sports.nfl_results_this_week)
        for message in messages:
            await ctx.send(embed=_embed(message))

    @client.hybrid_command(name="cfb", description="This week's ranked college football games, with a South Florida spotlight")
    async def cfb(ctx: commands.Context):
        await ctx.defer()
        messages = await asyncio.to_thread(sports.cfb_synopsis)
        for message in messages:
            await ctx.send(embed=_embed(message))

    @client.hybrid_command(name="mlb", description="This week's MLB series and their records")
    async def mlb(ctx: commands.Context):
        await ctx.defer()
        messages = await asyncio.to_thread(sports.mlb_series_synopsis)
        for message in messages:
            await ctx.send(embed=_embed(message))

    @client.hybrid_command(name="soccer", description="This week's matches, one page per competition")
    async def soccer(ctx: commands.Context):
        await ctx.defer()
        pages = await asyncio.to_thread(sports.soccer_pages)
        if not pages:
            await ctx.send(embed=_embed("No matches scheduled in any tracked competition this week."))
            return
        view = PaginatorView(pages, author_id=ctx.author.id, command_name="soccer")
        message = await ctx.send(embed=view.embed(), view=view)
        view.message = message

    @client.hybrid_command(name="nflstandings", description="NFL standings by division, and the current playoff picture")
    async def nflstandings(ctx: commands.Context):
        await ctx.defer()
        pages = await asyncio.to_thread(sports.nfl_standings_pages)
        view = PaginatorView(pages, author_id=ctx.author.id, command_name="nflstandings")
        message = await ctx.send(embed=view.embed(), view=view)
        view.message = message

    @client.hybrid_command(name="livesoccer", description="Only soccer matches currently in progress")
    async def livesoccer(ctx: commands.Context):
        await ctx.defer()
        messages = await asyncio.to_thread(sports.live_soccer_matches)
        for message in messages:
            await ctx.send(embed=_embed(message))

    @client.hybrid_command(name="soccerresults", description="Today's finished soccer matches and final scores")
    async def soccerresults(ctx: commands.Context):
        await ctx.defer()
        messages = await asyncio.to_thread(sports.soccer_results_today)
        for message in messages:
            await ctx.send(embed=_embed(message))

    @client.hybrid_command(name="prem", description="Current Premier League standings")
    async def prem(ctx: commands.Context):
        await ctx.defer()
        messages = await asyncio.to_thread(sports.premier_league_table)
        for message in messages:
            await ctx.send(embed=_embed(message))

    @client.hybrid_command(name="laliga", description="Current La Liga standings")
    async def laliga(ctx: commands.Context):
        await ctx.defer()
        messages = await asyncio.to_thread(sports.la_liga_table)
        for message in messages:
            await ctx.send(embed=_embed(message))

    @client.hybrid_command(name="ucl", description="Current Champions League standings or bracket")
    async def ucl(ctx: commands.Context):
        await ctx.defer()
        messages = await asyncio.to_thread(sports.ucl_table)
        for message in messages:
            await ctx.send(embed=_embed(message))

    @client.hybrid_command(name="f1", description="Is it F1 race weekend right now?")
    async def f1cmd(ctx: commands.Context):
        await ctx.defer()
        result = await asyncio.to_thread(f1.f1_status)
        await ctx.send(embed=_embed(result))

    @client.hybrid_command(name="f1standings", description="Current F1 drivers' and constructors' championship standings")
    async def f1standings(ctx: commands.Context):
        await ctx.defer()
        messages = await asyncio.to_thread(f1.f1_standings)
        for message in messages:
            await ctx.send(embed=_embed(message))

    @client.hybrid_command(name="mcstatus", description="Check the Minecraft server status")
    async def mcstatus(ctx: commands.Context):
        await ctx.defer()
        result = await asyncio.to_thread(bf.mc_status)
        await ctx.send(embed=_embed(result))

    @client.hybrid_command(name="setchannel", description="Choose which channel an announcement feature posts into")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def setchannel(ctx: commands.Context):
        view = SetChannelView(author_id=ctx.author.id)
        message = await ctx.send(embed=_embed("Step 1: choose a feature to configure."), view=view)
        view.message = message

    @setchannel.error
    async def setchannel_error(ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send(embed=_embed("You need the Manage Server permission to do that."), ephemeral=True)
        elif isinstance(error, commands.NoPrivateMessage):
            await ctx.send(embed=_embed("This can only be used in a server, not a DM."), ephemeral=True)
        else:
            raise error

    @client.hybrid_command(name="chat", description="Chat with DJ Shinx's AI brain")
    @discord.app_commands.describe(message="What do you want to say?")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def chat(ctx: commands.Context, *, message: str):
        question_line = f"**From {ctx.author.display_name}:** {message}"
        thinking_message = await ctx.send(embed=_embed(_with_question(question_line, "🧠 Thinking")))
        conversation_id = (ctx.channel.id, ctx.author.id)

        # llmask.ask() runs in a worker thread and calls on_status from
        # there -- it just records the latest label, rather than editing
        # Discord directly, since the actual edits (and the animated dots)
        # are driven by _animate_thinking below on the main event loop.
        status = {'label': "🧠 Thinking"}

        def on_status(text: str):
            status['label'] = text.rstrip('.')

        async def animate_thinking():
            dots = 0
            while True:
                await asyncio.sleep(1.5)
                dots = dots % 3 + 1
                try:
                    await thinking_message.edit(
                        embed=_embed(_with_question(question_line, status['label'] + '.' * dots))
                    )
                except discord.HTTPException:
                    pass

        animation_task = asyncio.create_task(animate_thinking())
        try:
            result, chart_path = await asyncio.to_thread(llmask.ask, message, conversation_id, on_status, ctx.author.id)
        finally:
            animation_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await animation_task

        chunks = llmask.chunk_response(result)

        first_embed = _embed(_with_question(question_line, chunks[0]))
        if chart_path:
            # A locally-rendered chart has no public URL to point an embed
            # at -- attach the file itself and reference it via Discord's
            # attachment:// scheme instead, then drop the local temp file
            # now that Discord has its own copy.
            filename = os.path.basename(chart_path)
            first_embed.set_image(url=f"attachment://{filename}")
            await thinking_message.edit(embed=first_embed, attachments=[discord.File(chart_path, filename=filename)])
            try:
                os.remove(chart_path)
            except OSError:
                pass
        else:
            await thinking_message.edit(embed=first_embed)

        for chunk in chunks[1:]:
            await ctx.send(embed=_embed(chunk))

        # If the answer cited a YouTube/X link, also send it as its own
        # plain message so Discord's native preview actually renders --
        # see RICH_PREVIEW_URL_RE above for why that can't happen from
        # inside the embed itself.
        preview_match = RICH_PREVIEW_URL_RE.search(result)
        if preview_match:
            await ctx.send(preview_match.group(0))

    @chat.error
    async def chat_error(ctx: commands.Context, error: commands.CommandError):
        # Ollama requests are serialized behind one lock (see llmask.py),
        # so one person spamming /chat directly slows down everyone else's
        # queue -- this cooldown is what actually protects that, this just
        # reports it instead of leaving the interaction failing silently.
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(
                embed=_embed(f"Slow down a bit — try again in {error.retry_after:.0f}s."),
                ephemeral=True,
            )
        else:
            raise error

    @client.hybrid_command(name="forget", description="Clears your conversation history with DJ Shinx's AI brain")
    async def forget(ctx: commands.Context):
        conversation_id = (ctx.channel.id, ctx.author.id)
        await asyncio.to_thread(llmask.forget, conversation_id)
        await ctx.send(embed=_embed("Alright, clean slate — I've forgotten our conversation so far."))

    @client.hybrid_command(name="forgetme", description="Deletes everything Agent Shinx has remembered about you long-term")
    async def forgetme(ctx: commands.Context):
        deleted = await asyncio.to_thread(memory_db.clear_facts, ctx.author.id)
        if deleted:
            await ctx.send(embed=_embed(f"Done — deleted {deleted} thing{'s' if deleted != 1 else ''} I'd remembered about you."))
        else:
            await ctx.send(embed=_embed("There wasn't anything long-term saved about you to delete."))

    @client.hybrid_command(name="help", description="Lists every command DJ Shinx offers")
    async def help_command(ctx: commands.Context):
        by_category = {category: [] for category in CATEGORY_ORDER}
        for command in client.commands:
            if command.name in HIDDEN_COMMANDS:
                continue
            by_category.setdefault(COMMAND_CATEGORIES.get(command.name, 'Other'), []).append(command)

        pages = []
        for category, group in by_category.items():
            if not group:
                continue
            lines = [f"## {category} Commands", "*(use with / or !)*"]
            for command in sorted(group, key=lambda c: c.name):
                lines.append(f"`{command.name}` — {command.description or 'No description.'}")
            pages.append((category, "\n".join(lines)))

        view = PaginatorView(pages, author_id=ctx.author.id, command_name="help")
        message = await ctx.send(embed=view.embed(), view=view)
        view.message = message

 #=========================--END COMMANDS--===================================#

    async def _broadcast(feature: str, send):
        """Calls send(channel) for every channel configured for this
        feature (via /setchannel) across every guild the bot is in, so a
        single result gets fanned out to everyone who's opted in instead
        of one hardcoded destination."""
        for channel_id in channel_config.get_all_for_feature(feature).values():
            channel = client.get_channel(channel_id)
            if channel is not None:
                await send(channel)

    @tasks.loop(time=datetime.time(hour=13, minute=0, tzinfo=EASTERN))
    async def sotd():
        result = await asyncio.to_thread(bf.recsongs)
        await _broadcast('sotd', lambda channel: channel.send(embed=_embed(result)))

    @tasks.loop(hours=6.0)
    async def new_chapter_announcements():
        berserk_announcement = await asyncio.to_thread(bf.check_berserk_release)
        batman_announcement = await asyncio.to_thread(bf.check_absolute_batman_release)
        if not berserk_announcement and not batman_announcement:
            return

        async def send(channel):
            if berserk_announcement:
                await channel.send(embed=_embed(berserk_announcement))
            if batman_announcement:
                await channel.send(embed=_embed(batman_announcement))

        await _broadcast('manga_comics', send)

    @tasks.loop(minutes=15.0)
    async def f1_updates():
        messages = await asyncio.to_thread(f1.check_f1_updates)
        if not messages:
            return

        async def send(channel):
            for message in messages:
                await channel.send(embed=_embed(message))

        await _broadcast('f1_updates', send)

    @tasks.loop(minutes=30.0)
    async def game_announcements():
        messages = await asyncio.to_thread(game_news.check_game_announcements)
        if not messages:
            return

        async def send(channel):
            for message in messages:
                await channel.send(embed=_embed(message))
                # The embed's own link never unfurls (see ANNOUNCEMENT_URL_RE
                # above), so re-post it as plain content to get a real
                # preview card for the article.
                url_match = ANNOUNCEMENT_URL_RE.search(message)
                if url_match:
                    await channel.send(url_match.group(0))

        await _broadcast('game_announcements', send)

    @client.event
    async def on_ready():
        sotd.start()
        new_chapter_announcements.start()
        f1_updates.start()
        game_announcements.start()
        logger.info(f'{client.user} is now running!')
        await client.tree.sync()

    client.run(TOKEN)


async def sendMessage(message, user_message, is_private):
    try:
        response = responses.getResponse(user_message)
        await message.author.send(response) if is_private else await message.channel.send(response)
    except Exception as e:
        logger.exception(f"Failed to send response message: {e}")

async def sendTopSongs(message):
    songs, artist, rank = await asyncio.to_thread(bf.topsongs)
    await message.channel.send("## The top 5 songs on iTunes right now!")
    for i in range(5):
        await message.channel.send('**Rank: **' + f'{rank[i]}' + '\n' + f'*"{songs[i]}"*' + ', ' + f'{artist[i]}')
    await message.channel.send('Source: https://www.popvortex.com/music/charts/top-100-songs.php')

if __name__ == '__main__':
    run_discord_bot()
    bf.updateSongList()