"""The DJ: plans every transition (where, how long, which technique) and renders one continuous mix.

How a transition is built
- Beat-matching: the incoming song is time-stretched (Rubber Band, pitch kept) to the outgoing song's
  tempo and its first downbeat is placed on a phrase boundary of the outgoing song (beat grids are
  accurate to a few milliseconds). After the blend it glides back to its own tempo.
- EQ work happens in the frequency domain: bass swaps, filter sweeps, fades.
- Echo, spinback and cuts are used when tempos are too far apart to blend (or when asked for).
"""
import math
import subprocess

import numpy as np
import pedalboard

from analysis import FFMPEG, SR, decode

STYLES = ('auto', 'blend', 'filter', 'echo', 'cut', 'spin')
NAMES = {'blend': 'Smooth Blend', 'filter': 'Filter Sweep', 'echo': 'Echo Out', 'cut': 'Quick Cut',
         'spin': 'Spinback', 'fade': 'Crossfade'}
BODY_TARGET = -8.0    # loudness of each song's main part after normalisation (LUFS-like)
MAX_STRETCH = 0.08    # beat-match only if the tempo change needed is at most 8 %
PREROLL = 0.03        # start hard-entering songs a hair early so the first kick keeps its attack
N_FFT, HOP = 4096, 1024


class Deck:
    """One song in the mix: its analysis plus how and when it is played."""

    def __init__(self, path, meta, an):
        self.path, self.meta, self.an = path, meta, an
        self.beats = np.array(an['beats'])
        self.bars = self.beats[an['downbeat']::4]
        self.bar_db = np.array(an['bar_db'])
        self.nov = np.array(an['novelty'])
        self.bounds = set(an['boundaries'])
        self.rhythmic = an['beat_conf'] >= 1.5
        self.in_bar = min(int(np.searchsorted(self.bars, an['start'] - 0.05)), max(0, len(self.bars) - 8))
        strong = np.flatnonzero(self.bar_db >= -9)
        self.end_bar = int(strong[-1]) + 1 if len(strong) else len(self.bars) - 1
        votes = np.zeros(8)
        for j in self.bounds:
            votes[j % 8] += max(self.nov[j], 0)
        self.phrase0 = int(np.argmax(votes)) if votes.any() else self.in_bar % 8
        self.gain = float(np.clip(10 ** ((BODY_TARGET - an['loudness']) / 20), 0.25, 2.5))
        # Playback plan, filled in by plan()
        self.src_in, self.src_end = max(0.0, an['start'] - 0.01), an['duration']
        self.s0, self.ramp, self.p1 = 1.0, None, self.src_in
        self.entry, self.exit, self.lead = {'kind': 'start'}, None, 0.0

    def beat_len(self, t):
        """Local beat length (s) around source time t."""
        if self.an['steady']:
            return 60 / self.an['bpm']
        i = int(np.searchsorted(self.beats, t))
        seg = self.beats[max(0, i - 8):i + 8]
        return float(np.median(np.diff(seg))) if len(seg) > 2 else 60 / self.an['bpm']

    def bar_time(self, j):
        if j < len(self.bars):
            return float(self.bars[j])
        return float(self.bars[-1] + (j - len(self.bars) + 1) * 4 * self.beat_len(self.bars[-1]))

    def bar_at(self, t):
        return int(np.searchsorted(self.bars, t - 1e-3))

    def speed(self, p):
        """Playback speed (1 = original tempo) at source time p."""
        p = np.asarray(p, float)
        if self.ramp is None:
            return np.full(p.shape, self.s0)
        a, b = self.ramp
        u = np.clip((p - a) / (b - a), 0, 1)
        # exactly 1.0 once settled: Rubber Band then passes the audio through untouched (and fast)
        return np.where(u >= 1, 1.0, self.s0 + (1 - self.s0) * (0.5 - 0.5 * np.cos(np.pi * u)))

    def out_time(self, p):
        """Seconds after this deck starts playing at which source time p is heard."""
        q = np.linspace(self.src_in, p, 4001)
        return float(np.trapezoid(1 / self.speed(q), q))

    def drop(self, j):
        """How much quieter the 4 bars after bar j are than the 4 before (leaving after a big part)."""
        a, b = self.bar_db[max(0, j - 4):j], self.bar_db[j:j + 4]
        return float(a.mean() - b.mean()) if len(a) and len(b) else 0.0


