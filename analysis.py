"""Music analysis for DJ mixing: tempo, beat grid, downbeats, song sections, key and loudness.

Only numpy is used (no scipy/librosa/numba), so it also runs on locked-down Windows machines.
"""
import json
import subprocess
from pathlib import Path

import imageio_ffmpeg
import numpy as np

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
SR = 44100           # sample rate of the rendered mix
ASR = 22050          # sample rate used for analysis
HOP = 256            # onset hop: 86 frames per second
FPS = ASR / HOP
HOP2 = 1024          # hop for chroma / energy features: 21.5 frames per second
FPS2 = ASR / HOP2
VERSION = 6          # bump to invalidate cached analyses

KEYS = ['C', 'C#', 'D', 'Eb', 'E', 'F', 'F#', 'G', 'Ab', 'A', 'Bb', 'B']
MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])  # Krumhansl-Kessler
MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])


def decode(path, sr=SR, mono=False):
    """Decode any audio file with the bundled ffmpeg. Returns float32 (2, n), or (n,) when mono."""
    ch = 1 if mono else 2
    raw = subprocess.run([FFMPEG, '-v', 'error', '-i', str(path), '-f', 'f32le', '-ac', str(ch),
                          '-ar', str(sr), '-'], capture_output=True, check=True).stdout
    audio = np.frombuffer(raw, np.float32).reshape(-1, ch).T
    return audio[0].copy() if mono else audio.copy()


