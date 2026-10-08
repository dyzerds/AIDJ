"""Offline self-check: builds synthetic songs with known tempo/downbeats, analyses and mixes them.

Run:  python selftest.py      (no internet needed, takes a couple of minutes)
"""
import tempfile
import wave
from pathlib import Path

import numpy as np

import analysis
import comments
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


def check_mix(decks, style, position, out, overrides=None):
    for d in decks:   # fresh playback plans
        d.__init__(d.path, d.meta, d.an)
    notes = mixer.plan(decks, style, position, overrides)
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


def check_comments():
    """The comment interpreter must turn everyday phrases into the right mixing changes."""
    cases = {
        'make it longer': {'length': [('mul', 2.0)]},
        'a bit shorter': {'length': [('mul', 0.75)]},
        '16 bars': {'length': [('abs', 16.0, 'bars')]},
        'start 8 bars earlier': {'shift': [(-8.0, 'bars')]},
        'mix it 10 seconds later': {'shift': [(10.0, 'seconds')]},
        'shorten it by 4 bars': {'length': [('add', -4.0, 'bars')]},
        'use an echo instead': {'kind': 'echo'},
        'no spinback please': {'avoid': ['spin']},
        "I don't like the echo, use a cut": {'avoid': ['echo'], 'kind': 'cut'},
        'use an echo instead of the spinback': {'avoid': ['spin'], 'kind': 'echo'},
        'start the next song at the chorus': {'enter': 'drop'},
        'skip the intro': {'enter': 'drop'},
        'too abrupt': {'mood': 'smooth'},
        'needs more energy': {'mood': 'punchy'},
        'the beats are out of sync': {'sync': True},
        'next song is too loud': {'volume': -3.0},
        'do it in the middle of the song': {'position': 'middle'},
        'let the song finish': {'position': 'end'},
        'less echo': {'echo': 'short'},
        'cut the outro': {'shift': [(-8, 'bars')]},
        "don't cut the song short": {'shift': [(8, 'bars')]},
        'too much bass, muddy': {'mood': 'clean'},
        'you decide': {'kind': 'auto'},
        'hello there': {},
    }
    for text, want in cases.items():
        got, said = comments.interpret(text)
        assert got == want, f'{text!r}: {got} != {want}'
        assert bool(said) == bool(want), f'{text!r}: summary {said}'
    assert comments.merge(['use echo', 'reset', 'longer'])[0] == {'length': [('mul', 2.0)]}
    assert comments.merge(['use spinback', 'you decide'])[0] == {}
    assert comments.merge(['no echo', 'use echo'])[0].get('kind') == 'echo'
    assert 'kind' not in comments.merge(['use echo', 'no echo'])[0]
    assert comments.merge(['louder', 'louder', 'louder', 'louder'])[0]['volume'] == 9.0
    assert comments.merge(['the beats clash', 'use a blend'])[0] == {'kind': 'blend'}
    assert comments.merge(['use a blend', 'the beats clash'])[0] == {'sync': True}
    print(f'comments ok: {len(cases)} phrases understood correctly')


def check_plan_follows_comments(decks):
    """Every kind of comment must change the planned transition the way the listener asked."""
    a, b, c = decks

    def plan(pair, style='auto', position='end', ov=None):
        for d in pair:
            d.__init__(d.path, d.meta, d.an)
        notes = mixer.plan(pair, style, position, [ov] if ov else None)
        return notes[0], pair

    base, _ = plan([a, b], 'blend')
    assert base['kind'] == 'blend' and not base['edited']
    assert plan([a, b], ov={'kind': 'spin'})[0]['kind'] == 'spin'
    assert plan([a, b], 'echo', ov={'avoid': ['echo']})[0]['kind'] != 'echo'
    assert plan([a, b], ov={'sync': True})[0]['kind'] not in ('blend', 'filter', 'fade')
    assert plan([a, b], 'blend', ov={'length': [('abs', 16, 'bars')]})[0]['bars'] == 16
    assert plan([a, b], 'blend', ov={'length': [('mul', 0.5)]})[0]['bars'] == max(4, base['bars'] // 2)
    smooth, _ = plan([a, b], ov={'mood': 'smooth'})
    assert smooth['kind'] == 'blend' and smooth['bars'] >= 16 and smooth['edited']
    note, (A, B) = plan([a, c], ov={'mood': 'smooth'})        # 124 -> 100 BPM: cannot blend
    assert note['kind'] == 'echo' and B.entry['soft'] and A.exit['echo'] == 'long', note
    # (plan() reuses the same Deck objects, so read the numbers out straight away)
    t0 = plan([a, b], 'cut', 'middle')[1][0].exit['t']
    t1 = plan([a, b], 'cut', 'middle', {'shift': [(-8, 'bars')]})[1][0].exit['t']
    assert abs((t0 - t1) - 8 * 4 * 60 / 124) < 0.05, f'shift by 8 bars: {t0:.2f} -> {t1:.2f}'
    B = plan([a, b], 'cut')[1][1]
    in0, gain0 = B.src_in, B.gain
    B = plan([a, b], 'cut', ov={'enter': 'drop', 'volume': 3.0})[1][1]
    assert B.src_in >= in0 + 4 * 4 * 60 / 126 - 0.1, 'next song should start at its drop'
    assert abs(B.gain / gain0 - 10 ** (3 / 20)) < 1e-6
    assert plan([a, b], 'cut', 'end', {'position': 'middle'})[1][0].exit['t'] < plan([a, b], 'cut', 'end')[1][0].exit['t']
    print('plans follow comments ok')


def main():
    check_comments()
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

    check_plan_follows_comments(decks)
    notes, _, _ = check_mix(decks, 'auto', 'end', tmp / 'commented.mp3',
                            [{'mood': 'smooth'}, {'kind': 'echo', 'echo': 'long', 'enter': 'drop'}])
    assert [n['edited'] for n in notes] == [True, True] and notes[1]['kind'] == 'echo'
    for pair, ov, kind in (([decks[0], decks[2]], {'mood': 'smooth'}, 'echo'),            # soft entry + long echo
                           (decks[:2], {'kind': 'echo', 'echo': 'short'}, 'echo'),
                           (decks[:2], {'kind': 'fade', 'length': [('abs', 6, 'seconds')]}, 'fade')):
        notes, _, _ = check_mix(pair, 'auto', 'end', tmp / f'commented-{kind}.mp3', [ov])
        assert notes[0]['kind'] == kind, notes
    print('mix with comments ok')

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
