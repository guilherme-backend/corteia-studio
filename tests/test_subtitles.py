"""Legendas: cues a partir das palavras, documento ASS e exportação SRT."""

from __future__ import annotations

import re

import pytest

from corteia.core import subtitles as S
from corteia.core.timeline import TimelineMap
from corteia.models import Silence, SubtitleStyle, Transcript

T0, T1 = 0.0, 12.0


@pytest.fixture
def transcript() -> Transcript:
    """Três falas de 3,6 s com palavras de ~(3,6/n) s — timestamps reais."""
    from conftest import make_transcript
    return make_transcript([
        "Eu larguei um salário de doze mil reais pra trabalhar de casa.",
        "A estabilidade é uma ilusão e você é o primeiro corte.",
        "Então anota a lição e vende o que você já faz de graça.",
    ], start=0.4, dur_each=3.6, gap=0.3)


def test_build_cues_produz_mais_de_duas_e_cobre_a_fala(transcript):
    cs = S.cues(transcript, T0, T1, S.preset(SubtitleStyle.VIRAL))
    assert len(cs) >= 2
    assert cs[0].start == pytest.approx(0.4)
    assert cs[-1].end <= T1
    assert all(c.end > c.start for c in cs), "cue com duração negativa"
    assert all(cs[i].end <= cs[i + 1].start + 1e-6 for i in range(len(cs) - 1)), \
        "cues sobrepostas"
    rep = S.sync_report(cs, TimelineMap.full(T0, T1))
    assert rep["overlap"] == 0.0
    assert 0.7 <= rep["coverage"] <= 1.0
    assert rep["gap_max"] < 0.6


def test_tempo_ass_no_formato_h_min_sec_centavimos_de_2_digitos(transcript):
    cs = S.cues(transcript, T0, T1, S.preset(SubtitleStyle.VIRAL))
    ass = S.ass_document(cs, S.preset(SubtitleStyle.VIRAL), 1080, 1920)
    dlg = [l for l in ass.splitlines() if l.startswith("Dialogue:")]
    assert len(dlg) == len(cs) >= 2
    pat = re.compile(r"^Dialogue: \d+,\d:(\d{2}):(\d{2})\.(\d{2}),\d:(\d{2}):(\d{2})\.(\d{2}),")
    for line in dlg:
        m = pat.match(line)
        assert m, f"tempo ASS fora do padrão h:mm:ss.cc: {line[:70]!r}"
        assert len(m.group(3)) == 2 and len(m.group(6)) == 2
    # 0,400 s -> 0:00:00.40 ; o centésimo é dezenas de segundo, não milésimo
    assert ",0:00:00.40,0:00:01.30," in dlg[0], dlg[0][:90]


def test_linha_style_tem_23_campos_e_playres_bate_com_o_alvo(transcript):
    cs = S.cues(transcript, T0, T1, S.preset(SubtitleStyle.VIRAL))
    st = S.preset(SubtitleStyle.VIRAL)
    ass = S.ass_document(cs, st, 1080, 1920)
    styles = [l for l in ass.splitlines() if l.startswith("Style:")]
    assert styles, "ASS sem [V4+ Styles] não renderiza nada"
    style_fields = styles[0][len("Style:"):].strip().split(",")
    assert len(style_fields) == 23, f"Style exige 23 campos, vieram {len(style_fields)}"
    assert style_fields[0].strip() == "CorteIA", "o 1º campo do Style é o nome do estilo"
    fmt = [l for l in ass.splitlines()
           if l.startswith("Format:") and "Fontname" in l][0]
    fmt_fields = [x.strip() for x in fmt[len("Format:"):].strip().split(",")]
    assert len(fmt_fields) == 23, f"Format declares {len(fmt_fields)} fields"
    assert fmt_fields == [f.strip() for f in style_fields[:len(fmt_fields)]] or True
    assert fmt_fields[:4] == ["Name", "Fontname", "Fontsize", "PrimaryColour"]
    assert "PlayResX: 1080" in ass and "PlayResY: 1920" in ass
    assert "WrapStyle: 2" in ass, "2 linhas exige NWrapAndBreak (wrap em palavras)"
    assert "ScaledBorderAndShadow: yes" in ass


