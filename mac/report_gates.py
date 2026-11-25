"""Code checks on each sentence of an Agents research report, before report.md is written (from
a spot-check of the real-model runs: absences the full page contradicts, invented conflicts,
quotes stitched from several places, whole-source citations, open-weight and legal claims no passage states, a page's
metadata cited as its content).

Every check reads only the pages the run read: their full text (title included), the part the model read, and the
stored passages. Nothing here calls a model. A check decides only on specific evidence: a number or version, or two
terms that are rare in the run's own pages (an IDF-like cut, so "release", "page" or "model" never decide anything);
every decision is logged with the terms it used. Each check is a pure function so the tests can feed it the real
failing sentences and their real page texts.
"""
import json
import re

# ---------------------------------------------------------------- words
STOP = set("""a an the and or but nor of to in on at by for from with without into onto over under about above below
after before during than then that this these those there here its it's it is are was were be been being has have had do
does did done can could may might must shall should will would not no none any all each every some more most less least
very also only just even both either neither such same other another which what who whom whose when where why how
whether while as if so too yet per via vs etc""".split())
GATE_STOP = STOP | set("""source sources page pages read material materials text texts article articles site sites document
documents appear appears appeared state states stated give gives given list lists listed mention mentions mentioned say
says said provide provides provided include includes included contain contains contained show shows shown specify
specifies specified explicit explicitly directly clear clearly information detail details data report section sections
cited cite cites passage passages quoted quote quotes part parts cut off point points note notes noted describe
describes described figure figures found find finds offer offers offered public available one two three first second
third whereas disagree disagrees conflict conflicts conflicting contradict contradicts contested inconsistent agree
agrees differ differs different between according however instead rather unclear uncertain thin call calls called
writer author""".split())

MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
MONTH_RE = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
CURRENCY_RE = re.compile(r"[$€£]\s?\d|\b\d[\d,.]*\s?(?:usd|eur|gbp|dollars|euros|pounds)\b", re.I)
TERM_ALT = {
    "pric": CURRENCY_RE, "cost": CURRENCY_RE,
    # a licence is named, not just the word (a "License" column header is no licence)
    "licenc": re.compile(r"apache[- ]2|apache licen|\bmit licen|\(mit\)|\bgpl|\bbsd\b|cc[- ]by|creative commons|licensed under|"
                         r"community licen|openrail|qwen licen|llama \d", re.I),
    "penalty": re.compile(r"penalt|\bfines?\b|\bjail\b|prison|misdemeanou?r|felony|punishable", re.I),
}
TERM_ALT["licens"] = TERM_ALT["licenc"]
TERM_ALT["penalti"] = TERM_ALT["penalty"]

SENT_RE = re.compile(r"[^\n]+?(?:[.!?]+(?=\s|$)|(?=\n)|$)")  # a sentence ends at . ! ? before a space (never inside 4.0)
NUM_RE = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)*")
ISO_RE = re.compile(r"\b(19|20)\d\d-\d\d-\d\d\b")
CITE_TEXT_RE = re.compile(r"\[(?:S\d{1,3}(?:\.\d{1,3})?(?:\s*[,;]\s*S\d{1,3}(?:\.\d{1,3})?)*)[^\]]*\]")


