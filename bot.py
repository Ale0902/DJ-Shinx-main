import discord
from discord.ext import commands, tasks
import asyncio
import datetime
from zoneinfo import ZoneInfo
import responses
import botFunctions as bf
import llmask
import sports
import f1
import os
from dotenv import load_dotenv

EASTERN = ZoneInfo("America/New_York")

# Folder that contains this script, so file paths work on Windows and Linux
BASE = os.path.dirname(os.path.abspath(__file__))

ENV_PATH = os.path.join(BASE, 'code.env')
load_dotenv(dotenv_path=ENV_PATH)
TOKEN = os.getenv('DISCORD_TOKEN')

if not TOKEN:
    raise RuntimeError(f"DISCORD_TOKEN not found. Make sure it is set in {os.path.abspath(ENV_PATH)}")


class SoccerPaginator(discord.ui.View):
    """Lets the /soccer command show one competition per page, flipped
    through with buttons instead of needing a separate command per league."""

    def __init__(self, pages, author_id):
        super().__init__(timeout=180)
        self.pages = pages  # list of (title, page_text)
        self.index = 0
        self.author_id = author_id
        self.message = None
        self._update_buttons()

    def _update_buttons(self):
        self.previous_button.disabled = self.index == 0
        self.next_button.disabled = self.index == len(self.pages) - 1

    def content(self):
        title, body = self.pages[self.index]
        return f"{body}\n\n*Page {self.index + 1}/{len(self.pages)} — {title}*"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only the person who ran /soccer can flip pages.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index -= 1
        self._update_buttons()
        await interaction.response.edit_message(content=self.content(), view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index += 1
        self._update_buttons()
        await interaction.response.edit_message(content=self.content(), view=self)

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
    client = commands.Bot(command_prefix='/', intents=intents)

 #=========================--SLASH COMMANDS--================================#

    @client.tree.command(name="hello", description="Says hello")
    async def hello(ctx: discord.Interaction):
        await ctx.response.send_message("Hello!")

    @client.tree.command(name="recsong", description="Recommends a random song from the server's list")
    async def recsong(ctx: discord.Interaction):
        await ctx.response.defer()
        result = await asyncio.to_thread(bf.recsongs)
        await ctx.followup.send(result)

    @client.tree.command(name="top5songs", description="Top 5 songs on iTunes charts!")
    async def top5(ctx: discord.Interaction):
        await ctx.response.defer()
        result = await asyncio.to_thread(bf.topsongs)
        await ctx.followup.send(result)

    @client.tree.command(name='rolld6', description='Rolls a D6 dice')
    async def rolld6(ctx: discord.Interaction):
        await ctx.response.send_message(responses.rolld6())

    @client.tree.command(name='rolld20', description='Rolls a D20 dice')
    async def rolld20(ctx: discord.Interaction):
        await ctx.response.send_message(responses.rolld20())

    @client.tree.command(name='ping', description='pingpong')
    async def ping(ctx: discord.Interaction):
        await ctx.response.send_message(responses.ping())

    @client.tree.command(name="coin_flip", description="Flip a coin!")
    async def coinflip(ctx: discord.Interaction):
        await ctx.response.send_message(responses.coinflip())

    @client.tree.command(name="8ball", description="Shakes an eight ball")
    async def eightball(ctx: discord.Interaction):
        await ctx.response.send_message(bf.eightball())

    @client.tree.command(name="nfl", description="This week's NFL games and live scores")
    async def nfl(ctx: discord.Interaction):
        await ctx.response.defer()
        result = await asyncio.to_thread(sports.nfl_synopsis)
        for chunk in llmask.chunk_response(result):
            await ctx.followup.send(chunk)

    @client.tree.command(name="cfb", description="This week's ranked college football games, with a South Florida spotlight")
    async def cfb(ctx: discord.Interaction):
        await ctx.response.defer()
        result = await asyncio.to_thread(sports.cfb_synopsis)
        for chunk in llmask.chunk_response(result):
            await ctx.followup.send(chunk)

    @client.tree.command(name="mlb", description="This week's MLB series and their records")
    async def mlb(ctx: discord.Interaction):
        await ctx.response.defer()
        result = await asyncio.to_thread(sports.mlb_series_synopsis)
        for chunk in llmask.chunk_response(result):
            await ctx.followup.send(chunk)

    @client.tree.command(name="soccer", description="This week's matches, one page per competition")
    async def soccer(ctx: discord.Interaction):
        await ctx.response.defer()
        pages = await asyncio.to_thread(sports.soccer_pages)
        view = SoccerPaginator(pages, author_id=ctx.user.id)
        message = await ctx.followup.send(view.content(), view=view)
        view.message = message

    @client.tree.command(name="premtable", description="Current Premier League standings")
    async def premtable(ctx: discord.Interaction):
        await ctx.response.defer()
        messages = await asyncio.to_thread(sports.premier_league_table)
        for message in messages:
            await ctx.followup.send(message)

    @client.tree.command(name="laligatable", description="Current La Liga standings")
    async def laligatable(ctx: discord.Interaction):
        await ctx.response.defer()
        messages = await asyncio.to_thread(sports.la_liga_table)
        for message in messages:
            await ctx.followup.send(message)

    @client.tree.command(name="ucltable", description="Current Champions League standings or bracket")
    async def ucltable(ctx: discord.Interaction):
        await ctx.response.defer()
        messages = await asyncio.to_thread(sports.ucl_table)
        for message in messages:
            await ctx.followup.send(message)

    @client.tree.command(name="f1", description="Is it F1 race weekend right now?")
    async def f1cmd(ctx: discord.Interaction):
        await ctx.response.defer()
        result = await asyncio.to_thread(f1.f1_status)
        await ctx.followup.send(result)

    @client.tree.command(name="f1standings", description="Current F1 drivers' and constructors' championship standings")
    async def f1standings(ctx: discord.Interaction):
        await ctx.response.defer()
        messages = await asyncio.to_thread(f1.f1_standings)
        for message in messages:
            await ctx.followup.send(message)

    @client.tree.command(name="status", description="Check the Minecraft server status")
    async def status(ctx: discord.Interaction):
        await ctx.response.defer()
        result = await asyncio.to_thread(bf.mc_status)
        await ctx.followup.send(result)

    @client.tree.command(name="ask", description="Ask DJ Shinx's AI brain a question")
    @discord.app_commands.describe(question="What do you want to ask?")
    async def ask(ctx: discord.Interaction, question: str):
        await ctx.response.send_message(f"**Question:** {question}")
        result = await asyncio.to_thread(llmask.ask, question)
        for chunk in llmask.chunk_response(result):
            await ctx.followup.send(chunk)

 #=========================--END SLASH COMMANDS--============================#

    @tasks.loop(time=datetime.time(hour=13, minute=0, tzinfo=EASTERN))
    async def sotd():
        channel = client.get_channel(1023430299335532615)
        result = await asyncio.to_thread(bf.recsongs)
        await channel.send(result)

    @tasks.loop(hours=6.0)
    async def new_chapter_announcements():
        channel = client.get_channel(748287973795168346)
        if channel is None:
            return

        berserk_announcement = await asyncio.to_thread(bf.check_berserk_release)
        if berserk_announcement:
            await channel.send(berserk_announcement)

        batman_announcement = await asyncio.to_thread(bf.check_absolute_batman_release)
        if batman_announcement:
            await channel.send(batman_announcement)

    @tasks.loop(minutes=15.0)
    async def f1_updates():
        channel = client.get_channel(1510340061026058472)
        if channel is None:
            return

        messages = await asyncio.to_thread(f1.check_f1_updates)
        for message in messages:
            await channel.send(message)

    @client.event
    async def on_ready():
        sotd.start()
        new_chapter_announcements.start()
        f1_updates.start()
        print(f'{client.user} is now running!')
        await client.tree.sync()

    client.run(TOKEN)


async def sendMessage(message, user_message, is_private):
    try:
        response = responses.getResponse(user_message)
        await message.author.send(response) if is_private else await message.channel.send(response)
    except Exception as e:
        print(e)

async def sendTopSongs(message):
    songs, artist, rank = await asyncio.to_thread(bf.topsongs)
    await message.channel.send("## The top 5 songs on iTunes right now!")
    for i in range(5):
        await message.channel.send('**Rank: **' + f'{rank[i]}' + '\n' + f'*"{songs[i]}"*' + ', ' + f'{artist[i]}')
    await message.channel.send('Source: https://www.popvortex.com/music/charts/top-100-songs.php')

if __name__ == '__main__':
    run_discord_bot()
    bf.updateSongList()