def _camelot_distance(a, b):
    na, nb = int(a[:-1]), int(b[:-1])
    d = min(abs(na - nb), 12 - abs(na - nb))
    return d + (a[-1] != b[-1])


def _match(pa, pb):
    """Speed for the incoming song so its beat lands on the outgoing beat (half/double time allowed)."""
    s = min((pb / (pa * k) for k in (0.5, 1, 2)), key=lambda s: abs(math.log(s)))
    return 1.0 if abs(s - 1) < 2e-4 else s   # same tempo: no stretching needed


def _choose(A, B, style, prev):
    """Pick the transition technique. Returns (kind, reason)."""
    s = _match(A.beat_len(A.bar_time(A.end_bar)), B.beat_len(B.bar_time(B.in_bar) + 4))
    gap = abs(s - 1)
    beat_ok = A.rhythmic and B.rhythmic
    key_ok = _camelot_distance(A.an['camelot'], B.an['camelot']) <= 1
    tempo = f"{A.an['bpm']:.0f} → {B.an['bpm']:.0f} BPM"
    if style in ('blend', 'filter'):
        if not beat_ok:
            return 'fade', 'no steady beat to match, so a smooth crossfade instead'
        if gap > MAX_STRETCH:
            return 'echo', f'tempos too far apart to blend ({tempo}), so an echo out instead'
        return style, f'beat-matched ({tempo})'
    if style != 'auto':
        return style, tempo
    if not beat_ok:
        return 'fade', 'no steady beat, smooth crossfade'
    keys = f"{A.an['camelot']} → {B.an['camelot']}"
    if gap <= 0.06 and (key_ok or prev == 'filter'):
        return 'blend', f'tempos match ({tempo}), keys {"match" if key_ok else "differ, so a shorter blend"} ({keys})'
    if gap <= 0.06:
        return 'filter', f'tempos match ({tempo}) but keys clash ({keys}), filters keep it clean'
    if B.bar_db[B.in_bar:B.in_bar + 2].mean() > -4 and prev != 'cut':
        return 'cut', f'tempo jump ({tempo}) and the next song starts strong'
    if prev != 'echo':
        return 'echo', f'tempo jump ({tempo})'
    return 'spin', f'tempo jump ({tempo}), mixing it up'


def _length(kind, position, A, B):
    """Transition length in bars."""
    if kind == 'blend':
        key_ok = _camelot_distance(A.an['camelot'], B.an['camelot']) <= 1
        long = position == 'end' and key_ok and A.an['steady'] and B.an['steady']
        return 16 if long else 8
    if kind == 'filter':
        return 8
    if kind == 'fade':
        return max(2, round(8 / (4 * A.beat_len(A.bar_time(A.end_bar)))))
    return 0


def _exit_bars(A, kind, over, position):
    """Choose the bar where the transition starts in the outgoing song (on a phrase line)."""
    max_bar = max(A.in_bar, A.bar_at(A.an['end'] - 0.3) - 1 - over)   # leave before the music stops
    lo = min(A.bar_at(A.p1) + 4, max_bar)
    if position == 'end':
        last = min(max(A.end_bar, lo + over), max_bar + over)
        target = last - over
        first, final = target - 8, target + 1
    else:
        n = A.end_bar - A.in_bar
        target = A.in_bar + 0.5 * n
        first, final = int(A.in_bar + 0.35 * n), int(A.in_bar + 0.65 * n)
    first = min(max(lo, first), max_bar)          # very short songs: still at least one candidate
    starts = range(first, max(first, min(final, max_bar)) + 1)

    def score(j):
        rel = (j - A.phrase0) % 8
        s = 1.0 if rel == 0 else 0.5 if rel == 4 else -1.0
        bound = lambda k: min(max(A.nov[k], 0), 4) / 2 if k in A.bounds and k < len(A.nov) else 0
        s += bound(j) * (1.5 if position == 'middle' or not over else 1.0) + 0.5 * bound(j + over)
        if position == 'middle':
            s += 0.3 * np.clip(A.drop(j), 0, 6)
        return s - (0.25 if position == 'end' else 0.1) * abs(j - target)

    j = max(starts, key=score)
    return j, j + over


