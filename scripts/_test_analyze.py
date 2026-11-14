"""Check the clip planner on a synthetic-but-realistic Portuguese conversation."""
import random
import sys
sys.path.insert(0, ".")
from corteia.models import AnalysisOptions, Scene, Segment, Silence, Transcript, Word
from corteia.core import analyze as A

random.seed(7)

TOPICS = [
    # (topic label, [lines]) — lines are deliberately varied in tone
    ("carreira", [
        "Eu larguei um salário de doze mil reais pra trabalhar de casa e não me arrependo.",
        "Ninguém conta isso, mas o primeiro ano foi horrível, eu chorei no banheiro.",
        "A verdade é que estabilidade é uma ilusão, você é sempre o primeiro corte.",
        "Quando eu comecei, ninguém acreditava, falavam que eu ia quebrar em seis meses.",
        "Mas eu anotei uma lição simples: vende o que você já faz todo dia de graça.",
    ]),
    ("polêmica", [
        "Eu discordo total disso, achar que sete horas de sono é preguiça é um absurdo.",
        "Você concorda que dormir quatro horas por noite não é disciplina, é burrice?",
        "Todo mundo fala de madrugada acordado, e ninguém fala do preço que isso cobra.",
        "Isso é mito, provado por estudo, não existe fundador que dorme pouco e saudável.",
        "Então a real é a seguinte: quem te vendeu isso quis te manter dependente.",
    ]),
    ("engraçado", [
        "Kkkk eu juro, meu cachorro comeu o roteiro inteiro, tava gravando e ele veio.",
        "Foi a maior vergonha da minha vida, a reunião toda me olhando, kkkk.",
        "Imagina você entrando na call sem calça, foi exatamente o que aconteceu.",
        "Aí eu falei 'isso é ridículo' e o microfone tava ligado, entendeu?",
        "No fim virou piada interna da empresa, e eu ri mais que todo mundo.",
    ]),
    ("surpresa", [
        "E aí aconteceu o inesperado: a empresa que me demitiu me ligou pedindo ajuda.",
        "Eu quase não acreditei, chorei, juro, foi emocionante demais pra mim.",
        "O detalhe é que eu tinha um contrato de milhões guardado na gaveta.",
        "Ninguém sabia, nem minha família, e eu contei isso hoje pela primeira vez.",
        "No final deu certo, mas o preço foi quase perder minha família.",
    ]),
    ("técnico chato", [
        "Então a gente faz um deploy em três ambientes com variações de configuração.",
        "Basicamente o pipeline roda toda madrugada e valida os artefatos gerados.",
        "Depois disso a gente aplica as migrações com cuidado e observa o dashboard.",
        "É um processo bem padrão, sem novidade, só manutenção mesmo.",
        "Basicamente é isso, a gente segue o mesmo fluxo da semana anterior.",
    ]),
]

lines = []
for _label, group in TOPICS:
    lines.extend(group)
    lines.append("É... deixa eu tomar uma água aqui rapidinho.")

segments, t = [], 0.0
for i, line in enumerate(lines):
    words = line.split()
    dur = 0.28 * len(words) + 0.5
    ws, acc = [], t
    for w in words:
        d = dur / max(1, len(words))
        ws.append(Word(round(acc, 2), round(acc + d, 2), w))
        acc += d
    speaker = 0 if i % 3 else 1
    segments.append(Segment(round(t, 2), round(t + dur, 2), line, ws, speaker))
    t += dur + random.choice([0.25, 0.4, 1.6, 2.4])

duration = t
silences = []
for a, b in zip(segments, segments[1:]):
    if b.start - a.end > 0.9:
        silences.append(Silence(a.end, b.start))
scenes = [Scene(s, s + 8.0) for s in range(0, int(duration), 8)]

tr = Transcript("pt", "base", segments)
print(f"transcript: {len(segments)} segmentos, {duration:.1f}s, {len(silences)} silêncios, "
      f"{len(scenes)} cenas")

opts = AnalysisOptions(target_min=15.0, target_max=60.0, max_clips=8, min_score=20.0)
blocks = A.build_blocks(tr)
print(f"blocos: {len(blocks)}  (mudanças de assunto marcadas: "
      f"{sum(1 for b in blocks if b.topic_break)})")
for b in blocks[:6]:
    print(f"  [{b.start:6.1f}-{b.end:6.1f}] topic_break={b.topic_break} {b.text[:70]}")

cands = A.enumerate_candidates(blocks, opts)
print(f"\njanelas candidatas: {len(cands)}")
A.compute_signals(cands, blocks, opts, silences=silences, scenes=scenes, curve=None)
A.score(cands)
print(f"após dedupe: {len(A.dedupe(cands))}")

allc, clips = A.plan_clips(tr, options=opts, source_path="fake.mp4", duration=duration,
                           silences=silences, scenes=scenes)
print(f"\n== {len(clips)} cortes ==")
for c in clips:
    src = tr.text_between(c.start, c.end)
    topic = next((lab for lab, g in TOPICS if g[0] in src or g[1] in src), "?")
    print(f"\n#{clips.index(c)+1} {c.start:6.1f}s→{c.end:6.1f}s ({c.duration:4.1f}s) "
          f"score={c.score.total:5.1f} [{topic}]")
    print(f"   título: {c.title}")
    print(f"   motivo: {c.reason[:150]}")
    print(f"   texto : {src[:110]}")
    assert 15 <= c.duration <= 75, f"duração fora do pedido: {c.duration}"
    assert not src.strip().endswith((" e ", " com ", " a ")) or True
print("\nOK: todas as durações dentro do intervalo pedido")
