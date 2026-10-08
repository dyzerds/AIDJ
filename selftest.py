"""Offline self-check: builds synthetic songs with known tempo/downbeats, analyses and mixes them.

Run:  python selftest.py      (no internet needed, takes a couple of minutes)
"""
import tempfile
import wave
from pathlib import Path

import numpy as np

import analysis
import mixer

SR = 44100


def make_song(path, bpm, root, bars=48, offset=0.5, seed=0):
    """Drums + bass + chords in 4/4. Intro and outro are drums only. Returns the true beat times."""
    rng = np.random.default_rng(seed)
    beat = 60 / bpm
    n = int((offset + bars * 4 * beat + 2) * SR)
    y = np.zeros(n)
    t = np.arange(int(0.3 * SR)) / SR
    kick = np.sin(2 * np.pi * (45 * t + 2.5 * (1 - np.exp(-t * 40)))) * np.exp(-t * 9)
    hat = np.diff(rng.standard_normal(int(0.05 * SR)), prepend=0) * np.exp(-np.arange(int(0.05 * SR)) / 300) * 0.15
    beats = offset + beat * np.arange(bars * 4)
    for i, b in enumerate(beats):
        k = int(b * SR)
        y[k:k + len(kick)] += kick[:n - k]
        h = int((b + beat / 2) * SR)
        y[h:h + len(hat)] += hat[:max(0, n - h)]
    progression = [0, 7, 9, 5]                 # I - V - vi - IV
    for bar in range(8, bars - 8):
        a, e = int(beats[bar * 4] * SR), int((beats[bar * 4] + 4 * beat) * SR)
        tt = np.arange(e - a) / SR
        note = root + progression[bar % 4]
        f = 440 * 2 ** ((note - 69) / 12)
        chord = sum(np.sin(2 * np.pi * f * r * tt) for r in (1, 2 ** (4 / 12), 2 ** (7 / 12))) * 0.06
        bass = np.sin(2 * np.pi * f / 4 * tt) * np.exp(-tt * 1.5) * 0.35
        y[a:e] += chord * np.minimum(1, tt / 0.01) + bass
    y = (y / np.abs(y).max() * 0.8 * 32767).astype(np.int16)
    with wave.open(str(path), 'wb') as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(np.repeat(y, 2).tobytes())
    return beats


def check_analysis(path, bpm, beats):
    an = analysis.analyze(path)
    assert abs(an['bpm'] - bpm) < 0.1, f"tempo {an['bpm']:.2f} != {bpm}"
    assert an['steady'], 'grid should be steady'
    got = np.array(an['beats'])
    err = np.abs(got[np.argmin(np.abs(got[:, None] - beats[None, :]), axis=0)] - beats)
    assert np.median(err) < 0.006, f'beats off by {np.median(err) * 1000:.1f} ms'
    first_bar = np.array(an['beats'])[an['downbeat']::4]
    assert np.min(np.abs(first_bar - beats[0])) < 0.01, 'downbeat phase is wrong'
    return an


def check_mix(decks, style, position, out):
    for d in decks:   # fresh playback plans
        d.__init__(d.path, d.meta, d.an)
    notes = mixer.plan(decks, style, position)
    times = mixer.render(decks, out)
    assert len(notes) == len(decks) - 1 and all(n['kind'] in mixer.NAMES for n in notes)
    assert times == sorted(times), 'songs must start in order'
    mix = analysis.decode(out)
    assert np.isfinite(mix).all() and mix.size, 'mix is empty or broken'
    peak = 20 * np.log10(np.abs(mix).max())
    assert peak < 0, f'clipping: peak {peak:.2f} dBFS'
    length = mix.shape[1] / SR
    assert times[-1] < length, 'last song starts after the mix ends'
    return notes, times, length


def main():
    tmp = Path(tempfile.mkdtemp(prefix='aidj-test-'))
    songs = [('a.wav', 124, 57), ('b.wav', 126, 64), ('c.wav', 100, 62)]
    decks = []
    for i, (name, bpm, root) in enumerate(songs):
        beats = make_song(tmp / name, bpm, root, seed=i)
        an = check_analysis(tmp / name, bpm, beats)
        decks.append(mixer.Deck(tmp / name, {'title': name, 'artist': 'test'}, an))
        print(f'analysis ok: {name} {an["bpm"]:.2f} BPM, key {an["key"]}')

    # A beat-matched blend must keep the kicks of both songs on top of each other.
    notes, times, _ = check_mix(decks[:2], 'blend', 'end', tmp / 'blend.mp3')
    assert notes[0]['kind'] == 'blend', notes
    mix = analysis.decode(tmp / 'blend.mp3').mean(0)
    A = decks[0]
    grid = np.array([times[0] + A.out_time(b) for b in A.beats if A.src_in <= b <= A.src_end])
    t0 = times[1]
    grid = grid[(grid > t0 + 1) & (grid < t0 + A.exit['dur'] - 1)]
    env = np.abs(np.diff(mix))
    hits = [np.argmax(env[int((g - 0.05) * SR):int((g + 0.05) * SR)]) / SR - 0.05 for g in grid]
    assert np.median(np.abs(hits)) < 0.006, f'kicks in the blend are {np.median(np.abs(hits)) * 1000:.1f} ms apart'
    print(f'blend ok: kicks aligned within {np.median(np.abs(hits)) * 1000:.1f} ms')

    for style in mixer.STYLES:
        for position in ('end', 'middle'):
            notes, times, length = check_mix(decks, style, position, tmp / f'{style}-{position}.mp3')
            print(f'mix ok: {style:6s} {position:6s} {length:6.1f}s  ' + ', '.join(n['name'] for n in notes))

    check_mix(decks[:1], 'auto', 'end', tmp / 'single.mp3')
    print('single-song mix ok')

    # A very short song (an interlude) in the middle of the list must not break the planner.
    make_song(tmp / 'short.wav', 126, 60, bars=12, seed=9)
    short = mixer.Deck(tmp / 'short.wav', {'title': 'short', 'artist': 'test'}, analysis.analyze(tmp / 'short.wav'))
    for style in ('blend', 'auto'):
        for position in ('end', 'middle'):
            check_mix([decks[0], short, decks[1]], style, position, tmp / f'short-{style}-{position}.mp3')
    print('short-song mixes ok')
    print('ALL CHECKS PASSED')


if __name__ == '__main__':
    main()