def _settle(d, exit_src):
    """Plan the glide back to the song's own tempo so it is steady before it mixes out."""
    if d.s0 == 1.0:
        d.ramp = None
        return
    bar = 4 * d.beat_len(d.p1)
    want = (8 if abs(d.s0 - 1) <= 0.04 else 16) * bar
    room = exit_src - d.p1 - bar
    d.ramp = (d.p1, d.p1 + min(want, room)) if room >= 3 * bar else None


def plan(decks, style='auto', position='end'):
    """Decide every transition. Returns notes for the tracklist."""
    notes, prev = [], None
    for A, B in zip(decks, decks[1:]):
        kind, why = _choose(A, B, style, prev)
        over = min(_length(kind, position, A, B), max(0, A.bar_at(A.an['end'] - 0.3) - A.bar_at(A.p1) - 2))
        if over < 2 and kind in ('blend', 'filter', 'fade'):
            kind, why, over = 'cut', 'song too short to blend', 0
        j_out, j_end = _exit_bars(A, kind, over, position)
        _settle(A, A.bar_time(j_out))
        sA = float(A.speed(A.bar_time(j_out)))
        pa = A.beat_len(A.bar_time(j_out)) / sA          # beat length in the mix (s)
        B.p1 = B.bar_time(B.in_bar)
        if kind in ('blend', 'filter'):
            B.s0 = _match(pa, B.beat_len(B.p1 + 4))
            if abs(B.s0 - 1) > MAX_STRETCH:              # A could not glide back in time: fall back
                kind, why, B.s0 = 'echo', 'tempos too far apart to blend, so an echo out instead', 1.0
                over = 0
                j_out, j_end = _exit_bars(A, kind, over, position)
                _settle(A, A.bar_time(j_out))
        t_out = A.out_time(A.bar_time(j_out))
        dur = A.out_time(A.bar_time(j_end)) - t_out
        A.exit = {'kind': kind, 't': t_out, 'dur': dur, 'beat': pa, 'bars': over}
        A.src_end = A.bar_time(j_end) + 0.05
        B.entry = {'kind': kind, 'dur': dur, 'bars': over}
        if kind in ('blend', 'filter', 'fade'):
            B.src_in = B.p1
            B.p1 = B.src_in + dur * B.s0                 # where the overlap ends in B
            B.lead = t_out
        else:
            B.src_in = max(0.0, B.p1 - PREROLL)
            B.lead = t_out - (B.p1 - B.src_in)
            if kind == 'spin':
                A.exit['spin'] = max(2, round(1.4 / pa)) * pa
                B.lead += A.exit['spin']
        notes.append({'kind': kind, 'name': NAMES[kind], 'bars': over, 'why': why})
        prev = kind
    last = decks[-1]
    _settle(last, last.an['duration'])
    last.src_end = last.an['duration']
    return notes


# ---------------------------------------------------------------- audio effects

def _filt(x, hp=None, lp=None):
    """Zero-phase high/low-pass (24 dB/oct) via FFT, for short clips."""
    pad = SR // 10
    X = np.fft.rfft(np.pad(x, ((0, 0), (pad, pad))), axis=1)
    f = np.maximum(np.fft.rfftfreq(x.shape[1] + 2 * pad, 1 / SR), 1.0)
    H = np.ones_like(f)
    if hp:
        H /= np.sqrt(1 + (hp / f) ** 8)
    if lp:
        H /= np.sqrt(1 + (f / lp) ** 8)
    return np.fft.irfft(X * H, x.shape[1] + 2 * pad, axis=1)[:, pad:-pad].astype(np.float32)


