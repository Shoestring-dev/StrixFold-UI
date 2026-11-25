"""Replies that go round in circles. One reply announced the same four steps over and over, without a call, until it was
stopped: the engine's loop guard reads thinking only and wants one exact cycle of 64 tokens at most for 256 tokens (this
one was eight rounds of four lines, three others, and again), and the app counted repeated calls, never repeated words.

Watch reads a reply as it streams and says where its repeats begin; collapse gives a stored answer to the model with its
repeats said once (the chat keeps the whole text). Exact repeats only. Two kinds: steps announced and not taken ("Let me
check the log.", with or without the command shown), in a reply that could make calls, end soon, unless a song, a pledge or
the like was asked for; any other text only after a great many rounds, or fewer long ones, as a breathing exercise or a
chorus is meant to repeat. Never code by itself, nor a table's rows. So a loop of code alone, or one whose words change
each round, is not caught."""
import re

import chat_tools as T

STEP_REPEATS, STEP_CHARS = 6, 400      # announced steps: this many rounds, this long in all
REPEATS, MIN_CHARS = 40, 6000          # any other text ...
LONG_ROUND, LONG_REPEATS, LONG_CHARS = 200, 10, 8000  # ... or fewer rounds of a paragraph
BARE_CHARS = 8000                      # a stretch of one or two words ("no no no")
WINDOW = 16_000                        # the end of the text that is read
EVERY = 160                            # characters between two looks
_WORD = re.compile(r"[^\W\d_]{2,}")
_FENCE = re.compile(r"^[ \t]*(?:```|~~~)", re.M)
_BLOCK = re.compile(r"^[ \t]*(```|~~~)[^\n]*\n.*?^[ \t]*\1[ \t]*(?:\n|$)", re.M | re.S)
_LOOK = r"(?:check|read|inspect|verify|fetch|search|list|install|grep|look (?:at|for|into|up)|examine|query|view)"
_ACT = r"(?:open|run|find|get|start|test|update|edit|write|create|see(?! you)|try|do|make|fix|use|set|stop|restart|build|print)"
# (a step's verb close by: "I will be kind", "Let's go" and "First, I ___" announce nothing)
_STEP = re.compile(rf"\b(?:let me|i'll|i need to|i should|now i|then i(?:'ll)?|next,? i(?:'ll| will)?)\b[^.!?\n]{{0,30}}?\b(?:{_LOOK}|{_ACT})\b"
                   rf"|\b(?:let's|i will|i am going to|i'm going to|first,? i(?:'ll| will)?)\b[^.!?\n]{{0,30}}?\b{_LOOK}\b", re.I)
_ASKED = re.compile(r"\b(?:affirmations?|mantras?|pledges?|chorus|lyrics|songs?|poems?|verses?|rhymes?|chants?|hymns?|prayers?|lullab(?:y|ies)|oaths?|vows?|cheers?|slogans?|repeat\w*|\d+ (?:times|copies|rounds))\b", re.I)


def bare(stretch):
    """`stretch` without its code blocks."""
    return _BLOCK.sub("", stretch)


def announced(stretch):
    """Nearly every line of it (its code blocks aside) says what will be done next."""
    lines = [x.strip() for x in bare(stretch).split("\n") if x.strip()]
    said = sum(1 for x in lines if len(x) <= 200 and "__" not in x and (_STEP.search(x) or T._DOING.match(x)) and not T._NOT_INTENT.search(x))
    return bool(lines) and said >= max(1, 0.75 * len(lines))


def rows(stretch):
    """Every line of it is a table's row."""
    return all(x.strip().startswith("|") for x in stretch.split("\n") if x.strip())


def outside(text, start, p):
    """The first line's start outside a code block from `start` on, at most one round of `p` later; None: there is none.
    (A run reaches back over what it shares with the code before it: the end of a block.)"""
    inside = len(_FENCE.findall(text[:start])) % 2 == 1
    if not inside:
        return start
    at = start
    while True:
        at = text.find("\n", at) + 1
        if not at or at > start + p:
            return None
        if len(_FENCE.findall(text[:at])) % 2 == 0:
            return at


