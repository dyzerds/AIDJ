"""AIDJ web app: paste a Spotify playlist, pick a transition style, get a DJ mix.

Run:  python app.py   then open http://127.0.0.1:5050
"""
import logging
import re
import socket
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
import comments
import downloader
import mixer

ROOT = Path(__file__).parent
MIXES = ROOT / 'mixes'
PORT = 5050
# The web page is also published on GitHub Pages. Let that page (and only that page) talk to this app.
# A fork published under another account adds its own https://<name>.github.io address here.
PAGES_ORIGINS = {'https://dyzerds.github.io'}

# Song titles can contain any character; never let the console's code page crash a mix.
for stream in (sys.stdout, sys.stderr):
    stream.reconfigure(errors='replace')

app = Flask(__name__, static_folder=None)
logging.getLogger('werkzeug').setLevel(logging.ERROR)   # no log line for every progress poll
jobs = {}
worker = ThreadPoolExecutor(1)   # one mix at a time; extra requests wait in line


@app.get('/')
def index():
    return send_from_directory(ROOT, 'index.html')


@app.after_request
def allow_pages(response):
    origin = request.headers.get('Origin')
    if origin in PAGES_ORIGINS:
        response.headers['Access-Control-Allow-Origin'] = origin
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        response.headers['Access-Control-Allow-Private-Network'] = 'true'   # a public site calling 127.0.0.1
        response.vary.add('Origin')
    return response


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
    worker.submit(run_job, jid, url, style, position, _clean_comments(data.get('comments')))
    return jsonify(id=jid)


@app.post('/api/interpret')
def interpret_comment():
    """Tell the listener right away what the AI understood from a comment (applied on the next re-mix)."""
    data = request.get_json(silent=True)
    text = data.get('text') if isinstance(data, dict) else None
    if not isinstance(text, str) or not text.strip():
        return jsonify(error='Write a comment first.'), 400
    _, said = comments.interpret(text.strip()[:300])
    return jsonify(ok=bool(said), reply=comments.reply(said))


@app.get('/api/jobs/<jid>')
def job_status(jid):
    job = jobs.get(jid)
    return jsonify(dict(job)) if job else (jsonify(error='Unknown job'), 404)


@app.get('/mixes/<path:name>')
def mix_file(name):
    # ?download=<name> saves the file: the page's download attribute is ignored when it runs on GitHub Pages.
    download = request.args.get('download')
    return send_from_directory(MIXES, name, as_attachment=bool(download), download_name=download or None)


def _slug(text):
    return re.sub(r'[^A-Za-z0-9]+', '-', text).strip('-')[:40] or 'mix'


def _clean_comments(raw):
    """{'fromTrackId>toTrackId': ['comment', ...]} from the browser, trimmed to sane sizes."""
    if not isinstance(raw, dict):
        return {}
    out = {}
    for key, texts in list(raw.items())[:200]:
        if isinstance(key, str) and isinstance(texts, list):
            texts = [t.strip()[:300] for t in texts if isinstance(t, str) and t.strip()][:10]
            if texts:
                out[key[:100]] = texts
    return out


def run_job(jid, url, style, position, feedback):
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

        step(0.65, 'Planning the transitions with your comments…' if feedback else 'Planning the transitions…')
        keys = [f"{a.meta['id']}>{b.meta['id']}" for a, b in zip(decks, decks[1:])]
        merged = [comments.merge(feedback.get(key, [])) for key in keys]
        notes = mixer.plan(decks, style, position, [changes for changes, _ in merged])
        for note, key, (_, said) in zip(notes, keys, merged):
            note.update(key=key, comments=said)
        MIXES.mkdir(exist_ok=True)
        fname = f'{_slug(name)}-{style}-{position}-{jid[:6]}.mp3'
        times = mixer.render(decks, MIXES / fname, lambda i, n, title: step(
            0.67 + 0.33 * i / n, f'Mixing “{title}” ({i + 1}/{n})'))

        job.update(state='done', progress=1.0, message='Your mix is ready!', result={
            'name': name,
            'file': f'/mixes/{fname}',
            'download_name': f'{name} (AIDJ mix).mp3',
            'tracks': [{'id': d.meta['id'], 'title': d.meta['title'], 'artist': d.meta['artist'], 'time': round(t, 2),
                        'bpm': round(d.an['bpm']), 'key': d.an['camelot']} for d, t in zip(decks, times)],
            'transitions': notes,
            'request': {'url': url, 'style': style, 'position': position},
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


def _port_owner():
    """None if the port is free, 'aidj' if AIDJ already runs there, 'other' for another program.
    (On Windows a second server could silently share the port, so check before starting.)"""
    with socket.socket() as s:
        if s.connect_ex(('127.0.0.1', PORT)):
            return None
    try:
        return 'aidj' if requests.get(f'http://127.0.0.1:{PORT}/api/jobs/-', timeout=3).json().get('error') == 'Unknown job' else 'other'
    except (requests.RequestException, ValueError, AttributeError):
        return 'other'


if __name__ == '__main__':
    url = f'http://127.0.0.1:{PORT}'
    owner = _port_owner()
    if owner == 'other':
        sys.exit(f'Port {PORT} is used by another program. Close it, or change PORT in app.py.')
    if owner == 'aidj':
        print(f'AIDJ is already running at {url}, so this window is not needed. '
              f'(Just updated AIDJ? Close the other AIDJ window first, then start it again.)')
    else:
        print(f'AIDJ is running at {url}  (press Ctrl+C to stop)')
    if '--no-browser' not in sys.argv:
        threading.Timer(1.5 if owner is None else 0, lambda: webbrowser.open(url)).start()
    if owner is None:
        app.run(host='127.0.0.1', port=PORT, threaded=True)