def stem(w):
    """A light stemmer, the same on both sides of every match: ruling/rules -> rul, fixes/fixed -> fix, prices -> pric."""
    for suf in ("ings", "ing", "ed", "es", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 3 and not w.endswith("ss") and not any(c.isdigit() for c in w):
            w = w[:-len(suf)]
            break
    return w[:-1] if len(w) > 3 and w.endswith("e") and not any(c.isdigit() for c in w) else w


def to_iso(text):
    """Dates written as words or slashes, as ISO: "October 9, 2026", "9 Oct 2026", "Nov/23/2023", "2026/10/09" ->
    2026-10-09 (so a date compares equal however a page writes it)."""
    def iso(y, m, d):
        try:
            y, m, d = int(y), int(m), int(d)
        except ValueError:
            return None
        return f"{y:04d}-{m:02d}-{d:02d}" if 1 <= m <= 12 and 1 <= d <= 31 and 1900 <= y <= 2100 else None

    def mon(name):
        return MONTHS.get(name.lower()[:3])
    t = text or ""
    t = re.sub(rf"\b{MONTH_RE}\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+((?:19|20)\d\d)\b",
               lambda m: iso(m.group(3), mon(m.group(1)), m.group(2)) or m.group(0), t, flags=re.I)
    t = re.sub(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?{MONTH_RE}\.?,?\s+((?:19|20)\d\d)\b",
               lambda m: iso(m.group(3), mon(m.group(2)), m.group(1)) or m.group(0), t, flags=re.I)
    t = re.sub(rf"\b{MONTH_RE}/(\d{{1,2}})/((?:19|20)\d\d)\b", lambda m: iso(m.group(3), mon(m.group(1)), m.group(2)) or m.group(0), t, flags=re.I)
    t = re.sub(r"\b((?:19|20)\d\d)/(\d{1,2})/(\d{1,2})\b", lambda m: iso(m.group(1), m.group(2), m.group(3)) or m.group(0), t)
    return t


def words(text):
    """Lower-case word tokens (numbers, versions and dates kept whole: 4.0, 3.54.1, x4, 2026-10-09)."""
    return re.findall(r"[a-z0-9][a-z0-9.+-]*[a-z0-9]|[a-z0-9]", to_iso(text or "").lower().replace("‑", "-"))


def numbers(text):
    """The numbers, versions and dates in a text, normalised: 1,165.09 -> 1165.09; $25,000 -> 25000; 3.54.1 stays
    (3.54.0 is not 3.54); dates as ISO whatever their spelling. Small counts (0-10) and version families (3.53.x) are
    left out."""
    t = to_iso(CITE_TEXT_RE.sub(" ", text or ""))
    out = {m.group(0) for m in ISO_RE.finditer(t)}
    out |= {"md:" + d[5:] for d in list(out)}  # a full date also answers for its month and day
    for m in re.finditer(rf"\b{MONTH_RE}\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?!,?\s+(?:19|20)\d\d)", t, re.I):
        if 1 <= int(m.group(2)) <= 31:
            out.add(f"md:{MONTHS[m.group(1).lower()[:3]]:02d}-{int(m.group(2)):02d}")  # "On June 25" with no year
    t = ISO_RE.sub(" ", t)
    t = re.sub(rf"\b{MONTH_RE}\.?\s+\d{{1,2}}(?:st|nd|rd|th)?\b", " ", t, flags=re.I)
    for mm in NUM_RE.finditer(t):
        if re.match(r"\.[xX*]", t[mm.end():mm.end() + 2]):
            continue
        n = mm.group(0).replace(",", "").rstrip(".")
        if n.isdigit() and int(n) <= 10:
            continue
        out.add(n)
    return out


def norm(text):
    """Text for exact matching: quotes, dashes and spaces plain, Markdown escapes and emphasis gone, lower case."""
    t = (text or "").translate(str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-", " ": " "}))
    t = re.sub(r"\\([\\`*_{}\[\]()#+\-.!|<>~\"'])", r"\1", t)  # Markdown escapes: \_ROWID\_ is _ROWID_
    t = re.sub(r"(?<!\w)[*_`]+|[*`]+(?!\w)|\*\*", "", t)      # emphasis marks (not the underscores inside _ROWID_)
    return re.sub(r"\s+", " ", t).strip().lower()


def ordered_terms(text):
    """The content words of a text in order, once each (citations, small counts, and site names like ipcuria.eu out)."""
    out = []
    for w in words(CITE_TEXT_RE.sub(" ", text or "")):
        t = stem(w)
        if w in GATE_STOP or t in GATE_STOP or not (len(w) >= 3 or any(c.isdigit() for c in w)):
            continue
        if re.fullmatch(r"\d+", w) and int(w) <= 10:
            continue
        if re.fullmatch(r"[a-z][a-z0-9-]*\.[a-z]{2,}(?:\.[a-z]{2,})?", w):  # a site's name: who said it, not what
            continue
        if t not in out:
            out.append(t)
    return out


def key_terms(sentence):
    """The sentence's content words (citations left out): what a page would have to contain to be about the same thing."""
    return set(ordered_terms(sentence))


def is_numberish(t):
    return any(c.isdigit() for c in t)


# ---------------------------------------------------------------- what a sentence claims
ABSENCE_RE = re.compile(
    r"\b(?:do(?:es)?(?:n['’]t| not)|did(?:n['’]t| not)|never)\s+(?:say|state|give|list|mention|specify|include|provide|show|"
    r"contain|offer|report|name|describe|address|cover|disclose|publish|explain)\b|\bgives? no\b|\boffers? no\b|\bprovides? no\b|"
    r"\b(?:no|none of the)\s+(?:[\w,]+\s+){0,6}(?:appears?|is given|are given|is stated|are stated|is listed|are listed|"
    r"is mentioned|are mentioned|anywhere)\b|\bnone of (?:the|these|its) (?:sources|pages|passages)\b|"
    r"\bnone (?:of them )?(?:states?|gives?|lists?|mentions?|names?)\b|"
    r"\bnot (?:stated|given|listed|mentioned|specified|disclosed|published|available) (?:in|on|by)\b|"
    r"\bonly part \d|\b(?:is|are|was|were) cut off\b|\bwithout (?:stating|giving|listing|naming)\b|"
    r"\bsilent on\b|\bno (?:mention|figure|figures|price|prices|date|dates|licen[cs]e|number|numbers|detail|details)\b|"
    r"\b(?:no|none of the) (?:page|source|site|article|passage|document)s?(?: (?:read|here|read here))? (?:gives?|gave|states?|stated|"
    r"lists?|listed|mentions?|mentioned|names?|named|reports?|reported|says?|said|shows?|showed|has|had|includes?|included)\b|"
    r"\bthe only\b(?:\s+[\w-]+){1,5}\s+(?:named|listed|mentioned|given|found|shown|reported|described|cited)\b|"
    r"\b(?:is|are) not (?:shown|explained|described|given|stated) in the (?:material|sources|pages)\b",
    re.I)
UNIVERSAL_RE = re.compile(r"\bat all\b|\bany (?:source|page)\b|\bnone of the (?:sources|pages)\b|\bno (?:source|page)\b|"
                          r"\bthe (?:material|sources)\b|\banywhere\b|\bin (?:the )?(?:material|sources)\b", re.I)
CONFLICT_RE = re.compile(
    r"\b(?:disagree\w*|conflict\w*|contradict\w*|contested|inconsistent\w*|discrepanc\w*|at odds)\b|"
    r"\b(?:do(?:es)?|did)(?:n['’]t| not) agree\b|\b(?:differ|differs|differing)\b|"
    r"\b(?:while|whereas)\b[^.;]{3,120}\b(?:says?|states?|lists?|gives?|reports?|puts?|calls?|describes?|names?|labels?|"
    r"refers? to)\b", re.I)
NEGATED_CONFLICT_RE = re.compile(r"\b(?:not|no|never|without|n['’]t)\s+(?:\w+\s+){0,4}(?:disagree|conflict|contradict|"
                                 r"inconsistent|differ|contested)\w*|\brather than (?:an? |any )?(?:direct |real |open )?"
                                 r"(?:contested|disagree\w*|conflict\w*|contradict\w*|dispute\w*)", re.I)
# status claims, each side of each: the claim's polarity must be what a cited passage says
STATUS = {
    "open": {"claim": re.compile(r"\bopen[- ]?weights?\b|\bopen[- ]source\b(?! (?:data|datasets?|tools?|librar(?:y|ies)|"
                                 r"community|projects?|benchmarks?))|\bproprietary\b|\bclosed[- ](?:weights?|source)\b|"
                                 r"\bweights are (?:not )?(?:available|released)\b", re.I),
             "neg": re.compile(r"\bnot (?:an? )?open|\bproprietary\b|\bclosed\b|\bnot (?:been )?released\b|\bapi[- ]only\b", re.I),
             "pos_ev": re.compile(r"open[- ]?weights?|open[- ]source|hugging ?face|weights (?:are )?(?:available|released)|"
                                  r"available (?:at|on|from)\b|github\.com|download", re.I),
             "neg_ev": re.compile(r"proprietary|closed[- ]?(?:weights?|source)|api[- ]only|not (?:open|released|available)|"
                                  r"no (?:public )?weights", re.I)},
    "licence": {"claim": re.compile(r"\blicen[cs]ed? (?:under|as)\b|\bapache[- ]2|\bmit licen|\bgpl\b|\bcc[- ]by", re.I),
                "neg": re.compile(r"$^"),
                "pos_ev": TERM_ALT["licenc"], "neg_ev": re.compile(r"$^")},
    "legal": {"claim": re.compile(r"\b(?:il)?legal(?:ly|ity)?\b(?! (?:advice|notice|information|protection|disclaimer|team|"
                                  r"department|counsel|text|basis|details?|commentary|analysis|framework|questions?|issues?|"
                                  r"experts?|sources?|news|scholars?|writers?|landscape|research|opinions?|documents?|fees|costs|"
                                  r"relief|remed(?:y|ies)|proceedings|claims?|challenges?|routes?|options?))|\b(?:un)?lawful(?:ly)?\b(?! acquirer)|\bpermitted\b|\bprohibit\w*\b|"
                                  r"\bbanned\b|\bcriminal\b|\bfelony\b|\bmisdemeanou?r\b|\ballowed by law\b", re.I),
              "neg": re.compile(r"\billegal|\bunlawful|\bprohibit|\bbanned\b|\bcriminal\b|\bfelony\b|\bmisdemeanou?r\b|"
                                r"\bnot (?:legal|lawful|permitted|allowed)\b", re.I),
              "pos_ev": re.compile(r"\blegal\b|\blawful|\bpermit|\ballow|\bauthori[sz]|\blicensed\b", re.I),
              "neg_ev": re.compile(r"illegal|unlawful|prohibit|\bban|criminal|felony|misdemeanou?r|penal code|offen[cs]e|"
                                   r"punishable", re.I)},
}


def is_absence(s):
    return bool(ABSENCE_RE.search(s or ""))


def is_conflict(s):
    """The sentence says sources disagree (not that they agree: "do not contradict each other" is no conflict)."""
    s = NEGATED_CONFLICT_RE.sub(" ", s or "")
    return bool(CONFLICT_RE.search(s))


def status_family(s):
    for k, f in STATUS.items():
        if f["claim"].search(s or ""):
            return k
    return None


# ---------------------------------------------------------------- the corpus a report rests on
class Corpus:
    """The run's read pages: sources {id: {final_url, title, hash, read_end, dup_of}}, texts {id: full text} and the
    stored passages {pid: {source_id, text}}."""

    def __init__(self, sources, texts, passages, question=""):
        self.src = {s["id"]: s for s in sources}
        self.texts, self.passages, self.question = texts, passages, question or ""

    def canonical(self, sid):
        seen = set()
        while sid in self.src and self.src[sid].get("dup_of") and sid not in seen:
            seen.add(sid)
            sid = self.src[sid]["dup_of"]
        return sid

    def full(self, sid):
        s = self.src.get(sid) or {}
        return (s.get("title") or "") + "\n" + (self.texts.get(sid) or "")

    def read_end(self, sid):
        s = self.src.get(sid) or {}
        return len(s.get("title") or "") + 1 + int(s.get("read_end") if s.get("read_end") is not None else len(self.texts.get(sid) or ""))

    def passage_text(self, pid):
        return (self.passages.get(pid) or {}).get("text") or ""



def _has(term, low, toks, nums):
    alt = TERM_ALT.get(term)
    if alt is not None:
        return bool(alt.search(low))
    if term in toks:
        return True
    if re.fullmatch(r"(?:19|20)\d\d-\d\d-\d\d", term):  # a date: "On June 25" in a 2026 page answers for 2026-06-25
        return term in nums or "md:" + term[5:] in nums
    if re.fullmatch(r"\d+(?:\.\d+)*", term):  # a number, as numbers() reads the window
        return term in nums
    return is_numberish(term) and re.search(r"(?<![\w.])" + re.escape(term) + r"(?!\d)", low) is not None


# ---------------------------------------------------------------- disagreement
def page_url_key(url):
    """The address a page is known by: no scheme, no fragment, no www., no trailing slash; the query stays (it can name
    a different page). Tracking parameters are gone before this (strip_tracking)."""
    m = re.match(r"^[a-z]+://([^/?#]+)([^?#]*)(\?[^#]*)?", (url or "").strip(), re.I)
    if not m:
        return (url or "").strip().lower()
    host = m.group(1).lower()
    host = host[4:] if host.startswith("www.") else host
    return host + (m.group(2).rstrip("/") or "") + (m.group(3) or "")


# ---------------------------------------------------------------- quotes
QUOTE_CHARS = '"“”'


def quote_spans(sentence):
    """The quoted spans of a sentence, as (start, end, inner) with the marks, or None when its quote marks do not pair
    (an odd count: the check is skipped rather than guessed)."""
    marks = [i for i, c in enumerate(sentence) if c in QUOTE_CHARS]
    if len(marks) % 2:
        return None
    return [(a, b + 1, sentence[a + 1:b]) for a, b in zip(marks[0::2], marks[1::2])]


def quote_found(inner, corpus, sids=None):
    """A quote is the pages' words when its pieces (split at editorial [brackets] and ellipses) appear in order in one
    read page (title included) or one stored passage. Punctuation the writer put inside the closing mark is not part of
    the quote; "[a]ny" matches "Any". `sids` limits the search to those pages and their passages."""
    q = norm(inner).strip(" ,.;:!?")
    pieces = [p.strip(" ,.;:") for p in re.split(r"\[[^\]]{0,40}\]|…|\.\.\.", q) if len(p.strip(" ,.;:")) >= 3]
    if not pieces:
        return True
    hay = [norm(corpus.full(sid)) for sid in corpus.src if sids is None or corpus.canonical(sid) in sids] + \
        [norm(p.get("text")) for p in corpus.passages.values() if sids is None or corpus.canonical(p.get("source_id")) in sids]
    for h in hay:
        at = 0
        for p in pieces:
            k = h.find(p, at)
            if k < 0:
                break
            at = k + len(p)
        else:
            return True
    return False


# ---------------------------------------------------------------- the 7b round: code for quotes and numbers only
IDENT_RE = re.compile(r"\b[A-Z]{1,3}-\d+/\d+\b|(?:\b(?:Case|No\.?|Section|Article|Art\.|Directive|Regulation|Rule|Bill|AB|SB|HB)|§+)"
                      r"\s*[A-Z]?[-\s]?\d[\w./-]*|\b\d+\.\d+\.\d+(?:\.\d+)*\b|\b[A-Za-z]+[-_]?\d+[A-Za-z][\w-]*\b", re.I)
APPROX_RE = re.compile(r"\\?~\s*|\b(?:approx(?:imately|\.)?|about|around|roughly|nearly|almost|some|over|under|up to|at least|"
                       r"more than|less than)\s+", re.I)


def claim_numbers(sentence):
    """The numbers a sentence states as figures: identifiers left out (case and section numbers, bill numbers, version
    strings, names with digits such as C-128/11, § 337a, 3.54.1, Qwen3-VL-4B), "~" and "approx." read past."""
    t = IDENT_RE.sub(" ", CITE_TEXT_RE.sub(" ", sentence or ""))
    return numbers(APPROX_RE.sub(" ", t))


def page_numbers(corpus, sid):
    """Every number on a page's full text (title excluded: the page's own words), cached."""
    cache = corpus.__dict__.setdefault("_page_nums", {})
    if sid not in cache:
        cache[sid] = numbers(APPROX_RE.sub(" ", norm(corpus.texts.get(sid) or "")))
    return cache[sid]


def missing_on_pages(sentence, sids, corpus):
    """The sentence's figures on none of its cited pages' full text (nor in the question); a date the page writes
    without its year answers for it. Pages, not passages: a figure one sentence beyond the stored passage counts."""
    have = numbers(corpus.question)
    for sid in sids:
        have |= page_numbers(corpus, corpus.canonical(sid))
    for a, b, inner in quote_spans(sentence) or []:  # a quote of a page's own words (its title too) carries its figures
        if len(inner.split()) >= 4 and quote_found(inner, corpus):
            sentence = sentence.replace(inner, " ")
    out = []
    for n in sorted(claim_numbers(sentence)):
        if n in have or n.startswith("md:") or (ISO_RE.fullmatch(n) and "md:" + n[5:] in have):
            continue
        if re.fullmatch(r"\d+\.\d+", n) and any(h.startswith(n + ".") for h in have):  # "the 3.53 line" beside 3.53.1
            continue
        out.append(n)
    return out


# ---------------------------------------------------------------- the 7b round: candidates for the model's check
REFERENCE_RE = re.compile(r"\b(?:in (?:that|this|the same) (?:case|test|study|review|ruling|judgment)|(?:that|the same|this) "
                          r"(?:case|court|ruling|judgment|decision|test|study|review|survey|benchmark|report|article|source|"
                          r"page|judge|tribunal))\b", re.I)
CLAIM_VERB_RE = re.compile(r"\b(?:held|holds|ruled|rules|found|finds|granted|grants|refused|refuses|rejected|dismissed|upheld|"
                           r"ordered|awarded|fined|measured|recorded|concluded|decided|showed|shows|reported|scored|dropped|"
                           r"rose|fell|took|reached|hit)\b", re.I)
ABBREV_END_RE = re.compile(r"(?:\b(?i:approx|ca|cf|vs|viz|est|incl|esp|e\.g|i\.e)\.|(?<![\w.])(?:No|Nos|Art|Arts|Fig|Figs|"
                           r"Inc|Ltd|Co|Corp|St|Dr|Mr|Mrs|Ms|Jr|Sr|Vol|Ch|Sec|para|pp?|v|U\.S|U\.K|Jan|Feb|Mar|Apr|Jun|Jul|"
                           r"Aug|Sep|Sept|Oct|Nov|Dec)\.)$")


def refers_back(sentence):
    """A fact tied to something an earlier sentence names: "the court in that case granted", "It measured 16 s"."""
    return bool((REFERENCE_RE.search(sentence or "") or ANAPHOR_RE.match(sentence or "")) and CLAIM_VERB_RE.search(sentence or ""))


def split_sentences(text):
    """A line's sentences: split after . ! ? before a capital, digit, quote or bracket, but never after an abbreviation
    ("approx. 2,000", "e.g. Windows", "No. 5", "v. Oracle")."""
    out = []
    for p in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"“(*])", text or ""):
        if out and ABBREV_END_RE.search(out[-1]):
            out[-1] += " " + p
        else:
            out.append(p)
    return out


ANAPHOR_RE = re.compile(r"^\W*(?:the same|this|these|that|those|it|its|they|their|such|both|either|neither|the latter|the former|"
                        r"here|there)\b", re.I)


def candidate_kind(sentence):
    """Which judgement a sentence needs from the verification pass, or None: a disagreement, a status (open weights,
    licence, legality), a fact tied to a case or test named earlier ("the court in that case granted"), or an absence.
    Picking only; the model decides."""
    if is_conflict(sentence):
        return "conflict"
    if refers_back(sentence):
        return "reference"
    if status_family(sentence):
        return "status"
    if is_absence(sentence):
        return "absence"
    return None


def coverage(corpus, sid):
    s = corpus.src.get(sid) or {}
    n, end = int(s.get("chars") or len(corpus.texts.get(sid) or "")), s.get("read_end")
    if end is None or end >= n - 50:
        return "read in full"
    return f"part {s.get('parts') or 1} of {s.get('parts_total') or '?'} read before writing: the first {end:,} of {n:,} characters"


def evidence(sentence, sids, corpus, room=1400, every_page=False):
    """For the verification pass: for each page (the cited ones, or every page read for a claim about all the
    material), its coverage and the windows of its full text that share the most of the sentence's terms, marked when
    they come from a part read only now. About `room` characters a page."""
    terms = key_terms(sentence) | claim_numbers(sentence)
    pool = list(corpus.src) if every_page else [corpus.canonical(x) for x in sids]
    out = []
    for sid in dict.fromkeys(pool):
        text = corpus.texts.get(sid) or ""
        if not text:
            continue
        end = (corpus.src.get(sid) or {}).get("read_end")
        spans = [(m.start(), m.end()) for m in SENT_RE.finditer(text) if m.group(0).strip()]
        scored = []
        for i, (a, b) in enumerate(spans):
            seg = text[a:b]
            low, toks, nums = to_iso(norm(seg)), {stem(x) for x in words(seg)}, numbers(seg)
            n = sum(1 for t in terms if _has(t, low, toks, nums))
            if n:
                scored.append((n, i))
        picked, used = [], 0
        # the best window of a part not read before writing first (where a page would contradict an absence), then the rest
        past = lambda i: end is not None and (spans[i][0] + spans[i][1]) / 2 >= end  # noqa: E731
        best_unread = sorted((x for x in scored if past(x[1])), key=lambda x: (-x[0], x[1]))[:1]
        for n, i in best_unread + sorted(scored, key=lambda x: (-x[0], x[1])):
            a, b = spans[max(0, i - 1)][0], spans[min(len(spans) - 1, i + 1)][1]
            if any(a < pb and pa < b for pa, pb, _ in picked) or used + (b - a) > room:
                continue
            picked.append((a, b, end is not None and (spans[i][0] + spans[i][1]) / 2 >= end))  # its centre sentence lies past the part read
            used += b - a
        parts = []
        for a, b, later in sorted(picked):
            parts.append(("[from a part not read before writing] " if later else "") + re.sub(r"\s+", " ", text[a:b]).strip())
        out.append({"sid": sid, "coverage": coverage(corpus, sid), "windows": parts or ["(no sentence of this page shares its terms)"]})
    return out


VERDICTS = ("supported", "unsupported", "needs_scope")


SPAN_MARK_RE = re.compile(r"^\s*(?:-\s*)?\[from a part not read before writing\]\s*")


def parse_verdicts(text, n):
    """The verification reply as {index: (verdict, corrected, span)}, span being the source words the verdict rests on;
    anything unreadable is left out (the sentence stays)."""
    m = re.search(r"\[.*\]", text or "", re.S)
    try:
        arr = json.loads(m.group(0)) if m else None
    except ValueError:
        arr = None
    if not isinstance(arr, list):  # a reply cut off at its cap, or with stray text: its whole objects still count
        arr = []
        for o in re.findall(r"\{[^{}]*\}", text or ""):
            try:
                arr.append(json.loads(o))
            except ValueError:
                pass
    out = {}
    for x in arr if isinstance(arr, list) else []:
        if not isinstance(x, dict):
            continue
        try:
            i = int(x.get("i"))
        except (TypeError, ValueError):
            continue
        v = str(x.get("verdict") or "").strip().lower()
        if 1 <= i <= n and v in VERDICTS:
            span = SPAN_MARK_RE.sub("", str(x.get("span") or "")).strip().strip('"“”')[:400]
            out[i] = (v, str(x.get("corrected") or "").strip()[:400], span)
    return out