def _spectrogram(y, n_fft, hop):
    """Magnitude STFT as (frames, bins), computed in chunks to keep memory low."""
    win = np.hanning(n_fft).astype(np.float32)
    frames = np.lib.stride_tricks.sliding_window_view(np.pad(y, n_fft // 2), n_fft)[::hop]
    return np.concatenate([np.abs(np.fft.rfft(frames[i:i + 1024] * win, axis=1))
                           for i in range(0, len(frames), 1024)])


def _onsets(y):
    """Spectral-flux onset strength: all bands, and the kick/bass band (43-150 Hz)."""
    S = _spectrogram(y, 1024, HOP)
    freqs = np.fft.rfftfreq(1024, 1 / ASR)
    band = np.digitize(freqs, np.geomspace(60, 10000, 25)) - 1
    W = (band[:, None] == np.arange(24)).astype(np.float32)
    W /= np.maximum(W.sum(0), 1)
    P = S @ W
    low = (S[:, 2:8] ** 2).sum(1, keepdims=True)
    full = np.maximum(0, np.diff(np.log10(np.maximum(P, P.max() * 1e-4)), axis=0, prepend=0)).mean(1)
    kick = np.maximum(0, np.diff(np.log10(np.maximum(low, low.max() * 1e-4)), axis=0, prepend=0))[:, 0]
    full[0] = kick[0] = 0
    return full, kick


def _tempo(onset):
    """Beat period in frames from the onset autocorrelation, weighted towards ~120 BPM."""
    x = onset - onset.mean()
    ac = np.fft.irfft(np.abs(np.fft.rfft(x, 2 * len(x))) ** 2)[:len(x)]
    ac /= ac[0] + 1e-12
    lags = np.arange(int(FPS * 60 / 200), int(FPS * 60 / 55) + 1)
    prior = np.exp(-0.5 * np.log2(60 * FPS / lags / 120) ** 2)
    k = lags[np.argmax(ac[lags] * prior)]
    a, b, c = ac[k - 1], ac[k], ac[k + 1]
    period = k + (0.5 * (a - c) / (a - 2 * b + c) if a - 2 * b + c < 0 else 0)
    while 60 * FPS / period < 78:       # keep tempos in a DJ-friendly 78-160 BPM range
        period /= 2
    while 60 * FPS / period >= 160:
        period *= 2
    return period, float(b)


def _dp_beats(onset, period, tightness=100.0):
    """Dynamic-programming beat tracker (Ellis 2007, as used by librosa). Returns beat frames."""
    onset = onset / (onset.std() + 1e-9)
    k = np.arange(-int(period), int(period) + 1)
    local = np.convolve(onset, np.exp(-0.5 * (k * 32.0 / period) ** 2), 'same')
    lags = np.arange(int(round(period / 2)), int(round(2 * period)) + 1)
    penalty = -tightness * np.log(lags / period) ** 2
    n = len(local)
    cum, back = np.zeros(n), np.full(n, -1)
    thresh, started = 0.01 * local.max(), False
    for t in range(n):
        prev = t - lags
        cand = penalty + np.where(prev >= 0, cum[np.maximum(prev, 0)], 0.0)
        j = int(np.argmax(cand))
        cum[t] = local[t] + cand[j]
        if started or local[t] >= thresh:
            started = True
            back[t] = prev[j] if prev[j] >= 0 else -1
    peaks = np.flatnonzero((cum[1:-1] > cum[:-2]) & (cum[1:-1] >= cum[2:])) + 1
    if len(peaks) == 0:
        return np.array([], int)
    last = peaks[cum[peaks] >= 0.5 * np.median(cum[peaks])][-1]
    beats = [last]
    while back[beats[-1]] >= 0:
        beats.append(back[beats[-1]])
    return np.array(beats[::-1])


def _comb(env, t, p, bins=64):
    """Onset strength folded onto one beat of length p: a histogram of where onsets fall in the beat."""
    ph = (t / p) % 1.0 * bins
    i = ph.astype(int)
    f = ph - i
    h = np.bincount(i % bins, env * (1 - f), bins) + np.bincount((i + 1) % bins, env * f, bins)
    return h + 0.5 * (np.roll(h, 1) + np.roll(h, -1))


def _grid(env, period):
    """Constant-tempo beat grid that best explains all onsets (like the grid in DJ software).

    Tries the autocorrelation tempo and its 3:2 relatives (trap/hip-hop triplets often fool the
    autocorrelation) and keeps the tempo whose folded onset histogram has the sharpest peak.
    Returns (first beat time, beat period s, peakiness, share of the song that sits on the grid).
    """
    t = np.arange(len(env)) / FPS
    peak = lambda p: (lambda h: h.max() / (h.mean() + 1e-9))(_comb(env, t, p))

    def search(p0, span, n):
        cands = p0 * (1 + np.linspace(-span, span, n))
        scores = [peak(q) for q in cands]
        return cands[int(np.argmax(scores))], max(scores)

    best = (0, 0)
    for m in (1, 1.5, 2 / 3):
        bpm = 60 * FPS / period * m
        while bpm < 78:
            bpm *= 2
        while bpm >= 160:
            bpm /= 2
        p, _ = search(60 / bpm, 0.02, 401)
        p, s = search(p, 1e-4, 41)
        best = max(best, (s, p))
    score, p = best
    h = _comb(env, t, p)
    k = int(np.argmax(h))
    a, b, c = h[k - 1], h[k], h[(k + 1) % 64]
    t0 = ((k + (0.5 * (a - c) / (a - 2 * b + c) if a - 2 * b + c < 0 else 0)) / 64 % 1) * p

    # Does every part of the song sit on this grid? (live drummers drift, DAW productions don't)
    good = total = 0
    for s in range(0, int(t[-1] / p) - 16, 16):
        m = (t >= t0 + s * p) & (t < t0 + (s + 32) * p)
        hl = _comb(env[m], t[m] - t0, p)
        if hl.max() < 1.5 * hl.mean():
            continue   # breakdown without clear rhythm: no evidence either way
        near = np.r_[hl[-6:], hl[:7]]
        total += 1
        good += abs(int(np.argmax(near)) - 6) <= 3 and near.max() >= 0.8 * hl.max()
    return t0, p, score, good / total if total else 0.0


def _refine_phase(y, bt):
    """Move the beats onto the drum attacks with ~1.5 ms precision.

    The grid search works on 12 ms frames, and in some songs the drums sit a little ahead of or behind
    the grid in different sections. So measure where the attacks are in ~12 s windows and follow them.
    """
    n_fft, hop = 256, 32
    S = _spectrogram(y, n_fft, hop)
    L = np.log10(np.maximum(S, S.max() * 1e-4))
    env = np.maximum(0, np.diff(L, axis=0, prepend=L[:1])).mean(1)
    t = np.arange(len(env)) * hop / ASR
    i = np.clip(np.searchsorted(bt, t), 1, len(bt) - 1)
    near = np.where(t - bt[i - 1] < bt[i] - t, bt[i - 1], bt[i])
    off = np.round((t - near) * 1000).astype(int)
    keep = np.abs(off) <= 60
    centers, shifts = [], []
    for a in np.arange(0, t[-1] - 6, 6.0):
        m = keep & (t >= a) & (t < a + 12)
        h = np.convolve(np.bincount(off[m] + 60, env[m], 121), np.ones(3) / 3, 'same')
        if h.max() >= 1.6 * np.median(h):   # a clear attack peak in this window
            centers.append(a + 6)
            shifts.append(int(np.argmax(h)) - 60)
    if not centers:
        return bt
    shifts = np.array(shifts, float)
    if len(shifts) >= 3:   # ignore single odd windows
        shifts = np.array([np.median(shifts[max(0, k - 1):k + 2]) for k in range(len(shifts))])
    return bt + np.interp(bt, centers, shifts) / 1000


def _features(y):
    """Chroma (12), band energies in dB (8) and K-weighted power per frame (for loudness)."""
    n_fft = 4096
    S = _spectrogram(y, n_fft, HOP2)
    f = np.fft.rfftfreq(n_fft, 1 / ASR)
    ok = (f >= 100) & (f <= 4200)   # above the kick drum
    midi = 69 + 12 * np.log2(f[ok] / 440)
    Wc = np.zeros((len(f), 12), np.float32)
    Wc[np.flatnonzero(ok), np.round(midi).astype(int) % 12] = np.exp(-0.5 * ((midi - np.round(midi)) / 0.25) ** 2)
    chroma = S @ Wc
    edges = [40, 120, 250, 500, 1000, 2000, 4000, 8000, 11025]
    Wb = np.stack([(f >= lo) & (f < hi) for lo, hi in zip(edges, edges[1:])], 1).astype(np.float32)
    P = S ** 2
    bands = 10 * np.log10(P @ Wb + 1e-6)
    fs = np.maximum(f, 1.0)   # rough K-weighting: +4 dB shelf above ~1.5 kHz and a 38 Hz high-pass
    kw = (1 + 1.512 * fs ** 2 / (fs ** 2 + 2700 ** 2)) * fs ** 4 / (fs ** 4 + 38.0 ** 4)
    power = 2 * (P @ kw.astype(np.float32)) / (n_fft * np.sum(np.hanning(n_fft) ** 2))
    return chroma, bands, power


def _sync(feat, times):
    """Average feature frames between consecutive time stamps (beat or bar sync)."""
    idx = np.clip(np.round(np.asarray(times) * FPS2).astype(int), 0, len(feat) - 1)
    return np.array([feat[a:max(b, a + 1)].mean(0) for a, b in zip(idx[:-1], idx[1:])])


def _z(v):
    return (v - v.mean()) / (v.std() + 1e-9)


def _novelty(F, w):
    """Distance between the mean of the w rows before and after each row (feature change)."""
    out = np.zeros(len(F))
    for j in range(1, len(F)):
        a, b = F[max(0, j - w):j], F[j:j + w]
        if len(b):
            out[j] = np.linalg.norm(a.mean(0) - b.mean(0))
    return out / np.sqrt(F.shape[1])


def _downbeat_phase(beats, chroma, bands, onset, kick, first_beat):
    """Which of every 4 beats is the 'one': harmonic changes, bass hits and section changes land there."""
    C = _sync(chroma, beats)
    C /= np.linalg.norm(C, axis=1, keepdims=True) + 1e-9
    harm = np.zeros(len(beats))
    for i in range(2, len(C) - 1):
        a, b = C[i - 2] + C[i - 1], C[i] + C[i + 1]
        harm[i] = 1 - a @ b / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9)
    F = np.hstack([_z(_sync(bands, beats)), C])
    sect = np.concatenate([_novelty(F, 4), [0]])
    fr = np.clip(np.round(beats * FPS).astype(int), 0, len(onset) - 1)
    near = lambda env: np.array([env[max(0, i - 2):i + 3].max() for i in fr])
    score = _z(harm) + 0.5 * _z(near(kick)) + 0.3 * _z(near(onset)) + 0.5 * _z(sect)
    phase = np.array([score[k::4].mean() for k in range(4)])
    phase[first_beat % 4] += 0.25   # songs usually start on the one
    return int(np.argmax(phase))


def key_of(chroma_mean):
    """Krumhansl-Schmuckler key estimate. Returns (name like 'A minor', camelot like '8A')."""
    x = chroma_mean - chroma_mean.mean()
    best = (-2, 0, 'major')
    for mode, prof in (('major', MAJOR), ('minor', MINOR)):
        p = prof - prof.mean()
        for t in range(12):
            r = np.roll(p, t)
            c = float(x @ r / (np.linalg.norm(x) * np.linalg.norm(r) + 1e-9))
            best = max(best, (c, t, mode))
    _, tonic, mode = best
    num = ((7 * (tonic if mode == 'major' else tonic + 3) + 7) % 12) + 1
    return f"{KEYS[tonic]} {mode}", f"{num}{'B' if mode == 'major' else 'A'}"


def analyze(path):
    """Analyse one audio file. Returns a JSON-friendly dict (see keys at the end)."""
    y = decode(path, ASR, mono=True)
    duration = len(y) / ASR
    onset, kick = _onsets(y)
    chroma, bands, power = _features(y)

    # Loudness (LUFS-like): short-term 3 s windows; 'body' = how loud the main part of the song is.
    lufs = lambda p: -0.691 + 10 * np.log10(2 * p + 1e-12)
    st = lufs(np.convolve(power, np.ones(int(3 * FPS2)) / int(3 * FPS2), 'same'))
    body = float(np.percentile(st[st > -70], 75)) if np.any(st > -70) else -70.0
    short = lufs(np.convolve(power, np.ones(9) / 9, 'same'))
    audible = np.flatnonzero(short > body - 35)
    start = audible[0] / FPS2 if len(audible) else 0.0
    end = (audible[-1] + 1) / FPS2 if len(audible) else duration

    # Tempo and beats: a constant grid when the song allows it, otherwise follow the drummer.
    period, _ = _tempo(onset)
    env = onset / (onset.std() + 1e-9) + kick / (kick.std() + 1e-9)
    t0, p, beat_conf, on_grid = _grid(env, period)
    steady = on_grid >= 0.75
    if steady:
        bt = t0 + p * np.arange(np.ceil(-t0 / p), np.floor((duration - t0) / p) + 1)
    else:
        bt = _dp_beats(onset, p * FPS) / FPS
    if len(bt) < 8:   # no usable beat: fake a grid so the mixer still works (it will crossfade)
        bt, beat_conf = np.arange(start, duration, 0.5), 0.0
    else:
        bt = _refine_phase(y, bt)
    bpm = 60 / p if steady else 60 / np.median(np.diff(bt))

    first_beat = int(np.searchsorted(bt, start - 0.05))
    phase = _downbeat_phase(bt, chroma, bands, onset, kick, first_beat)
    bars = bt[phase::4]

    # Song sections: where the sound changes a lot (on bar lines)
    C = _sync(chroma, bars)
    C /= np.linalg.norm(C, axis=1, keepdims=True) + 1e-9
    F = np.hstack([_z(_sync(bands, bars)), C])
    nov = np.concatenate([_novelty(F, 4), [0]])
    bar_db = np.concatenate([lufs(_sync(power, bars)) - body, [-60]])
    core = nov[2:-2] if len(nov) > 8 else nov   # the song's start/end always look like big changes
    zn = (nov - np.median(core)) / (1.4826 * np.median(np.abs(core - np.median(core))) + 1e-9)
    bounds = [j for j in range(2, len(nov) - 1)
              if zn[j] > 1.5 and nov[j] == nov[max(0, j - 2):j + 3].max()]

    name, camelot = key_of(chroma.mean(0))
    return {
        'version': VERSION, 'duration': duration, 'bpm': float(bpm), 'steady': bool(steady),
        'beat_conf': beat_conf, 'beats': np.round(bt, 4).tolist(), 'downbeat': phase,
        'bar_db': np.round(bar_db, 2).tolist(), 'novelty': np.round(zn, 3).tolist(), 'boundaries': bounds,
        'key': name, 'camelot': camelot, 'loudness': body, 'start': float(start), 'end': float(end),
    }


def analyze_cached(path):
    """Analysis is slow-ish, so keep it next to the audio file as JSON."""
    cache = Path(path).with_suffix('.json')
    if cache.exists():
        try:
            data = json.loads(cache.read_text())
            if data.get('version') == VERSION:
                return data
        except ValueError:
            pass
    data = analyze(path)
    cache.write_text(json.dumps(data))
    return data
