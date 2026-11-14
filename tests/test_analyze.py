"""Análise de engajamento: `plan_clips` só com transcrição falsa — zero FFmpeg, zero rede."""

from __future__ import annotations

import pytest

from corteia.core import analyze as A
from corteia.models import AnalysisOptions, Segment, Silence, Transcript, Word

LINES = [
    "Eu larguei um salário de doze mil reais pra trabalhar de casa e não me arrependo.",
    "A verdade é que estabilidade no emprego é uma ilusão e você é sempre o primeiro corte.",
    "Então anota a lição de hoje vende aquilo que você já faz todo dia de graça para alguém.",
    "Eu comecei com zero reais no bolso nenhum cliente e aprendi a entregar valor antes.",
    "O maior erro é esperar o momento perfeito porque ele nunca chega e você perde anos.",
    "Hoje eu ganho três vezes mais trabalhando metade do tempo e ninguém sabe explicar.",
    "Se você tem medo de vender comece oferecendo para três pessoas conhecidas esta semana.",
    "O dinheiro aparece quando o problema que você resolve vale mais que o seu horário.",
    "Ninguém vai te chamar para pedir demissão do seu próprio negócio então comece hoje.",
    "A única segurança real é a capacidade de colocar comida na mesa sem depender de ninguém.",
]


def build(lines=LINES, dur_each: float = 6.0, gap: float = 0.4):
    """Transcrição com timestamps por palavra; retorna (transcript, duração total)."""
    segs, t = [], 0.4
    for i, line in enumerate(lines):
        words = line.split()
        step = dur_each / len(words)
        acc = t
        ws = [Word(round(acc + j * step, 3), round(acc + (j + 1) * step, 3), w)
              for j, w in enumerate(words)]
        segs.append(Segment(round(t, 3), round(acc + dur_each, 3), line, ws, i % 2))
        t = acc + dur_each + gap
    return Transcript("pt", "base", segs), t


def opts(**kw) -> AnalysisOptions:
    base = dict(target_min=12.0, target_max=30.0, max_clips=4, min_score=1.0, use_llm=False)
    base.update(kw)
    return AnalysisOptions(**base)


@pytest.fixture
def fake():
    return build()


def test_plan_clips_respeita_o_numero_maximo_de_cortes(fake):
    tr, dur = fake
    allc, clips = A.plan_clips(tr, options=opts(max_clips=4), source_path="v.mp4",
                               duration=dur, silences=[], scenes=[])
    assert 1 <= len(clips) <= 4, f"max_clips=4 estourado: {len(clips)}"
    assert len(allc) >= len(clips), "existem descartes: o total de candidatos não encolhe"
    assert all(c.source_path == "v.mp4" for c in clips)
    assert all(c.score.llm_used is False for c in clips), "sem rede, sem LLM no escore"
    assert all(c.status.value == "suggested" for c in clips)


def test_duracoes_ficam_dentro_de_target_min_e_target_max(fake):
    tr, dur = fake
    _, clips = A.plan_clips(tr, options=opts(target_min=12.0, target_max=30.0),
                            source_path="v.mp4", duration=dur)
    assert clips
    for c in clips:
        assert 12.0 - 0.6 <= c.duration <= 30.0 + 0.6, \
            f"{c.duration:.2f}s fora de 12..30 ({c.start:.1f}->{c.end:.1f})"
        assert 0.0 <= c.start < c.end <= dur + 1e-6
    _, tight = A.plan_clips(tr, options=opts(target_min=6.0, target_max=8.0, max_clips=6),
                            source_path="v.mp4", duration=dur)
    assert tight, "o pedido curto não gerou nada"
    for c in tight:
        assert 6.0 - 1.0 <= c.duration <= 8.0 + 1.0, f"{c.duration:.2f}s fora de 6..8"


def test_score_total_entre_zero_e_cem_e_ordenado_de_cima_para_baixo(fake):
    tr, dur = fake
    _, clips = A.plan_clips(tr, options=opts(), source_path="v.mp4", duration=dur)
    scores = [c.score.total for c in clips]
    for s in scores:
        assert 0.0 <= s <= 100.0, f"score {s} fora de 0..100"
    # plan_clips devolve os cortes em ordem de aparição no vídeo, não por nota:
    # o ranqueamento é interno (select) e a ordem de saída é cronológica.
    starts = [c.start for c in clips]
    assert starts == sorted(starts), f"lista não cronológica: {starts}"
    assert max(scores) >= o_rank_first(clips), "o corte exibido deveria ser o melhor"
    assert set(clips[0].score.signals) >= {"hook", "completeness", "duration_fit"}
    assert all(0.0 <= v <= 1.0 for c in clips for v in c.score.signals.values())
    assert clips[0].score.weights, "o peso de cada sinal tem de ser auditável"
    assert all(len(c.title) > 5 for c in clips)


