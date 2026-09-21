import discord
from discord.ext import commands, tasks
import asyncio
import datetime
import logging
from zoneinfo import ZoneInfo
import responses
import botFunctions as bf
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


def _embed(text: str) -> discord.Embed:
    """Wraps plain text in a cyan-bordered embed -- the standard shape for
    every bot response. Existing sports.py/llmask.py output is already
    sized for Discord's 2000-char plain-message limit, well under an
    embed description's 4096-char cap, so no re-chunking is needed."""
    return discord.Embed(description=text, color=EMBED_COLOR)

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

    @client.hybrid_command(name="chat", description="Chat with DJ Shinx's AI brain")
    @discord.app_commands.describe(message="What do you want to say?")
    async def chat(ctx: commands.Context, *, message: str):
        await ctx.send(embed=_embed(f"**From {ctx.author.display_name}:** {message}"))
        thinking_message = await ctx.send(embed=_embed("🧠 Thinking..."))
        conversation_id = (ctx.channel.id, ctx.author.id)

        loop = asyncio.get_running_loop()
        notified = {'shown': False}

        def on_queued():
            if notified['shown']:
                return
            notified['shown'] = True
            asyncio.run_coroutine_threadsafe(
                thinking_message.edit(
                    embed=_embed("⏳ Someone else is chatting with me right now -- you're queued, this might take a bit longer than usual...")
                ),
                loop,
            )

        result = await asyncio.to_thread(llmask.ask, message, conversation_id, on_queued, ctx.author.id)
        chunks = llmask.chunk_response(result)
        await thinking_message.edit(embed=_embed(chunks[0]))
        for chunk in chunks[1:]:
            await ctx.send(embed=_embed(chunk))

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

    @tasks.loop(time=datetime.time(hour=13, minute=0, tzinfo=EASTERN))
    async def sotd():
        channel = client.get_channel(1023430299335532615)
        result = await asyncio.to_thread(bf.recsongs)
        await channel.send(embed=_embed(result))

    @tasks.loop(hours=6.0)
    async def new_chapter_announcements():
        channel = client.get_channel(748287973795168346)
        if channel is None:
            return

        berserk_announcement = await asyncio.to_thread(bf.check_berserk_release)
        if berserk_announcement:
            await channel.send(embed=_embed(berserk_announcement))

        batman_announcement = await asyncio.to_thread(bf.check_absolute_batman_release)
        if batman_announcement:
            await channel.send(embed=_embed(batman_announcement))

    @tasks.loop(minutes=15.0)
    async def f1_updates():
        channel = client.get_channel(1510340061026058472)
        if channel is None:
            return

        messages = await asyncio.to_thread(f1.check_f1_updates)
        for message in messages:
            await channel.send(embed=_embed(message))

    @client.event
    async def on_ready():
        sotd.start()
        new_chapter_announcements.start()
        f1_updates.start()
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