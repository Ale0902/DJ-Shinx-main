import discord
from discord.ext import commands, tasks
import asyncio
import responses
import botFunctions as bf
import os
from dotenv import load_dotenv

ENV_PATH = os.path.join(os.path.dirname(__file__), '..', 'code.env')
load_dotenv(dotenv_path=ENV_PATH)
TOKEN = os.getenv('DISCORD_TOKEN')

if not TOKEN:
    raise RuntimeError(f"DISCORD_TOKEN not found. Make sure it is set in {os.path.abspath(ENV_PATH)}")

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

    @client.tree.command(name="status", description="Check the Minecraft server status")
    async def status(ctx: discord.Interaction):
        await ctx.response.defer()
        result = await asyncio.to_thread(bf.mc_status)
        await ctx.followup.send(result)

 #=========================--END SLASH COMMANDS--============================#

    @tasks.loop(hours=24.0)
    async def sotd():
        channel = client.get_channel(1023430299335532615)
        result = await asyncio.to_thread(bf.recsongs)
        await channel.send(result)

    @client.event
    async def on_ready():
        sotd.start()
        print(f'{client.user} is now running!')
        await client.tree.sync()

    @client.event
    async def on_message(message):
        if message.author == client.user:
            return
        username = str(message.author)
        user_message = str(message.content)
        channel = str(message.channel)
        print(f'{username} said: {user_message} in channel: ({channel})')

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