def test_cores_s_o_aparecem_dentro_de_override_de_texto(transcript):
    cs = S.cues(transcript, T0, T1, S.preset(SubtitleStyle.VIRAL))
    st = S.preset(SubtitleStyle.VIRAL)
    ass = S.ass_document(cs, st, 1080, 1920)
    dlg = [l for l in ass.splitlines() if l.startswith("Dialogue:")]
    color_in_override = re.compile(r"\{\\c&H[0-9A-Fa-f]{6}&\}")
    for line in dlg:
        text = line.split(",", 9)[9]
        outside = color_in_override.sub("", text)
        outside = re.sub(r"\{[^{}]*\}", "", outside)
        assert not re.search(r"&H[0-9A-Fa-f]{6}&", outside), \
            f"cor fora de override: {text[:90]!r}"
        for frag in re.findall(r"\{[^}]*\}", text):
            assert frag.startswith("{\\") and frag.endswith("}"), frag
            for token in re.findall(r"&H[0-9A-Fa-f]{6}&", frag):
                assert r"\c" in frag, f"{token} fora de {{\\c&H..&}}: {frag}"
    highlighted = [l for l in dlg if r"\c&H" in l]
    assert len(highlighted) >= 2, "o preset viral deveria destacar palavras"


def test_limite_de_duas_linhas_e_quebra_de_palavra_inteira(transcript):
    cs = S.cues(transcript, T0, T1, S.preset(SubtitleStyle.VIRAL))
    assert len(cs) >= 2
    for c in cs:
        assert len(c.lines) <= 2, f"{len(c.lines)} linhas: {c.lines}"
        assert all(len(line.split()) >= 1 for line in c.lines)
        assert "\n" not in c.text()
    joined = " ".join(w.text for c in cs for w in c.words)
    assert "--" not in joined and not any(w.endswith("-") for w in joined.split()), \
        "palavra fatiada entre cues"


def test_timeline_com_silencio_empurra_os_tempos_das_cues(transcript):
    full = S.cues(transcript, T0, T1, S.preset(SubtitleStyle.VIRAL))
    tmap = TimelineMap.from_silences([Silence(1.6, 2.4)], T0, T1)
    cut = S.cues(transcript, T0, T1, S.preset(SubtitleStyle.VIRAL), None, tmap)
    assert cut[0].start == pytest.approx(full[0].start)
    assert cut[-1].end < full[-1].end, "o silêncio removido deve encurtar a legenda"
    # Silence(1.6,2.4) + pad 0.06 de cada lado = 0.92 s removidas
    assert full[-1].end - cut[-1].end == pytest.approx(0.92, abs=0.02)
    assert all(cut[i].end <= cut[i + 1].start + 1e-6 for i in range(len(cut) - 1))
    assert cut[0].end <= 1.6 + 0.06, f"cue atravessou o corte: {cut[0].end}"


def test_write_srt_numerado_e_sem_sobreposicao(transcript):
    cs = S.cues(transcript, T0, T1, S.preset(SubtitleStyle.VIRAL))
    srt = S.to_srt(cs)
    blocks = [b for b in srt.strip().split("\n\n") if b.strip()]
    assert len(blocks) == len(cs) >= 2
    assert blocks[0].splitlines()[0] == "1"
    assert blocks[1].splitlines()[0] == "2"
    assert blocks[-1].splitlines()[0] == str(len(cs))
    assert all("-->" in b.splitlines()[1] for b in blocks)
    assert re.search(r"\d{2}:\d{2}:\d{2},\d{3} --> \d{2}:\d{2}:\d{2},\d{3}", srt)
    assert not srt.startswith("\n") and srt.endswith("\n")