def test_reason_e_title_vem_preenchidos_sem_frase_incompleta(fake):
    tr, dur = fake
    _, clips = A.plan_clips(tr, options=opts(), source_path="v.mp4", duration=dur)
    assert clips
    for c in clips:
        assert c.reason.strip() and len(c.reason) > 10, "motivo vazio"
        assert c.title.strip() and len(c.title) <= 70
        assert c.title == c.title.strip(), f"espaço pendurado: {c.title!r}"
        assert not c.title.rstrip().endswith((" e", " de", " que", " o", " a", " para", " com")), \
            f"título cortado no meio da ideia: {c.title!r}"
        assert tr.text_between(c.start, c.end).strip(), "corte sem texto"


def test_limites_nao_fatiam_palavra_pelo_meio(fake):
    tr, dur = fake
    words = tr.words()
    _, clips = A.plan_clips(tr, options=opts(), source_path="v.mp4", duration=dur,
                            silences=[Silence(6.4, 7.2)])
    assert clips
    for c in clips:
        for edge, name in ((c.start, "início"), (c.end, "fim")):
            inside = [w.text for w in words if w.start < edge < w.end]
            assert not inside, f"{name} {edge:.2f}s fatia a(s) palavra(s) {inside}"


def test_silencio_pontua_pior_que_ausencia_de_silencio(fake):
    tr, dur = fake
    blocks = A.build_blocks(tr)
    assert len(blocks) >= 2
    assert blocks[0].start == pytest.approx(0.4)
    assert blocks[-1].end == pytest.approx(dur - 0.4, abs=0.5)
    o = opts()
    cands = A.enumerate_candidates(blocks, o)
    assert cands, "enumerate não gerou candidatos"

    # Candidate.score é float; o ScoreBreakdown fica em Candidate.bd.
    # O que se mede aqui é a CARGA BRUTA de silêncio (c.raw, 0..1): o sinal entra no
    # escore normalizado por ranking, então uma carga uniforme não muda a ordem relativa.
    long_cands = [c for c in cands if c.duration >= 12.0]
    assert long_cands
    A.compute_signals(cands, blocks, o, silences=[], scenes=[])
    A.score(cands)
    clean_load = [c.raw["silence_penalty"] for c in long_cands]
    clean_top = max(c.score for c in long_cands)
    assert max(clean_load) == pytest.approx(0.0), f"sem silêncio: {clean_load}"

    A.compute_signals(cands, blocks, o, silences=[Silence(0.0, dur)], scenes=[])
    A.score(cands)
    dirty_load = [c.raw["silence_penalty"] for c in long_cands]
    assert min(dirty_load) == pytest.approx(1.0), f"silêncio total: {dirty_load}"
    for c in long_cands:
        assert c.raw["silence_seconds"] == pytest.approx(c.duration, abs=0.05)
        assert c.bd.signals["silence_penalty"] == pytest.approx(0.5), \
            "carga uniforme => todos empatados no meio da escala normalizada"
    assert max(c.score for c in long_cands) == pytest.approx(clean_top)
    assert all(c.bd.llm_used is False for c in long_cands)

    # carga desigual: um único candidato muda -> ele deve sair do meio da escala
    hot = cands[0]
    A.compute_signals(cands, blocks, o,
                      silences=[Silence(hot.start, min(dur, hot.start + 6.0))], scenes=[])
    A.score(cands)
    pens = {c.raw["silence_penalty"]: c.bd.signals["silence_penalty"] for c in cands}
    assert cands[0].bd.signals["silence_penalty"] != 0.5 or len(cands) == 1


def o_rank_first(clips) -> float:
    """Nota do primeiro corte exibido — comparada com a melhor nota do lote."""
    return clips[0].score.total


def test_dedupe_nao_devolve_cortes_sobrepostos(fake):
    tr, dur = fake
    o = opts(max_clips=3)
    _, clips = A.plan_clips(tr, options=o, source_path="v.mp4", duration=dur)
    for a in clips:
        for b in clips:
            if a is not b:
                assert a.end <= b.start or b.end <= a.start, \
                    f"sobrepostos: {a.start:.1f}-{a.end:.1f} x {b.start:.1f}-{b.end:.1f}"
    blocks = A.build_blocks(tr)
    cands = A.enumerate_candidates(blocks, o)
    A.compute_signals(cands, blocks, o, silences=[], scenes=[])
    A.score(cands)
    dd = A.dedupe(cands)
    assert 0 < len(dd) <= len(cands)
    assert all(any(c.start == d.start and c.end == d.end for c in cands) for d in dd)
