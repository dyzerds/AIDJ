"""Read a public Spotify playlist and download each song's audio from YouTube Music."""
import difflib
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
import yt_dlp
from ytmusicapi import YTMusic

LIBRARY = Path(__file__).parent / 'library'
AUDIO_EXTS = ('.webm', '.m4a', '.opus', '.mp3', '.ogg', '.aac', '.flac', '.wav')
URL_RE = re.compile(r'(?:open\.spotify\.com/(?:intl-[\w-]+/)?(?:embed/)?|spotify:)(playlist|album)[/:]([A-Za-z0-9]{22})')
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36'
# Words that mean "not the original studio version" unless the Spotify title has them too.
BAD_WORDS = ('karaoke', 'instrumental', 'cover', 'remix', 'live', 'sped up', 'slowed', 'nightcore',
             '8d', 'reverb', 'acapella', 'a cappella', 'lyrics', 'tutorial', 'reaction')


def read_playlist(url):
    """Return (playlist name, [track dicts]) using Spotify's public embed page (no API key needed)."""
    m = URL_RE.search(url or '')
    if not m:
        raise ValueError("That doesn't look like a Spotify playlist link. "
                         "It should look like https://open.spotify.com/playlist/...")
    kind, pid = m.groups()
    r = requests.get(f'https://open.spotify.com/embed/{kind}/{pid}', headers={'User-Agent': UA}, timeout=20)
    if r.status_code in (400, 404):
        raise ValueError('Spotify could not find that playlist. Make sure it is public.')
    r.raise_for_status()
    data = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', r.text, re.S)
    try:
        entity = json.loads(data.group(1))['props']['pageProps']['state']['data']['entity']
    except (AttributeError, KeyError, TypeError, ValueError):
        raise RuntimeError('Could not read the playlist from Spotify. Make sure it is public and try again.')
    tracks = [{'id': t['uri'].split(':')[-1],
               'title': t.get('title') or 'Unknown',
               'artist': (t.get('subtitle') or '').replace(' ', ' '),
               'duration': (t.get('duration') or 0) / 1000}
              for t in entity.get('trackList') or [] if str(t.get('uri', '')).startswith('spotify:track:')]
    if not tracks:
        raise ValueError('That playlist has no songs we can use.')
    return entity.get('name') or entity.get('title') or 'My playlist', tracks


def _norm(s):
    return re.sub(r'[^a-z0-9 ]+', ' ', (s or '').lower()).strip()


def _score(cand, track):
    """How likely a YouTube Music result is the same recording as the Spotify track."""
    title, want = _norm(cand.get('title')), _norm(track['title'])
    score = 3 * difflib.SequenceMatcher(None, title, want).ratio()
    artists = [_norm(a.get('name')) for a in cand.get('artists') or []]
    if any(a and a in _norm(track['artist']) for a in artists):
        score += 2
    dur = cand.get('duration_seconds')
    if dur and track['duration']:
        diff = abs(dur - track['duration'])
        score += 3 if diff <= 3 else 2 if diff <= 8 else 0 if diff <= 20 else -3
    for w in BAD_WORDS:
        if re.search(rf'\b{w}\b', title) and not re.search(rf'\b{w}\b', want):
            score -= 4
    return score


_local = threading.local()


class _Silent:
    """yt-dlp logger that keeps the console clean (failures are reported by download_all)."""
    def debug(self, msg): pass
    info = warning = error = debug


def _find_video(track):
    yt = getattr(_local, 'yt', None) or YTMusic()
    _local.yt = yt
    query = f"{track['artist'].split(',')[0]} {track['title']}"
    best = None
    for flt in ('songs', 'videos'):
        for cand in yt.search(query, filter=flt, limit=10):
            if cand.get('videoId'):
                s = _score(cand, track)
                if best is None or s > best[0]:
                    best = (s, cand['videoId'])
        if best and best[0] >= 5:
            break
    return best[1] if best and best[0] >= 2 else None


def cached_file(track_id):
    return next((p for p in LIBRARY.glob(f'{track_id}.*') if p.suffix in AUDIO_EXTS), None)


def download(track):
    """Download one track (or reuse the cached file). Returns the file path or None."""
    LIBRARY.mkdir(exist_ok=True)
    found = cached_file(track['id'])
    if found:
        return found
    vid = _find_video(track)
    if not vid:
        return None
    opts = {'format': 'bestaudio/best', 'outtmpl': str(LIBRARY / f"{track['id']}.%(ext)s"),
            'quiet': True, 'no_warnings': True, 'noprogress': True, 'noplaylist': True, 'retries': 3,
            'logger': _Silent(),
            # YouTube needs a JavaScript runtime; use whichever of Deno / Node.js is installed.
            'js_runtimes': {'deno': {}, 'node': {}}}
    for attempt in range(3):  # YouTube sometimes answers 403 at random; a retry usually works
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.extract_info(f'https://music.youtube.com/watch?v={vid}', download=True)
            break
        except yt_dlp.utils.DownloadError:
            if attempt == 2:
                raise
            time.sleep(2 + 3 * attempt)
    return cached_file(track['id'])


def download_all(tracks, on_progress=lambda done, total, title: None, workers=4):
    """Download tracks in parallel. Returns a list of file paths (None where a song was not found)."""
    unique = {t['id']: t for t in tracks}   # a song listed twice is downloaded once
    found = {}
    with ThreadPoolExecutor(workers) as pool:
        futures = {pool.submit(download, t): t for t in unique.values()}
        for done, fut in enumerate(as_completed(futures), 1):
            t = futures[fut]
            try:
                found[t['id']] = fut.result()
            except Exception as e:  # one bad song must not kill the whole mix
                print(f"[download] {t['artist']} - {t['title']}: {e}")
            on_progress(done, len(unique), t['title'])
    return [found.get(t['id']) for t in tracks]
