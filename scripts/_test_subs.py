"""Standalone check of cue building + ASS output (run: python scripts/_test_subs.py)."""
import sys
sys.path.insert(0, ".")
from corteia.models import Silence, SubtitleStyle, Transcript, Segment, Word
from corteia.core import subtitles as S
from corteia.core.timeline import TimelineMap


def mkseg(start, words, sp=None):
    ws, acc = [], start
    for text, dur in words:
        ws.append(Word(round(acc, 3), round(acc + dur, 3), text))
        acc += dur
    return Segment(start, acc, " ".join(w for w, _ in words), ws, sp)


segs = [
    mkseg(0.20, [("Então", .3), ("a", .15), ("verdade", .5), ("é", .2), ("que", .15),
                 ("ninguém", .4), ("conta", .4), ("isso", .35), ("pra", .2), ("você.", .4)], 0),
    mkseg(3.60, [("Porque", .3), ("o", .1), ("medo", .4), ("de", .15), ("perder", .5),
                 ("é", .2), ("maior", .4), ("que", .2), ("a", .1), ("vontade", .5),
                 ("de", .15), ("ganhar.", .45)], 1),
    mkseg(7.20, [("E", .2), ("aí", .3), ("eu", .2), ("fiz", .35), ("o", .1),
                 ("seguinte:", .5), ("anota", .5), ("a", .1), ("lição.", .5)], 0),
]
tr = Transcript("pt", "base", segs)
t0, t1 = 0.0, 10.5
sil = [Silence(3.1, 3.5), Silence(6.6, 7.1)]
tmap = TimelineMap.from_silences(sil, t0, t1)
print("MAP:", tmap)
print("ranges:", [(round(a, 2), round(b, 2)) for a, b in tmap.ranges])

for style in SubtitleStyle:
    if style is SubtitleStyle.NONE:
        continue
    st = S.preset(style)
    cs = S.cues(tr, t0, t1, st, None, tmap)
    rep = S.sync_report(cs, tmap)
    print(f"\n== {style.value}: {len(cs)} cues  coverage={rep['coverage']} "
          f"gap_max={rep['gap_max']} overlap={rep['overlap']} cps={rep['avg_cps']}")
    for c in cs:
        print(f"   [{c.start:5.2f}-{c.end:5.2f}] sp={c.speaker} lines={c.lines} hi={sorted(c.highlight)}")
    with open(f"/tmp/test_{style.value}.ass", "w", encoding="utf-8") as fh:
        fh.write(S.ass_document(cs, st, 1080, 1920))

print("\n--- manual text edit ---")
edits = {1: "Porque o medo de perder é maior que a vontade."}
cs = S.cues(tr, t0, t1, S.preset(SubtitleStyle.VIRAL), edits, tmap)
for c in cs:
    if c.speaker == 1 or "MEDO" in c.text().upper():
        print("  ", [w.text for w in c.words])

print("\n--- ASS viral sample ---")
lines = open("/tmp/test_viral.ass", encoding="utf-8").read().splitlines()
print("\n".join(lines[:21]))
print("...")
print("\n".join(lines[-3:]))