def tail_run(text, steps=True):
    """(start, period) of the run `text` ends on, the longest there is, or None. steps: announced steps count sooner."""
    r = text[-WINDOW:][::-1]
    pi, k, soon, late = [0] * len(r), 0, None, None
    for i in range(1, len(r)):  # each end of the text's shortest period (Knuth-Morris-Pratt, over the text reversed)
        while k and r[i] != r[k]:
            k = pi[k - 1]
        k += r[i] == r[k]
        pi[i] = k
        n, p = i + 1, i + 1 - k
        if n >= STEP_CHARS and n >= STEP_REPEATS * p:
            soon = (n, p)
            if (n >= MIN_CHARS and n >= REPEATS * p) or (p >= LONG_ROUND and n >= LONG_CHARS and n >= LONG_REPEATS * p):
                late = (n, p)
    for run, sure in ((late, True), (soon, False)):
        if run is None:
            continue
        n, p = run
        start = outside(text, len(text) - n, p)
        if start is None:
            continue  # code
        n = len(text) - start
        stretch = text[start:start + p]
        words = len(_WORD.findall(bare(stretch)))
        if not words:
            continue  # a rule of dashes
        if sure and not rows(stretch) and (words >= 3 or n >= BARE_CHARS):
            return start, p
        if not sure and steps and announced(stretch):
            return start, p
    return None


class Watch:
    """A reply as it streams. `seen`: what its tools returned this turn, `asked`: the user's last message; lines that are in
    the one, or that the user wrote a few times ("write this 20 times"), may repeat. `steps`: it could make calls."""
    def __init__(self, seen="", asked="", steps=True):
        self.text, self.next, self.seen, self.asked = "", STEP_CHARS, seen, asked
        self.steps = steps and not _ASKED.search(asked)

    def add(self, s):
        """None, or how much of the text to keep (up to the end of the first round) once it goes round in circles. A piece
        that comes all at once is read as it would have come."""
        for i in range(0, len(s), EVERY):
            self.text += s[i:i + EVERY]
            if len(self.text) < self.next:
                continue
            self.next = len(self.text) + EVERY
            run = tail_run(self.text, self.steps)
            if run is not None and not self.allowed(self.text[run[0]:run[0] + run[1]]):
                return run[0] + run[1]
        return None

    def allowed(self, stretch):
        lines = [x for x in (y.strip() for y in stretch.split("\n")) if len(x) >= 12] or [stretch.strip()]
        return any(x in self.seen or 0 < self.asked.count(x) <= 3 for x in lines)


def collapse(text):
    """`text` with each block of lines that only announces steps and comes four times or more in a row (any other: REPEATS
    times and MIN_CHARS long) said once, and how often. Not in code blocks."""
    if len(text) < STEP_CHARS:
        return text
    lines = text.split("\n")
    for _ in range(3):  # rounds of rounds: the inner ones first
        out, i, fence, done = [], 0, False, False
        while i < len(lines):
            if _FENCE.match(lines[i]):
                fence = not fence
            took = 0
            if lines[i].strip():
                for p in range(1, min(80, (len(lines) - i) // 4) + 1):
                    if lines[i + p] != lines[i]:
                        continue
                    block, k = lines[i:i + p], 1
                    while lines[i + k * p:i + (k + 1) * p] == block:
                        k += 1
                    if k < 4:
                        continue
                    said = "\n".join(block)
                    marks = len(_FENCE.findall(said))
                    took = k * p
                    if not fence and marks % 2 == 0 and not rows(said) and (
                            (k * len(said) >= 200 and announced(said)) or (_WORD.search(bare(said)) and (
                                (k >= REPEATS and k * len(said) >= MIN_CHARS)
                                or (len(said) >= LONG_ROUND and k >= LONG_REPEATS and k * len(said) >= LONG_CHARS)))):
                        out += block + [f"(Said {k - 1} more times.)"]
                        done = True
                    else:  # left alone, all of it, and passed over
                        out += lines[i:i + took]
                        fence ^= (marks * k - bool(_FENCE.match(lines[i]))) % 2 == 1  # (its first line's was counted above)
                    break
            if not took:
                out.append(lines[i])
            i += took or 1
        lines = out
        if not done:
            break
    return "\n".join(lines)
