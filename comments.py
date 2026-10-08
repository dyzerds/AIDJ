"""Understand a plain-English comment about one transition and turn it into mixing changes.

    interpret("too abrupt, start the next song at the chorus")
    -> ({'enter': 'drop', 'mood': 'smooth'}, ['start the next song at its drop/chorus', 'smoother'])

The mixer (mixer.plan) applies the changes; merge() combines every comment left on a transition.
"""
import re

NAMES = {'blend': 'Smooth Blend', 'filter': 'Filter Sweep', 'echo': 'Echo Out', 'cut': 'Quick Cut',
         'spin': 'Spinback', 'fade': 'Crossfade'}
KIND_WORDS = [   # what people call each technique
    ('spin', r"spin ?backs?|back ?spins?|spins?|spinning|rewinds?|vinyl"),
    ('echo', r"echo(?:es|ing)?(?: out)?"),
    ('filter', r"filters?|filtered|filtering|sweeps?|high ?pass|low ?pass"),
    ('fade', r"cross ?fades?|cross ?fading|(?:just|simple|plain|normal|regular) fade"),
    ('blend', r"blends?|blended|blending|beat ?match(?:ed|ing)?|overlap(?:ping)?|layer(?:ed|ing)?"
              r"|mix (?:them|it|both|the songs) together"),
    ('cut', r"(?:quick |hard |clean )?cuts?|chop|switch (?:right away|instantly|straight away)"),
]
NEG = r"\b(?:no|not|don'?t|do not|never|without|avoid|stop|instead of|hate|dislike|lose|remove|less of)\b"
OVERLAP = ('blend', 'filter', 'fade')   # techniques where both songs play at once
BIG = r"\b(?:much|way|a lot|lots|really|very|super|twice|far)\b"
SMALL = r"\b(?:a (?:little|bit|tad)|slightly|little|bit|tad|somewhat)\b"
WORD_NUMS = {'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6, 'eight': 8, 'ten': 10,
             'twelve': 12, 'fifteen': 15, 'sixteen': 16, 'twenty': 20, 'thirty two': 32, 'thirty': 30}
NUM = r"(\d+(?:\.\d+)?|" + '|'.join(WORD_NUMS) + r")"
UNIT = r"(bars?|beats?|seconds?|secs?)"
HELP = ('Try things like "longer", "start 8 bars earlier", "use an echo", "no spinback", '
        '"start the next song at the chorus", "smoother", "punchier" or "the beats clash".')


def _unit(u):
    return 'bars' if u.startswith('bar') else 'beats' if u.startswith('beat') else 'seconds'


