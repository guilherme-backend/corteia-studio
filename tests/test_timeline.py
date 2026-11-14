"""TimelineMap: a única fonte de verdade entre o tempo da fonte e o do corte."""

from __future__ import annotations

import pytest

from corteia.core.timeline import TimelineMap
from corteia.models import Silence


# ------------------------------------------------------------------ fábricas
def test_sem_silencio_o_mapa_e_identidade():
    m = TimelineMap.from_silences([], 0.0, 14.0)
    assert m.ranges == [(0.0, 14.0)]
    assert m.is_identity(0.0, 14.0) is True
    assert m.cuts == 0
    assert m.duration == pytest.approx(14.0)
    assert m.removed == pytest.approx(0.0)
    for t in (0.0, 5.5, 14.0):
        assert m.src_to_clip(t) == pytest.approx(t)
        assert m.clip_to_src(t) == pytest.approx(t)


def test_from_silences_rm_a_janela_e_deixa_o_pad_de_respiro():
    m = TimelineMap.from_silences([Silence(4.0, 7.0)], 0.0, 14.0)
    # pad padrão 0.06 s: o corte não cola na palavra
    assert m.ranges[0] == pytest.approx((0.0, 3.94))
    assert m.ranges[1] == pytest.approx((7.06, 14.0))
    assert m.duration == pytest.approx(10.88, abs=1e-6)
    assert m.removed == pytest.approx(3.12, abs=1e-6)
    assert m.cuts == 1
    assert m.is_identity(0.0, 14.0) is False


def test_silencio_cobrindo_o_clipe_inteiro_nao_colapsa_para_zero():
    m = TimelineMap.from_silences([Silence(0.0, 14.0)], 0.0, 14.0)
    assert m.duration > 0.0, "um mapa vazio travaria o render em divide-by-zero"
    assert m.ranges == [(0.0, 14.0)], m.ranges


def test_full_recorta_e_inverte_janela_invalida():
    assert TimelineMap.full(2.0, 8.0).ranges == [(2.0, 8.0)]
    assert TimelineMap.full(2.0, 8.0).duration == pytest.approx(6.0)
    assert TimelineMap.full(-5.0, 3.0).ranges == [(0.0, 3.0)], "clamp no 0"
    assert TimelineMap.full(5.0, 2.0).duration == pytest.approx(0.0)


# ------------------------------------------------------------- mapeamento
def test_src_to_clip_devolve_none_dentro_do_silencio_removido():
    m = TimelineMap.from_silences([Silence(4.0, 7.0)], 0.0, 14.0)
    assert m.src_to_clip(3.0) == pytest.approx(3.0)
    assert m.src_to_clip(3.95) is None          # engolido pelo pad
    assert m.src_to_clip(5.0) is None
    assert m.src_to_clip(8.06) == pytest.approx(4.94, abs=1e-9)
    assert m.clip_to_src(0.0) == pytest.approx(0.0)
    assert m.clip_to_src(4.0) == pytest.approx(7.12, abs=1e-9)


def test_ida_e_volta_e_consistente_em_toda_a_timeline():
    m = TimelineMap.from_silences([Silence(4.0, 7.0), Silence(10.0, 11.0)], 0.0, 14.0)
    acc = 0.0
    for a, b in m.ranges:
        # clip_time é ambíguo exatamente na junta entre dois ranges (3.94 pertence ao
        # range anterior), então só conferimos pontos estritamente internos + o início
        for t in (a + 1e-3, a + (b - a) / 2.0, b - 1e-3):
            assert m.clip_to_src(m.src_to_clip(t)) == pytest.approx(t, abs=1e-9)
        acc += b - a
    assert m.duration == pytest.approx(acc)
    assert m.clip_to_src(m.duration) == pytest.approx(m.ranges[-1][1])
    assert m.src_to_clip(5.0) is None and m.src_to_clip(10.5) is None


def test_window_divide_um_enunciado_em_partes_de_clip_time():
    m = TimelineMap.from_silences([Silence(4.0, 7.0)], 0.0, 14.0)
    s0, s1, pieces = m.window(1.0, 8.0)
    assert s0 == pytest.approx(1.0)
    assert s1 == pytest.approx(4.88)
    assert len(pieces) == 2
    assert pieces[0][1] == pytest.approx(pieces[1][0]), "as partes precisam ser contíguas"
    dead = m.window(4.5, 6.5)
    assert dead[0] == -1.0 and dead[2] == [], "fala toda removida -> -1, nada inventado"


# ------------------------------------------------------------------- filtros
def test_min_keep_e_max_cut_limitam_os_segmentos():
    short_island = [(round(a, 3), round(b, 3)) for a, b in
                    TimelineMap.from_silences([Silence(0.0, 4.0), Silence(4.5, 10.0)],
                                              0.0, 10.0, min_keep=0.35).ranges]
    assert short_island == [(4.06, 4.44)]
    dropped = [(round(a, 3), round(b, 3)) for a, b in
               TimelineMap.from_silences([Silence(0.0, 4.0), Silence(4.5, 10.0)],
                                         0.0, 10.0, min_keep=0.5).ranges]
    assert dropped == [(0.0, 10.0)], "ilha curta demais: volta a ser identidade"
    many = TimelineMap.from_silences([Silence(i * 2.0, i * 2.0 + 0.5) for i in range(1, 30)],
                                     0.0, 60.0, max_cut=8)
    assert len(many.ranges) == 8
    assert many.cuts == 7
    assert many.duration > 45.0, "mesmo cortando, a fala preservada tem de dominar"


def test_trim_concat_filter_gera_um_vtrim_por_range():
    m = TimelineMap.from_silences([Silence(4.0, 7.0)], 0.0, 14.0)
    graph = m.trim_concat_filter()
    assert graph.count("vtrim=start=0.000:end=3.940") == 1
    assert graph.count("vtrim=") == len(m.ranges) == 2
    assert graph.count("atrim=") == len(m.ranges)
    assert "concat=n=2" in graph
    assert "setsar=1" in graph
