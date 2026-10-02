import requests
from bs4 import BeautifulSoup
import csv
import os
import uuid

# Folder that contains this script, so file paths work on Windows and Linux
BASE = os.path.dirname(os.path.abspath(__file__))

def updateSongList():
    """Rewrites Top_Songs.csv from the chart page. Raises if the page can't
    be fetched or has no songs on it -- the old file is only replaced once
    a complete new list is in hand, so a failed scrape leaves it intact."""
    page = requests.get("https://www.popvortex.com/music/charts/top-100-songs.php", timeout=10)
    page.raise_for_status()
    soup = BeautifulSoup(page.text, 'html.parser')

    songs = []
    for row in soup.find_all(class_="title-artist"):
        title, artist = row.find(class_="title"), row.find(class_="artist")
        if title and artist:
            songs.append((title.text.strip(), artist.text.strip()))
    if not songs:
        raise ValueError("no songs found on the chart page")

    # A uniquely named temp file renamed over the real one, so two
    # /top5songs at once can't interleave their writes into one file.
    filename = os.path.join(BASE, 'Top_Songs.csv')
    tmp_path = f"{filename}.{uuid.uuid4().hex}.tmp"
    with open(tmp_path, 'w', newline='', encoding='utf-8') as csvfile:
        f = csv.writer(csvfile)
        f.writerow(['Song', 'Artist', 'Rank'])
        for rank, (song, artist) in enumerate(songs, start=1):
            f.writerow([song, artist, rank])
    os.replace(tmp_path, filename)