def interpret(text):
    """Returns (changes, summary). An empty summary means the comment was not understood."""
    t = text.lower().replace('’', "'")
    t = re.sub(r"[.;!?]", ' . ', t).replace(',', ' , ')
    t = ' ' + re.sub(r"\s+", ' ', re.sub(r"[^a-z0-9'., ]", ' ', t)) + ' '
    ch, said = {}, []

    def take(pattern):
        """Find and remove a phrase (so later, more general rules don't read it again).
        Returns None, or how strongly it was said: 'small', 'normal' or 'big'."""
        nonlocal t
        m = re.search(r"\b(?:" + pattern + r")\b", t)
        if not m:
            return None
        before = t[max(0, m.start() - 18):m.start()]
        t = t[:m.start()] + ' # ' + t[m.end():]
        return 'big' if re.search(BIG, before) else 'small' if re.search(SMALL, before) else 'normal'

    if take(r"reset|undo|go back|start over|(?:the )?original(?: plan)?|as (?:it was )?before|default|never ?mind"
            r"|forget (?:it|that)|remove (?:my )?(?:changes|comments)"):
        return {'reset': True}, ["back to the AI's own plan"]
    if take(r"you (?:choose|decide|pick)|your (?:choice|call)|auto(?:matic)?|surprise me"
            r"|whatever (?:you think|sounds best)"):
        ch['kind'] = 'auto'
        said.append('let the AI pick the style')

    # Numbers: "8 bars earlier", "shorten it by 4 bars", "a 16 bar blend", "10 seconds later"
    while True:
        m = re.search(r"\b(earlier|sooner|later|longer|shorter|lengthen|shorten|extend)(?: it| the (?:transition|mix|blend))?"
                      r" by " + NUM + ' ?' + UNIT + r"\b", t)
        if m:
            d, v, u = m.group(1), m.group(2), m.group(3)
        else:
            m = re.search(r"\b" + NUM + ' ?' + UNIT + r"(?: (long|longer|shorter|earlier|sooner|later|before|after|more|less))?\b", t)
            if not m:
                break
            v, u, d = m.group(1), m.group(2), m.group(3) or ''
        v, u = float(WORD_NUMS.get(v, v)), _unit(u)
        t = t[:m.start()] + ' # ' + t[m.end():]
        n = f'{v:g} {u}'
        if d in ('earlier', 'sooner', 'before'):
            ch.setdefault('shift', []).append((-v, u))
            said.append(f'mix {n} earlier')
        elif d in ('later', 'after'):
            ch.setdefault('shift', []).append((v, u))
            said.append(f'mix {n} later')
        elif d in ('longer', 'lengthen', 'extend', 'more'):
            ch.setdefault('length', []).append(('add', v, u))
            said.append(f'{n} longer')
        elif d in ('shorter', 'shorten', 'less'):
            ch.setdefault('length', []).append(('add', -v, u))
            said.append(f'{n} shorter')
        else:
            ch.setdefault('length', []).append(('abs', v, u))
            said.append(f'make it {n} long')

    if take(r"(?:more|longer|bigger|louder|extra) echo|echo (?:is |was )?too (?:short|quiet|quick|subtle)"):
        ch['echo'] = 'long'
        said.append('a longer echo')
    elif take(r"(?:less|shorter|smaller|quieter|subtler|subtle|softer) echo|echo (?:is |was )?too (?:long|much|loud|big)"):
        ch['echo'] = 'short'
        said.append('a shorter echo')

    if take(r"(?:at|on|from|into|with|to) (?:the |its |her |his |their )?(?:drop|chorus|hook|best part|peak|main part"
            r"|good part|beat drop)|skip (?:the |its )?intro|no intro|without (?:the |an )?intro|straight (?:in|into it)"):
        ch['enter'] = 'drop'
        said.append('start the next song at its drop/chorus')
    elif take(r"from the (?:start|beginning)|(?:with|keep) (?:the |its )?intro|full intro|whole intro"):
        ch['enter'] = 'start'
        said.append('start the next song from its beginning')

    if take(r"(?:in )?the middle|halfway|half way|midway|mid ?song"):
        ch['position'] = 'middle'
        said.append('mix in the middle of the song')
    elif take(r"(?:at|near|towards?) the end|end of the song|very end|until the end|till the end|whole song|full song"
              r"|entire song|let (?:it|the song|the first song) (?:finish|end|play out)|play (?:it |the song )?(?:out|through)"):
        ch['position'] = 'end'
        said.append('let the song play until its end')

    size = take(r"earlier|sooner|too late|(?:skip|cut|drop|lose|shorten) (?:the |its )?outro|before the outro")
    if size:
        bars = {'small': 4, 'normal': 8, 'big': 16}[size]
        ch.setdefault('shift', []).append((-bars, 'bars'))
        said.append(f'mix {bars} bars earlier')
    else:
        size = take(r"later|too (?:early|soon)|wait(?: longer| more| a bit)?|delay (?:it|the (?:transition|mix|next song))"
                    r"|let (?:it|the song) (?:play|breathe|ring)(?: out)?|after the (?:chorus|drop|hook)"
                    r"|(?:don'?t|do not) (?:cut|end) (?:it|the song)(?: so)? (?:short|early|off)")
        if size:
            bars = {'small': 4, 'normal': 8, 'big': 16}[size]
            ch.setdefault('shift', []).append((bars, 'bars'))
            said.append(f'mix {bars} bars later')

    if take(r"off ?beat|out of sync|not in sync|off time|out of time|train ?wreck|flam(?:s|ming)?|double (?:beats?|kicks?)"
            r"|(?:beats?|drums?|kicks?) (?:clash(?:ed|ing)?|don'?t (?:match|line up)|(?:are|were|is|was) off)"):
        ch['sync'] = True
        said.append("no overlap, so the beats can't clash")
    if take(r"clash(?:es|ed|ing)?|messy|muddy|mud|chaotic|chaos|too busy|cluttered|crowded|too much going on|dissonant"
            r"|out of key|off key|wrong key|sounds? bad together|too much bass|boomy|bass(?:y)? (?:is |was )?(?:too )?(?:loud|heavy|much)"):
        ch['mood'] = 'clean'
        said.append('cleaner: less overlap, filters instead of a full blend')

    size = take(r"louder|too (?:quiet|soft|low)|turn (?:it )?up|more volume|can'?t hear")
    if size:
        db = {'small': 1.5, 'normal': 3.0, 'big': 6.0}[size]
        ch['volume'] = db
        said.append(f'next song {db:g} dB louder')
    else:
        size = take(r"quieter|too loud|turn (?:it )?down|less volume|lower (?:the )?volume")
        if size:
            db = {'small': 1.5, 'normal': 3.0, 'big': 6.0}[size]
            ch['volume'] = -db
            said.append(f'next song {db:g} dB quieter')

    if take(r"smooth(?:er|ly)?|soft(?:er)?|gentle|gentler|gently|seamless(?:ly)?|subtle|subtler|natural(?:ly)?"
            r"|flow(?:s|ing)? better|abrupt(?:ly)?|sudden(?:ly)?|jarring|harsh|rough|jumpy|choppy"):
        ch['mood'] = 'smooth'
        said.append('smoother')
    elif take(r"punch(?:y|ier)?|hard(?:er)?|hits? (?:harder|hard)|more (?:energy|impact|power|hype|excitement)"
              r"|energetic|hype|explosive|aggressive|boring|dull|lifeless|exciting|bigger impact|slam(?:s|ming)?"):
        ch['mood'] = 'punchy'
        said.append('punchier: a hard switch on the beat')

    size = take(r"longer|extend(?:ed)?|lengthen|more time|stretch (?:it )?out|drawn out|too (?:short|quick|fast)"
                r"|over too (?:fast|quickly|soon)|slower|take (?:its|more) time")
    if size:
        ch.setdefault('length', []).append(('mul', {'small': 1.5, 'normal': 2.0, 'big': 3.0}[size]))
        said.append('a longer transition')
    else:
        size = take(r"shorter|quicker|faster|tighter|less time|too (?:long|slow)|drags?(?: on)?|takes too long|shorten")
        if size:
            ch.setdefault('length', []).append(('mul', {'small': 0.75, 'normal': 0.5, 'big': 0.25}[size]))
            said.append('a shorter transition')

    # Techniques last: "no spinback", "use an echo instead of the cut", "cut, not echo"
    wants, avoid = [], []
    for kind, pattern in KIND_WORDS:
        for m in re.finditer(r"\b(?:" + pattern + r")\b", t):
            clause = re.split(r" [.,] | but | rather ", t[:m.start()])[-1][-30:]
            if re.search(NEG + r"(?: [\w']+){0,3} $", clause):
                avoid.append(kind)
            else:
                wants.append((m.start(), kind))
    first = [f'no {NAMES[kind]}' for kind in dict.fromkeys(avoid)]   # the technique is said first
    if avoid:
        ch['avoid'] = sorted(set(avoid))
    if wants and min(wants)[1] not in avoid:
        ch['kind'] = min(wants)[1]
        first.insert(0, f'switch to {NAMES[ch["kind"]]}')
    return ch, first + said


