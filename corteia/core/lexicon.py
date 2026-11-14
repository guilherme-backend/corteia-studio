"""Portuguese (and English) language signals used by the clip scorer and by the
subtitle highlighter.

This is a real, auditable feature layer: every score contribution names the
terms that produced it, so a user can open the tooltip of a clip and see
"porque: 'ninguém sabe', 'verdade', pergunta".  Nothing here is random.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter

# --------------------------------------------------------------------------- #
# normalisation
# --------------------------------------------------------------------------- #
_WS = re.compile(r"\s+")


def strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def norm_word(word: str) -> str:
    w = strip_accents((word or "").strip().lower())
    return re.sub(r"[^0-9a-z_'-]", "", w)


def tokens(text: str) -> list[str]:
    return [w for w in re.split(r"[^0-9A-Za-z'_-]+", strip_accents(text or "").lower()) if w]


def sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?…])\s+", (text or "").strip())
    return [p for p in parts if p.strip()]


# --------------------------------------------------------------------------- #
# stopwords
# --------------------------------------------------------------------------- #
STOPWORDS = frozenset("""
a o as os um uma uns umas de do da dos das em no na nos nas em o à às ao pelos pelas
que e ou mas se por para com sem sobre entre como mais menos muito pouco já sim não
ser é são foi era eram será serão ter tem tinha tinhahamos hemos há
ele ela eles elas você vocês teu seu sua seus suas meu minha nosso nossa
esse essa isso este esta isto aquele aquela aquilo
então aí tipo assim né tá tá ok então enfim
""".split())

# filler that adds nothing: used to measure information density
FILLERS = frozenset("""
né tá hum eh então aí tipo assim sei lá entendeu sabe cara mano vlw
""".split())

# --------------------------------------------------------------------------- #
# engagement lexicons (weights are contributions, later normalised)
# --------------------------------------------------------------------------- #
HOOKS = {  # openers that create curiosity / retention
    "ninguém conta": 1.0, "ninguem conta": 1.0, "segredo": 0.9, "segredo revelado": 1.2,
    "o que ninguém": 1.3, "verdade": 0.7, "mentira": 0.8, "erro": 0.7, "errado": 0.5,
    "impossível": 0.9, "nunca": 0.7, "jamais": 0.7, "secreto": 0.8, "inédito": 0.8,
    "polêmico": 1.0, "polêmica": 1.0, "exclusivo": 0.8, "revela": 0.7, "revelou": 0.8,
    "descobri": 0.8, "descobrir": 0.7, "olha isso": 0.9, "presta atenção": 0.9,
    "escuta": 0.5, "imagina": 0.6, "e se": 0.9, "por que": 0.6, "porque": 0.3,
    "a verdade é": 1.2, "a real é": 1.0, "na prática": 0.8, "sem corte": 0.4,
    "atenção": 0.6, "cuidado": 0.7, "perigo": 0.9, "urgente": 0.8, "última chance": 1.0,
    "antes e depois": 1.0, "do zero": 0.7, "milhões": 0.8, "bilhão": 0.9, "grátis": 0.7,
    "parece": 0.3, "prepare-se": 0.7, "vai mudar": 0.9, "mudou tudo": 1.0,
    "the truth": 0.9, "nobody": 0.9, "secret": 0.8, "what if": 0.8, "insane": 0.8,
}

EMOTION = {  # high-arousal words drive comments/shares
    "incrível": 1.0, "absurdo": 1.0, "insano": 0.9, "maluco": 0.8, "loucura": 0.9,
    "amei": 0.9, "odiado": 0.7, "amo": 0.7, "medo": 0.9, "assustado": 0.9, "susto": 0.9,
    "chorar": 1.0, "chorando": 0.9, "emocionante": 1.1, "emoção": 0.9, "arrepiou": 1.0,
    "engraçado": 1.0, "haha": 1.2, "kkk": 1.3, "risada": 1.1, "rir": 0.9, "piada": 0.8,
    "zoeira": 0.9, "humor": 0.6, "brincadeira": 0.6, "melhor": 0.7, "pior": 0.8,
    "amizade": 0.6, "traição": 1.0, "traído": 1.0, "mentira": 0.9, "vergonha": 0.9,
    "raiva": 0.9, "briga": 1.1, "discussão": 1.0, "discutir": 0.9, "polêmica": 1.0,
    "gritou": 0.9, "grito": 0.8, "silêncio": 0.5, "respeito": 0.6, "justiça": 0.8,
    "liberdade": 0.7, "superação": 0.9, "venceu": 0.8, "perdeu": 0.7, "ganhou": 0.7,
    "dinheiro": 0.7, "riqueza": 0.8, "pobre": 0.7, "fome": 0.7, "guerra": 0.9,
    "morte": 1.0, "vida": 0.5, "família": 0.7, "filho": 0.6, "mãe": 0.6, "amor": 0.8,
    "surpresa": 1.0, "surpreendido": 0.9, "inesperado": 1.0, "shock": 0.9, "wow": 1.0,
    "amazing": 0.9, "funny": 0.9, "crazy": 0.9, "angry": 0.8, "love": 0.6, "hate": 0.8,
}

RESOLUTION = {  # closers: a clip that ends on one of these feels complete
    "então é isso": 1.0, "resumo": 0.9, "conclusão": 1.0, "ou seja": 0.8, "por isso": 0.9,
    "no fim": 0.9, "ao final": 0.9, "resumindo": 1.0, "ou seja": 0.8, "entendeu": 0.5,
    "saquei": 0.6, "é isso": 0.8, "fechado": 0.7, "combinado": 0.6, "pra você": 0.5,
    "a lição": 1.1, "o que fica": 1.1, "aprendi": 0.9, "aprende": 0.8, "dica": 0.9,
    "conselho": 0.9, "faz o seguinte": 1.0, "anota": 0.7, "lembra": 0.7, "guarda isso": 0.9,
    "in summary": 0.9, "so yeah": 0.7, "lesson": 0.8, "takeaway": 0.9,
}

DEBATABLE = {  # words that generate comments
    "concorda": 1.0, "discorda": 1.1, "opinião": 0.9, "achismo": 0.8, "para mim": 0.7,
    "eu acho": 0.8, "eu discordo": 1.1, "não concordo": 1.1, "polêmica": 1.0,
    "qualquer um": 0.7, "todo mundo": 0.8, "ninguém": 0.8, "sempre": 0.5, "nunca": 0.7,
    "melhor": 0.7, "pior": 0.8, "culpa": 0.9, "responsável": 0.7, "justo": 0.8, "injusto": 0.9,
    "concordo": 0.7, "impossível": 0.9, "possível": 0.4, "provado": 0.7, "mito": 0.9,
    "agree": 0.7, "disagree": 1.0, "opinion": 0.7, "everyone": 0.6, "nobody": 0.8,
}

NAMES_HINT = {  # authority / celebrity mentions
    "depois": 0.0,
}

CURRENCY = re.compile(r"(r\$|\br$|dólar|real|reais|dolar|usd|€|\$)\s?\d|\d{1,3}(\.\d{3})*\s?(reais|dólares|usd|mil|k\b|mi\b|bi\b)", re.I)
NUMBERS = re.compile(r"\b\d{1,3}([.,]\d+)?\s?(%|por cento|anos|meses|dias|horas|minutos|segundos|mil|milhões|bilhões|pessoas|vezes|km|kg)\b", re.I)
QUESTIONS = re.compile(r"\?")
EXCLAMATIONS = re.compile(r"!|‼")
CAPS_RATIO = re.compile(r"[A-ZÀ-Ú]{4,}")
LAUGHTER = re.compile(r"\b(k+kk+|ha{2,}|rs{2,}|jajaja|lmao|lol)\b", re.I)
SECOND_PERSON = re.compile(r"\b(você|vocês|tu|teu|sua|seu|pra você|para você|you|your)\b", re.I)
FIRST_PERSON = re.compile(r"\b(eu|minha|meu|me|nós|nosso|minha história|contar)\b", re.I)
STORY = re.compile(r"\b(um dia|na época|quando eu|contei|história|vez que|lembro|comecei|aconteceu)\b", re.I)
TENSION = re.compile(r"\b(mas|porém|contudo|só que|no entanto|apesar|problema|difícil|complicado|deu errado)\b", re.I)
PAYOFF = re.compile(r"\b(aí|então|no final|no fim|deu certo|resultado|consegui|resolveu|descobri)\b", re.I)


# --------------------------------------------------------------------------- #
# matching helpers
# --------------------------------------------------------------------------- #
def _haystack(text: str) -> str:
    return " " + _WS.sub(" ", strip_accents((text or "").lower())) + " "


def hits(text: str, lexicon: dict[str, float]) -> tuple[float, list[str]]:
    """Weighted sum of lexicon terms found, plus the terms themselves.

    Multi-word entries are matched as substrings; single words as tokens, so
    ``"verdade"`` does not fire inside ``"verdadeiramente"``.
    """
    hay = _haystack(text)
    total, found = 0.0, []
    for term, weight in lexicon.items():
        t = strip_accents(term.lower())
        if not weight:
            continue
        if " " in t:
            if f" {t} " in hay or f" {t}," in hay or f" {t}?" in hay or f" {t}!" in hay:
                total += weight
                found.append(term)
        else:
            if re.search(rf"(?<![\w'-]){re.escape(t)}(?![\w'-])", hay):
                total += weight
                found.append(term)
    return total, found


def word_weight(word: str) -> float:
    """0..1 importance of a single word — drives subtitle highlighting."""
    w = norm_word(word)
    if not w or w in STOPWORDS:
        return 0.0
    score = 0.0
    if w in EMOTION:
        score = max(score, EMOTION[w])
    if w in HOOKS:
        score = max(score, HOOKS[w])
    if w in RESOLUTION:
        score = max(score, RESOLUTION[w] * 0.9)
    if w in DEBATABLE:
        score = max(score, DEBATABLE[w] * 0.9)
    if re.fullmatch(r"\d[\d.,%]*", w):
        score = max(score, 0.75)
    if w.endswith(("mente", "ção", "sia", "ância", "ez")) and len(w) > 7:
        score = max(score, 0.45)          # abstract nouns usually carry the payload
    if len(w) >= 8 and w not in FILLERS:
        score = max(score, 0.40)
    if word.strip()[:1].isupper() and len(w) > 2 and w not in ("eu",):
        score = max(score, 0.30)          # likely a proper noun
    return min(1.0, score)


# --------------------------------------------------------------------------- #
# text features
# --------------------------------------------------------------------------- #
FEATURE_KEYS = (
    "hook", "emotion", "resolution", "debate", "question", "exclaim", "numbers",
    "second_person", "story", "tension", "payoff", "laughter", "density",
    "sentences", "words", "caps", "currency",
)


def text_features(text: str) -> tuple[dict[str, float], dict[str, list[str]]]:
    """Auditable language features of a transcript window plus the terms found.

    Returns ``(features, matched_terms)``.  Feature values are raw (un-normalised)
    sums: :mod:`corteia.core.analyze` normalises them across candidates.
    """
    if not text:
        return {k: 0.0 for k in FEATURE_KEYS}, {"hook_terms": [], "emotion_terms": [],
                                               "resolution_terms": [], "debate_terms": []}
    tk = tokens(text)
    n = max(1, len(tk))
    content = [w for w in tk if w not in STOPWORDS]
    hook, hook_terms = hits(text, HOOKS)
    emo, emo_terms = hits(text, EMOTION)
    res, res_terms = hits(text, RESOLUTION)
    deb, deb_terms = hits(text, DEBATABLE)
    first = " ".join(tk[:6])
    hook_first, _ = hits(first, HOOKS)
    feats = {
        "hook": hook + hook_first * 1.5,
        "emotion": emo,
        "resolution": res,
        "debate": deb,
        "question": len(QUESTIONS.findall(text)) * 0.9,
        "exclaim": min(3.0, len(EXCLAMATIONS.findall(text)) * 0.6),
        "numbers": len(NUMBERS.findall(text)) * 0.7 + len(CURRENCY.findall(text)) * 0.9,
        "second_person": min(3.0, len(SECOND_PERSON.findall(text)) * 0.35),
        "story": 1.1 if STORY.search(text) else 0.0,
        "tension": min(2.5, len(TENSION.findall(text)) * 0.6),
        "payoff": 1.0 if PAYOFF.search(text) else 0.0,
        "laughter": 1.4 if LAUGHTER.search(text) else 0.0,
        "density": len(content) / n,
        "sentences": float(len(sentences(text))),
        "words": float(len(tk)),
        "caps": min(1.5, len(CAPS_RATIO.findall(text)) * 0.4),
        "currency": 0.8 if CURRENCY.search(text) else 0.0,
    }
    terms = {"hook_terms": hook_terms, "emotion_terms": emo_terms,
             "resolution_terms": res_terms, "debate_terms": deb_terms}
    return feats, terms


def keywords(text: str, limit: int = 6) -> list[str]:
    """Content words ranked by frequency × importance, bigrams included when they
    clearly beat the unigrams (e.g. "verdade ninguém conta")."""
    tk = [w for w in tokens(text) if w not in STOPWORDS and len(w) > 2 and w not in FILLERS]
    if not tk:
        return []
    counts = Counter(tk)
    scored = [(counts[w] * (0.6 + word_weight(w)), w) for w in counts]
    bigrams = Counter(zip(tk, tk[1:]))
    for (a, b), c in bigrams.items():
        if c >= 2 and (word_weight(a) + word_weight(b)) > 0.8:
            scored.append((c * (0.9 + (word_weight(a) + word_weight(b)) / 2), f"{a} {b}"))
    scored.sort(key=lambda x: (-x[0], x[1]))
    out, seen = [], set()
    for _, w in scored:
        if w in seen:
            continue
        seen.add(w)
        out.append(w)
        if len(out) >= limit:
            break
    return out


def make_title(text: str, fallback: str = "Momento do vídeo") -> str:
    """Deterministic short title: best sentence, cleaned, ≤ 62 characters.

    Preference order — a sentence containing an emotion/hook term, then a
    question, then the shortest sentence ≥ 4 words, then the first sentence.
    """
    sents = sentences(text)
    if not sents:
        return fallback
    scored: list[tuple[float, str]] = []
    for i, s in enumerate(sents):
        w = len(tokens(s))
        if w < 2:
            continue
        hook, _ = hits(s, HOOKS)
        emo, _ = hits(s, EMOTION)
        res, _ = hits(s, RESOLUTION)
        bonus = 0.0
        if s.strip().endswith("?"):
            bonus += 1.2
        if QUESTIONS.search(s):
            bonus += 0.6
        if w > 22:
            bonus -= 1.5            # too long to be a title
        if i == 0:
            bonus += 0.35
        scored.append((hook * 1.3 + emo * 1.1 + res * 0.5 + bonus, s))
    if not scored:
        return _trim(sents[0])
    scored.sort(key=lambda x: -x[0])
    return _trim(scored[0][1]) or fallback


def _trim(sentence: str, limit: int = 62) -> str:
    s = _WS.sub(" ", (sentence or "")).strip(" -–—\"'“”,.;:")
    if len(s) <= limit:
        return s[:1].upper() + s[1:] if s else s
    cut = s[:limit]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(",.;: ") + "…"


def describe(text: str, kws: list[str], duration: float, limit: int = 150) -> str:
    """One-sentence content summary built from the window's own words."""
    clean = _WS.sub(" ", (text or "")).strip()
    if not clean:
        return f"Trecho de {int(duration)} s sem fala identificada."
    short = clean if len(clean) <= limit else clean[:limit].rsplit(" ", 1)[0] + "…"
    tail = f" Temas: {', '.join(kws[:3])}." if kws else ""
    return short + tail