def _gains(f, p, n):
    """Gain per (frame, frequency) for a 3-band DJ EQ plus optional resonant high/low-pass filters."""
    f = np.maximum(f, 1.0)[None, :]
    col = lambda v: np.broadcast_to(np.asarray(v, float), (n,))[:, None]
    wl = 1 / (1 + (f / 180) ** 4)
    wh = 1 / (1 + (2500 / f) ** 4)
    G = col(p['low']) * wl + col(p['mid']) * np.clip(1 - wl - wh, 0, 1) + col(p['high']) * wh
    res = col(p.get('res', 0.0))
    bump = lambda fc: 1 + res * np.exp(-0.5 * (np.log2(f / fc) / 0.2) ** 2)
    if 'hp' in p:
        fc = col(p['hp'])
        G = G / np.sqrt(1 + (fc / f) ** 4) * bump(fc)
    if 'lp' in p:
        fc = col(p['lp'])
        G = G / np.sqrt(1 + (f / fc) ** 4) * bump(fc)
    return G.astype(np.float32)


def _automate(y, start, dur, curve):
    """Apply a time-varying EQ/filter (curve(u) -> params, u = 0..1) to y between start and start+dur."""
    a = max(0, int(start * SR) - N_FFT)
    b = min(y.shape[1], int((start + dur) * SR) + N_FFT)
    if b <= a or dur <= 0:
        return y
    sp = np.pad(y[:, a:b], ((0, 0), (N_FFT, N_FFT + HOP)))
    nfr = (sp.shape[1] - N_FFT) // HOP + 1
    times = (np.arange(nfr) * HOP + N_FFT / 2 - N_FFT + a) / SR
    u = np.clip((times - start) / dur, 0, 1)
    G = _gains(np.fft.rfftfreq(N_FFT, 1 / SR), curve(u), nfr)
    win = (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(N_FFT) / N_FFT)).astype(np.float32)
    out = np.zeros_like(sp)
    for c in range(sp.shape[0]):
        fr = np.lib.stride_tricks.sliding_window_view(sp[c], N_FFT)[::HOP][:nfr]
        rec = np.fft.irfft(np.fft.rfft(fr * win, axis=1) * G, N_FFT, axis=1).astype(np.float32) * win
        for i in range(nfr):
            out[c, i * HOP:i * HOP + N_FFT] += rec[i]
    y = y.copy()
    y[:, a:b] = out[:, N_FFT:N_FFT + b - a] / 1.5   # 1.5 = overlap-add gain of Hann at 75 % overlap
    return y


def _fade(y, a, n, up):
    """Short linear fade (anti-click) of n samples starting at sample a."""
    n = max(1, min(n, y.shape[1] - a))
    r = np.linspace(0, 1, n, dtype=np.float32)
    y[:, a:a + n] *= r if up else r[::-1]
    return y


def _swap(u, bars):
    """0 -> 1 over the half beat just before the middle downbeat: when the bass lines swap."""
    w = 0.5 / (4 * bars)
    return np.clip((u - (0.5 - w)) / w, 0, 1)


def _entry_curve(kind, bars):
    if kind == 'blend':
        return lambda u: (lambda v: dict(low=_swap(u, bars), mid=v, high=v))(np.sin(0.5 * np.pi * np.clip(u / 0.5, 0, 1)))
    # filter: comes in muffled and opens up; its bass waits until the outgoing bass is filtered away
    return lambda u: dict(low=np.clip((u - 0.62) / 0.12, 0, 1) * np.sin(0.5 * np.pi * np.clip(u / 0.3, 0, 1)),
                          mid=np.sin(0.5 * np.pi * np.clip(u / 0.3, 0, 1)),
                          high=np.sin(0.5 * np.pi * np.clip(u / 0.3, 0, 1)),
                          lp=300 * (1e5 / 300) ** np.clip(u / 0.9, 0, 1), res=0.4)


