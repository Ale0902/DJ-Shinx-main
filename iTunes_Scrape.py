import requests
from bs4 import BeautifulSoup
import csv
import os

# Folder that contains this script, so file paths work on Windows and Linux
BASE = os.path.dirname(os.path.abspath(__file__))

def updateSongList():
    page = requests.get("https://www.popvortex.com/music/charts/top-100-songs.php")
    soup = BeautifulSoup(page.text, 'html.parser')
    
    list = soup.find(class_="chart-wrapper")
    rows = soup.find_all(class_="title-artist")

    filename = os.path.join(BASE, 'Top_Songs.csv')
    with open(filename, 'w', newline ='') as csvfile:
        f = csv.writer(csvfile)
        f.writerow(['Song', 'Artist', 'Rank'])

        rank = 1
        for row in rows:
            song = row.find(class_="title").text.strip()
            artist = row.find(class_="artist").text.strip()
            f.writerow([song, artist, rank])
            rank += 1