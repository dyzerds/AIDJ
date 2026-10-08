"""AIDJ web app: paste a Spotify playlist, pick a transition style, get a DJ mix.

Run:  python app.py   then open http://127.0.0.1:5050
"""
import logging
import re
import sys
import threading
import traceback
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
from flask import Flask, jsonify, request, send_from_directory

import analysis
import downloader
import mixer

ROOT = Path(__file__).parent
MIXES = ROOT / 'mixes'
PORT = 5050

# Song titles can contain any character; never let the console's code page crash a mix.
for stream in (sys.stdout, sys.stderr):
    stream.reconfigure(errors='replace')

app = Flask(__name__, static_folder=None)
logging.getLogger('werkzeug').setLevel(logging.ERROR)   # no log line for every progress poll
jobs = {}
worker = ThreadPoolExecutor(1)   # one mix at a time; extra requests wait in line


@app.get('/')
def index():
    return send_from_directory(ROOT / 'static', 'index.html')


@app.post('/api/mix')
def start_mix():
    data = request.get_json(silent=True)
    data = data if isinstance(data, dict) else {}
    url = str(data.get('url', '')).strip()
    style, position = data.get('style', 'auto'), data.get('position', 'end')
    if style not in mixer.STYLES or position not in ('end', 'middle'):
        return jsonify(error='Unknown transition option.'), 400
    if not downloader.URL_RE.search(url):
        return jsonify(error="That doesn't look like a Spotify playlist link. "
                             "It should look like https://open.spotify.com/playlist/..."), 400
    jid = uuid.uuid4().hex[:12]
    jobs[jid] = {'state': 'queued', 'progress': 0, 'message': 'Waiting for the previous mix to finish…'}
    worker.submit(run_job, jid, url, style, position)
    return jsonify(id=jid)


@app.get('/api/jobs/<jid>')
def job_status(jid):
    job = jobs.get(jid)
    return jsonify(dict(job)) if job else (jsonify(error='Unknown job'), 404)


@app.get('/mixes/<path:name>')
def mix_file(name):
    return send_from_directory(MIXES, name)


def _slug(text):
    return re.sub(r'[^A-Za-z0-9]+', '-', text).strip('-')[:40] or 'mix'


def run_job(jid, url, style, position):
    job = jobs[jid]

    def step(progress, message):
        job.update(state='running', progress=round(progress, 3), message=message)

    try:
        step(0.01, 'Reading your playlist…')
        name, tracks = downloader.read_playlist(url)
        paths = downloader.download_all(
            tracks, lambda done, total, title: step(0.02 + 0.43 * done / total, f'Downloading songs ({done}/{total})'))

        decks, skipped = [], []
        for i, (track, path) in enumerate(zip(tracks, paths)):
            step(0.45 + 0.2 * i / len(tracks), f'Listening to “{track["title"]}” ({i + 1}/{len(tracks)})')
            if path is None:
                skipped.append(track)
                continue
            try:
                decks.append(mixer.Deck(path, track, analysis.analyze_cached(path)))
            except Exception:
                traceback.print_exc()
                skipped.append(track)
        if not decks:
            raise RuntimeError('None of the songs could be found on YouTube Music, so there is nothing to mix.')

        step(0.65, 'Planning the transitions…')
        notes = mixer.plan(decks, style, position)
        MIXES.mkdir(exist_ok=True)
        fname = f'{_slug(name)}-{style}-{position}-{jid[:6]}.mp3'
        times = mixer.render(decks, MIXES / fname, lambda i, n, title: step(
            0.67 + 0.33 * i / n, f'Mixing “{title}” ({i + 1}/{n})'))

        job.update(state='done', progress=1.0, message='Your mix is ready!', result={
            'name': name,
            'file': f'/mixes/{fname}',
            'download_name': f'{name} (AIDJ mix).mp3',
            'tracks': [{'title': d.meta['title'], 'artist': d.meta['artist'], 'time': round(t, 2),
                        'bpm': round(d.an['bpm']), 'key': d.an['camelot']} for d, t in zip(decks, times)],
            'transitions': notes,
            'skipped': [{'title': t['title'], 'artist': t['artist']} for t in skipped],
        })
    except requests.ConnectionError:
        job.update(state='error', message='Could not reach the internet. Check your connection and try again.')
    except (ValueError, RuntimeError) as e:
        traceback.print_exc()
        job.update(state='error', message=str(e))
    except Exception as e:
        traceback.print_exc()
        job.update(state='error', message=f'Something went wrong while mixing: {e}')


if __name__ == '__main__':
    print(f'AIDJ is running at http://127.0.0.1:{PORT}  (press Ctrl+C to stop)')
    if '--no-browser' not in sys.argv:
        threading.Timer(1.5, lambda: webbrowser.open(f'http://127.0.0.1:{PORT}')).start()
    app.run(host='127.0.0.1', port=PORT, threaded=True)