def _exit_curve(kind, bars):
    if kind == 'blend':
        return lambda u: (lambda v: dict(low=1 - _swap(u, bars), mid=v, high=v))(np.cos(0.5 * np.pi * np.clip((u - 0.5) / 0.5, 0, 1)))
    # filter: high-pass sweeps up (the song gets thinner), then it fades away
    return lambda u: (lambda v: dict(low=v, mid=v, high=v, hp=5 * (1800 / 5) ** (u ** 1.4), res=0.5))(
        np.cos(0.5 * np.pi * np.clip((u - 0.7) / 0.3, 0, 1)))


def _echo(y, d, beat):
    """Tempo-synced echo of the two beats before sample d: filtered feedback, ping-pong, some reverb."""
    s = max(0, d - int(2 * beat * SR))
    send = _filt(y[:, s:d] * np.linspace(0, 1, d - s, dtype=np.float32) ** 2, hp=300, lp=9000)
    delay, reps = int(0.75 * beat * SR), 9
    wet = np.zeros((2, d - s + delay * reps + 2 * SR), np.float32)
    r = send
    for k in range(1, reps + 1):
        r = _filt(r, hp=400, lp=max(1500, 8000 * 0.8 ** k)) * 0.6
        pan = np.array([[1 - 0.3 * (-1) ** k], [1 + 0.3 * (-1) ** k]], np.float32)
        wet[:, k * delay:k * delay + r.shape[1]] += r * pan
    verb = pedalboard.Reverb(room_size=0.6, damping=0.5, wet_level=0.25, dry_level=0.85, width=1.0)
    return verb(wet, SR), s


def _spinback(y, d, dur):
    """Vinyl spinback: the record is pushed backwards from sample d and slows to a stop."""
    n = int(dur * SR)
    t = np.arange(n) / SR
    grab = 0.05
    v = np.where(t < grab, 1 - 4 * t / grab, -3 * np.exp(-(t - grab) / (dur / 3.5)))
    lo = max(0, d - 6 * SR)
    src = _filt(y[:, lo:d + SR], lp=7000)       # less aliasing when the record spins fast
    pos = np.clip(d - lo + np.cumsum(v), 0, src.shape[1] - 2)
    i = pos.astype(int)
    f = (pos - i).astype(np.float32)
    out = src[:, i] * (1 - f) + src[:, i + 1] * f
    env = (np.minimum(1, np.abs(v) / 0.5) ** 0.7 * (1 - (t / dur) ** 2)).astype(np.float32)
    return out * env


# ---------------------------------------------------------------- rendering

def _play(d):
    """The deck's audio from src_in to src_end, time-stretched to the planned tempo curve."""
    x = decode(d.path)
    a, b = int(round(d.src_in * SR)), int(round(d.src_end * SR))
    seg = np.ascontiguousarray(x[:, max(0, a):b])
    if d.ramp is None and d.s0 == 1.0:
        return seg
    sf = float(d.s0) if d.ramp is None else d.speed(d.src_in + np.arange(seg.shape[1]) / SR)
    return pedalboard.time_stretch(seg, SR, stretch_factor=sf, high_quality=False)


def _apply_entry(y, e):
    k = e['kind']
    if k in ('blend', 'filter'):
        return _automate(y, 0, e['dur'], _entry_curve(k, e['bars']))
    if k == 'fade':
        n = min(y.shape[1], int(e['dur'] * SR))
        y[:, :n] *= np.sin(0.5 * np.pi * np.linspace(0, 1, n, dtype=np.float32))
        return y
    return _fade(y, 0, int(0.004 * SR), True)


