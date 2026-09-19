import iTunes_Scrape
import csv
import os
import random
from mcstatus import JavaServer

# Folder that contains this script, so file paths work on Windows and Linux
BASE = os.path.dirname(os.path.abspath(__file__))

def topsongs():
        iTunes_Scrape.updateSongList()
        with open(os.path.join(BASE, 'Top_Songs.csv'), 'r') as csvfile:
            csv_reader = csv.reader(csvfile, delimiter=',')
            count = 1
            song_name =[]
            artist =[]
            rank =[]

            for row in csv_reader:
                song_name.append(row[0])
                artist.append(row[1])
                rank.append(row[2])
                count += 1

                if(count > 6):
                    break   
        return ("## The Top 5 Songs on iTunes Right Now!\n"
                f"**Rank:** {rank[1]}\n*{song_name[1]}*, {artist[1]}\n"
                f"**Rank:** {rank[2]}\n*{song_name[2]}*, {artist[2]}\n"
                f"**Rank:** {rank[3]}\n*{song_name[3]}*, {artist[3]}\n"
                f"**Rank:** {rank[4]}\n*{song_name[4]}*, {artist[4]}\n"
                f"**Rank:** {rank[5]}\n*{song_name[5]}*, {artist[5]}\n"
                "\nSource: https://www.popvortex.com/music/charts/top-100-songs.php"
                )

def recsongs():
     with open(os.path.join(BASE, 'SOTD.csv'), 'r', encoding='utf-8') as csvfile:
        csv_reader = csv.reader(csvfile, delimiter=',')
        rows = list(csv_reader)
        rand = random.randrange(1,len(list(rows)))
        count = 0
        songname = []
        artist = []
        link = []
        name = []

        for row in rows:
            count += 1
            if(count == rand):
                songname = row[0]
                artist= row[1]
                link = row[2]
                name =row[3]
                #rows.remove(row)
                break

        return ("### SOTD!:\n"
                f"\n**Song:** {songname}\n"
                f"**Artist:** {artist}\n"
                f"**Submitted by:** {name}\n"
                f"\nLink: {link}"
                )
     
def eightball():
     roll = random.randint(1,20)
     if(roll==1):
          return 'It is certain'
     elif(roll==2):
          return 'Reply hazy, try again'
     elif(roll==3):
          return "Don't count on it"
     elif(roll==4):
          return 'It is decidedly so'
     elif(roll==5):
          return 'Ask again later'
     elif(roll==6):
          return 'My reply is no'
     elif(roll==7):
          return 'Without a doubt'
     elif(roll==8):
          return 'Better not tell you now'
     elif(roll==9):
          return 'My sources say no'
     elif(roll==10):
          return 'Yes definitely'
     elif(roll==11):
          return 'Cannot predict now'
     elif(roll==12):
          return 'Outlook not so good'
     elif(roll==13):
          return 'You may rely on it'
     elif(roll==14):
          return 'Concentrate and ask again'
     elif(roll==15):
          return 'Very doubtful'
     elif(roll==16):
          return 'As I see it, yes'
     elif(roll==17):
          return 'Most likely'
     elif(roll==18):
          return 'Outlook good'
     elif(roll==19):
          return 'Yes'
     elif(roll==20):
          return 'Signs point to yes'
     else:
          return 'Error, try again.'

def mc_status():
    server_ip = 'listened-refried.tun.ply.gg'
    try:
        server = JavaServer.lookup(server_ip, timeout=5)
        status = server.status()

        player_count = status.players.online
        max_players = status.players.max

        # Build player list if any are online and names are available
        if player_count > 0 and status.players.sample:
            player_names = [p.name for p in status.players.sample]
            players_str = '\n'.join(f'  - {name}' for name in player_names)
            player_section = f"**Online Players ({player_count}/{max_players}):**\n{players_str}"
        elif player_count > 0:
            # Server online but hides player names
            player_section = f"**Online Players:** {player_count}/{max_players} (names hidden by server)"
        else:
            player_section = f"**Online Players:** 0/{max_players} — Nobody is on right now!"

        return (
            f"🟢 **{server_ip} is ONLINE!**\n"
            f"{player_section}"
        )

    except Exception:
        return (
            f"🔴 **{server_ip} is OFFLINE** (or unreachable).\n"
            "The server may be down or restarting. Try again later!"
        )