def reply(said):
    if not said:
        return "I couldn't turn that into a change. " + HELP
    return 'Got it: ' + '; '.join(said) + '.'


def merge(texts):
    """Combine all comments on one transition, oldest first. Returns (changes, [{text, ok, reply}])."""
    ov, results = {}, []
    for text in texts:
        ch, said = interpret(text)
        results.append({'text': text, 'ok': bool(said), 'reply': reply(said)})
        if ch.pop('reset', False):
            ov = {}
            continue
        for key, value in ch.items():
            if key in ('length', 'shift'):
                ov.setdefault(key, []).extend(value)
            elif key == 'avoid':
                ov['avoid'] = sorted(set(ov.get('avoid', [])) | set(value))
                if ov.get('kind') in value:
                    del ov['kind']
            elif key == 'volume':
                ov['volume'] = max(-9.0, min(9.0, ov.get('volume', 0.0) + value))
            elif key == 'kind' and value == 'auto':
                ov.pop('kind', None)
            else:   # the newest comment wins
                ov[key] = value
                if key == 'kind' and value in ov.get('avoid', []):
                    ov['avoid'] = [k for k in ov['avoid'] if k != value]
                if key == 'kind' and value in OVERLAP:
                    ov.pop('sync', None)
                if key == 'sync' and ov.get('kind') in OVERLAP:
                    del ov['kind']
    return ov, results