def _apply_exit(y, x):
    """Outgoing effects. Returns the deck audio, possibly longer (echo / spin tails)."""
    if x is None:   # last song: let it ring out, with a safety fade at the very end
        return _fade(y, max(0, y.shape[1] - SR), SR, False)
    k, t = x['kind'], x['t']
    a = int(t * SR)
    if k in ('blend', 'filter'):
        y = _automate(y, t, x['dur'], _exit_curve(k, x['bars']))
        e = min(y.shape[1], int((t + x['dur']) * SR))
        return _fade(y[:, :e + int(0.02 * SR)], e, int(0.02 * SR), False)
    if k == 'fade':
        e = min(y.shape[1], int((t + x['dur']) * SR))
        y = y[:, :e].copy()
        y[:, a:e] *= np.cos(0.5 * np.pi * np.linspace(0, 1, e - a, dtype=np.float32))
        return y
    if k == 'cut':
        return _fade(y[:, :a + int(0.012 * SR)], a, int(0.012 * SR), False)
    if k == 'echo':
        beat = x['beat']
        y = _automate(y, t - 2 * beat, 2 * beat, lambda u: dict(low=1, mid=1, high=1, hp=5 * (500 / 5) ** u, res=0.4))
        wet, s = _echo(y, a, beat)
        y = _fade(y[:, :a + int(0.01 * SR)], a, int(0.01 * SR), False)
        y = np.pad(y, ((0, 0), (0, max(0, s + wet.shape[1] - y.shape[1]))))
        y[:, s:s + wet.shape[1]] += wet
        return y
    if k == 'spin':
        return np.concatenate([y[:, :a], _spinback(y, a, x['spin'])], axis=1)
    raise ValueError(k)


class _Encoder:
    """Streams the mix through a brick-wall limiter into an MP3, so long mixes never sit in memory."""

    def __init__(self, path):
        self.proc = subprocess.Popen([FFMPEG, '-v', 'error', '-y', '-f', 'f32le', '-ar', str(SR), '-ac', '2',
                                      '-i', 'pipe:0', '-c:a', 'libmp3lame', '-b:a', '320k', str(path)],
                                     stdin=subprocess.PIPE)
        self.buf, self.pos = np.zeros((2, 0), np.float32), 0
        self.limiter = pedalboard.BrickwallLimiter(ceiling_db=-1.0, release_ms=150, lookahead_ms=5, true_peak=True)

    def add(self, y, at):
        rel = at - self.pos
        if rel < 0:
            y, rel = y[:, -rel:], 0
        end = rel + y.shape[1]
        if end > self.buf.shape[1]:
            self.buf = np.pad(self.buf, ((0, 0), (0, end - self.buf.shape[1])))
        self.buf[:, rel:end] += y

    def flush(self, upto):
        k = int(np.clip(upto - self.pos, 0, self.buf.shape[1]))
        if k:
            chunk, self.buf, self.pos = self.buf[:, :k], self.buf[:, k:], self.pos + k
            self._write(chunk)

    def _write(self, chunk):
        out = self.limiter.process(np.ascontiguousarray(chunk), SR, reset=False)
        self.proc.stdin.write(np.ascontiguousarray(out.T, np.float32).tobytes())

    def close(self):
        self.flush(self.pos + self.buf.shape[1])
        self._write(np.zeros((2, SR // 10), np.float32))   # push out the limiter's look-ahead
        self.proc.stdin.close()
        if self.proc.wait():
            raise RuntimeError('ffmpeg failed to write the mix')


def render(decks, out_path, on_progress=lambda i, n, title: None):
    """Render planned decks into an MP3. Returns the start time (s) of each song in the mix."""
    enc = _Encoder(out_path)
    times, t_abs = [], 0.0
    try:
        for i, d in enumerate(decks):
            on_progress(i, len(decks), d.meta['title'])
            y = _apply_exit(_apply_entry(_play(d), d.entry), d.exit) * d.gain
            enc.add(y, int(round(t_abs * SR)))
            times.append(t_abs)
            if i + 1 < len(decks):
                t_abs += decks[i + 1].lead
                enc.flush(int((t_abs - 1.0) * SR))
    finally:
        enc.close()
    return times
