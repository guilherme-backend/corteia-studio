"""Avaliação de qualidade dos cortes em material REAL com licença aberta.

Não é um teste unitário: mede coisas que um usuário de verdade reclama.

    python scripts/quality_real.py [--model small] [--limit 2]

Métricas por vídeo
------------------
início    : o corte começa numa fronteira de fala (pausa >= 0,35 s ou início de
            sentença) e quantas palavras já se passaram quando a primeira
            sentença termina (contexto de abertura);
fim       : o corte termina em pontuação forte ou em pausa, e quantas palavras
            ficam "penduradas" depois da última pontuação;
repetição : sobreposição temporal (IoU) e similaridade lexical entre cortes;
score     : distribuição, correlação de Spearman com um proxy independente de
            engajamento (presença de gancho, pergunta, número, nome próprio e
            densidade de conteúdo na abertura);
potencial : fração de cortes que abrem com gancho e fecham uma ideia completa.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from corteia.core import analyze as AN            # noqa: E402
from corteia.core import lexicon as LX            # noqa: E402
from corteia.core import media, silence as SIL    # noqa: E402
from corteia.core import transcribe as TR         # noqa: E402
from corteia.models import AnalysisOptions        # noqa: E402

HERE = Path(__file__).resolve().parent.parent / "media-test"

#: gancho de abertura: pergunta, contraste, número, promessa ou primeira pessoa
HOOK_RE = re.compile(
    r"\b(você|vocês|por que|porque|como|quanto|nunca|sempre|o segredo|"
    r"a verdade|imagine|olha|repara|principal|primeiro|último|errado|certo|"
    r"what|why|how|really|never|always|secret|imagine|number one|most)\b",
    re.IGNORECASE)
STRONG_PUNCT = (".", "!", "?", "…")


def words_of(tr, t0: float, t1: float):
    out = []
    for seg in tr.segments:
        for w in seg.words:
            if w.start >= t0 - 0.001 and w.end <= t1 + 0.001:
                out.append(w)
    return out


def sentence_positions(tr) -> tuple[set[float], dict[float, str]]:
    """Inícios de sentença e o pontuação que fechou cada fim."""
    starts: set[float] = set()
    ends: dict[float, str] = {}
    for seg in tr.segments:
        for i, w in enumerate(seg.words):
            txt = (w.text or "").strip()
            if not txt:
                continue
            if i == 0 or txt[0].isupper() or re.search(r"[.!?…]\s*$", prev(seg, i)):
                starts.add(round(w.start, 2))
        if seg.words:
            last = (seg.words[-1].text or "").strip()
            if last and last[-1] in STRONG_PUNCT:
                ends[round(seg.words[-1].end, 2)] = last[-1]
    return starts, ends


def prev(seg, i) -> str:
    return (seg.words[i - 1].text or "") if i > 0 else ""


def gap_before(tr, t: float) -> float:
    """Silêncio imediatamente antes de ``t`` (0 se a fala emenda)."""
    best = 0.0
    for seg in tr.segments:
        for w in seg.words:
            if w.end <= t + 0.02:
                best = max(best, 0.0)
            if abs(w.end - t) < 0.35:
                # procura a próxima palavra depois de t
                nxt = min((x.start for s in tr.segments for x in s.words
                           if x.start >= t - 0.02), default=t)
                best = max(best, nxt - w.end)
    return best


def last_punct_gap(text: str) -> int:
    """Quantas palavras ficam depois da última pontuação forte (0 = fechou ideia)."""
    toks = text.split()
    for i in range(len(toks) - 1, -1, -1):
        if toks[i] and toks[i][-1] in STRONG_PUNCT:
            return len(toks) - 1 - i
    return len(toks)


def first_sentence_words(text: str) -> int:
    """Palavras até o fim da primeira sentença (abertura precisa de contexto)."""
    toks = text.split()
    for i, t in enumerate(toks):
        if t and t[-1] in STRONG_PUNCT:
            return i + 1
    return len(toks)


def jaccard(a: str, b: str) -> float:
    A = set(re.findall(r"[a-zà-ÿ]{3,}", a.lower()))
    B = set(re.findall(r"[a-zà-ÿ]{3,}", b.lower()))
    if not A or not B:
        return 0.0
    return len(A & B) / len(A | B)


def iou(a, b) -> float:
    i = max(0.0, min(a.end, b.end) - max(a.start, b.start))
    u = (a.end - a.start) + (b.end - b.start) - i
    return i / u if u > 0 else 0.0


def spearman(xs: list[float], ys: list[float]):
    if len(xs) < 3:
        return None, None

    def rank(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        for pos, idx in enumerate(order):
            r[idx] = pos + 1
        return r

    rx, ry = rank(xs), rank(ys)
    n = len(xs)
    d2 = sum((a - b) ** 2 for a, b in zip(rx, ry))
    rho = 1 - (6.0 * d2) / (n * (n ** 2 - 1))
    # erro padrão aproximado de Spearman
    se = 1.0 / math.sqrt(max(1, n - 3))
    return rho, se


def evaluate(path: Path, model: str, opts: AnalysisOptions, lang: str | None) -> dict:
    t0 = time.time()
    info = media.probe_or_raise(path)
    tr = TR.transcribe(path, model=model, language=lang)
    tt = time.time() - t0
    sil = SIL.detect_silences(path, db=-35.0, min_ms=350)
    scenes = []
    try:
        from corteia.core import scenes as SC
        scenes = SC.detect_scenes(path, threshold=27.0, min_scene_len=15)
    except Exception as exc:                          # noqa: BLE001
        print(f"   (cenas indisponíveis: {type(exc).__name__})")
    cands, clips = AN.plan_clips(
        tr, options=opts, source_path=str(path), duration=info.duration,
        silences=sil, scenes=scenes)

    starts, ends = sentence_positions(tr)
    rows = []
    for c in clips:
        ws = words_of(tr, c.start, c.end)
        text = " ".join((w.text or "").strip() for w in ws).strip() or c.transcript
        gap_start = gap_before(tr, c.start)
        near_start = min((abs(s - c.start) for s in starts), default=99.0)
        prox_end = min((abs(e - c.end) for e in ends), default=99.0) if ends else 99.0
        tail_punct = last_punct_gap(text)
        open_words = first_sentence_words(text)
        hook = bool(HOOK_RE.search(" ".join(text.split()[:14])))
        has_number = bool(re.search(r"\d", text))
        has_question = "?" in text
        rows.append({
            "start": round(c.start, 2), "end": round(c.end, 2),
            "dur": round(c.duration, 2), "score": round(c.score.total, 1),
            "words": len(ws), "chars": len(text),
            "gap_start_s": round(gap_start, 2),
            "dist_sentenca_inicial_s": round(near_start, 2),
            "dist_fim_sentenca_s": round(prox_end, 2),
            "palavras_penduradas_no_fim": tail_punct,
            "palavras_ate_fim_da_1a_sentenca": open_words,
            "abre_com_gancho": hook, "tem_numero": has_number,
            "tem_pergunta": has_question,
            "falantes": int(c.speaker_count or 0), "planos": int(c.scene_count or 0),
            "texto": text[:260], "titulo": c.title,
        })

    overlaps = [iou(a, b) for i, a in enumerate(clips) for b in clips[i + 1:]]
    sims = [jaccard(a.transcript, b.transcript) for i, a in enumerate(clips) for b in clips[i + 1:]]
    proxy = [sum([10 * r["abre_com_gancho"], 5 * r["tem_pergunta"], 3 * r["tem_numero"],
                  -min(8, r["palavras_penduradas_no_fim"]),
                  -max(0, 12 - r["palavras_ate_fim_da_1a_sentenca"])]) for r in rows]
    scores = [r["score"] for r in rows]
    rho, se = spearman(scores, proxy) if len(scores) >= 3 else (None, None)

    return {
        "arquivo": path.name, "duracao_s": round(info.duration, 1),
        "resolucao": f"{info.width}x{info.height}", "fps": round(info.fps or 0, 2),
        "transcricao_s": round(tt, 1), "palavras": sum(len(s.words) for s in tr.segments),
        "trechos": len(tr.segments), "silencios": len(sil), "planos": len(scenes),
        "cortes": len(clips),
        "duracoes": [r["dur"] for r in rows],
        "scores": scores,
        "início_em_fronteira_de_fala": f"{sum(1 for r in rows if r['gap_start_s'] >= 0.15)}"
                                       f"/{len(rows)}",
        "início_perto_de_sentenca": f"{sum(1 for r in rows if r['dist_sentenca_inicial_s'] <= 0.8)}"
                                    f"/{len(rows)}",
        "fim_em_pontuacao_ou_pausa": f"{sum(1 for r in rows if r['dist_fim_sentenca_s'] <= 1.2)}"
                                     f"/{len(rows)}",
        "sem_palavra_pendurada": f"{sum(1 for r in rows if r['palavras_penduradas_no_fim'] == 0)}"
                                 f"/{len(rows)}",
        "penduradas_mediana": (statistics.median([r["palavras_penduradas_no_fim"] for r in rows])
                               if rows else None),
        "abertura_com_gancho": f"{sum(1 for r in rows if r['abre_com_gancho'])}/{len(rows)}",
        "ideia_completa_no_fim": f"{sum(1 for r in rows if r['palavras_penduradas_no_fim'] <= 2)}"
                                 f"/{len(rows)}",
        "iou_maximo": round(max(overlaps), 3) if overlaps else 0.0,
        "iou_medio": round(statistics.mean(overlaps), 3) if overlaps else 0.0,
        "similaridade_lexical_maxima": round(max(sims), 3) if sims else 0.0,
        "score_mediana": round(statistics.median(scores), 1) if scores else None,
        "score_amplitude": round(max(scores) - min(scores), 1) if scores else None,
        "rho_spearman_proxy_engajamento": (round(rho, 2) if rho is not None else None),
        "linhas": rows,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="small")
    ap.add_argument("--lang", default="", help="pt/en; vazio = autodetect")
    ap.add_argument("--limit", type=int, default=12)
    ap.add_argument("--min-seconds", type=float, default=20.0)
    ap.add_argument("--max-seconds", type=float, default=60.0)
    ap.add_argument("--out", default=str(HERE / "qualidade.json"))
    args = ap.parse_args()

    vids = sorted(HERE.glob("*.mp4"))
    if not vids:
        print("nenhum vídeo em media-test/ — rode a bateria de download antes")
        return 2
    opts = AnalysisOptions(whisper_model=args.model, max_clips=args.limit,
                           target_min=args.min_seconds, target_max=args.max_seconds,
                           min_score=0.0)
    reports = []
    for v in vids:
        print(f"=== {v.name} ({v.stat().st_size / 1e6:.0f} MB) ===", flush=True)
        try:
            rep = evaluate(v, args.model, opts, args.lang or None)
        except Exception as exc:                      # noqa: BLE001
            print(f"   FALHOU {type(exc).__name__}: {str(exc)[:200]}", flush=True)
            rep = {"arquivo": v.name, "erro": f"{type(exc).__name__}: {str(exc)[:200]}"}
        reports.append(rep)
        for k, val in rep.items():
            if k not in ("linhas", "scores", "duracoes"):
                print(f"   {k:32s} {val}", flush=True)
        for r in rep.get("linhas", []):
            print(f"   ├─ {r['start']:6.1f}-{r['end']:6.1f} {r['dur']:5.1f}s "
                  f"score={r['score']:5.1f} gap={r['gap_start_s']:4.2f} "
                  f"pend={r['palavras_penduradas_no_fim']:2d} "
                  f"1ªsent={r['palavras_ate_fim_da_1a_sentenca']:3d} "
                  f"gancho={'S' if r['abre_com_gancho'] else '-'} "
                  f"{r['titulo'][:52]}", flush=True)
    Path(args.out).write_text(json.dumps(reports, ensure_ascii=False, indent=1),
                              encoding="utf-8")
    print(f"\nJSON completo em {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
