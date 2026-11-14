import os, sys, json, time
from pathlib import Path
sys.path.insert(0, ".")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from corteia.core import media, timeline, subtitles as SUB, llm, analyze as A, models as _m
from corteia.models import (Silence, Word, Segment, Transcript, SubtitleSettings, SubtitleStyle,
                            ExportJob, ExportSettings, JobState, Project, Clip, ClipEdit,
                            AnalysisOptions, Scene, VideoInfo, Resolution, AspectRatio,
                            SubtitleSettings as SS, ScoreBreakdown)
from corteia.project import Recent
from corteia.core.timeline import TimelineMap

T = Path("/tmp/recon"); T.mkdir(exist_ok=True)
# probe inexistente
try:
    media.probe(T/"nao_existe.mp4")
except Exception as e:
    print("A) probe() inexistente ->", type(e).__name__, repr(getattr(e,'message','')), repr(str(e))[:120])
try:
    media.thumbnail(T/"nao.mp4", 1.0)
except Exception as e: print("B) thumbnail inexistente ->", type(e).__name__, repr(e.message))

# timeline
sil = [Silence(4.0, 7.0)]
tm = TimelineMap.from_silences(sil, 0.0, 12.0, min_keep=0.5)
print("C) ranges:", tm.ranges, "dur=%.3f removed=%.3f cuts=%d" % (tm.duration, tm.removed, tm.cuts))
print("   to_out(3.9)=", tm.src_to_clip(3.9), "to_out(5.5)=", tm.src_to_clip(5.5), "to_out(8)=", tm.src_to_clip(8.0))
print("   clip_to_src(3.95)=", tm.clip_to_src(3.95), "window(3.9,8.1)=", tm.window(3.9,8.1))
print("   identity:", tm.is_identity(0,12), TimelineMap.full(0,12))
tm2 = TimelineMap.from_silences([], 2.0, 5.0); print("   sem silencia:", tm2.ranges)
tm3 = TimelineMap.from_silences([Silence(0.0,1.0),Silence(1.5,10.0)], 0.0, 10.0, min_keep=0.5)
print("   tudo mudo:", tm3.ranges, tm3.duration)

# subtitles
def mk_tr():
    segs=[]; t=0.3
    lines=["Eu larguei um salário de doze mil reais pra trabalhar de casa.",
           "A verdade é que estabilidade é uma ilusão, você é o primeiro corte!",
           "Então anota a lição: vende o que você já faz todo dia de graça."]
    for i,l in enumerate(lines):
        ws=[];acc=t
        for w in l.split():
            d=3.4/len(l.split()); ws.append(Word(round(acc,3),round(acc+d,3),w)); acc+=d
        segs.append(Segment(round(t,3),round(acc,3),l,ws, i%2)); t=acc+0.35
    return Transcript("pt","tiny",segs)
tr = mk_tr()
st = SUB.preset(SubtitleStyle.PODCAST)
cues = SUB.cues(tr, 0.0, 12.0, st, None, tm)
print("D) cues:", len(cues), [(round(c.start,2),round(c.end,2),c.text()[:22]) for c in cues][:4])
ass = SUB.ass_document(cues, st, 1080, 1920)
style_line = [l for l in ass.splitlines() if l.startswith("Style:")][0]
fmt = [l for l in ass.splitlines() if l.startswith("Format: Name")][0]
print("   style fields:", len(style_line.split(":",1)[1].split(",")), "format fields:", len(fmt.split(":",1)[1].split(",")))
dlg = [l for l in ass.splitlines() if l.startswith("Dialogue:")][0]
print("   dialogue:", dlg[:160])
import re
print("   tempos:", re.findall(r"\d:\d\d:\d\d\.\d+", dlg)[:2])
print("   [V4+ Styles]:", "[V4+ Styles]" in ass, " \\k:", "\\kf" in ass, "MarginV:", style_line.split(",")[-2])
print("   cues dentro de silencio viram?", [ (w.text, tm.src_to_clip(w.start)) for w in tr.segments[1].words[:3]])
# style viral (sem \k)
st2 = SUB.preset(SubtitleStyle.VIRAL)
ass2 = SUB.ass_document(SUB.cues(tr,0,12,st2,None,tm), st2, 1080, 1920)
print("   viral tem \\k?", "\\k" in ass2, "tem \\c?", "\\c" in ass2)
p = T/"s.ass"; SUB.build_ass(tr, 0.0, 12.0, st, p, clip_w=1080, clip_h=1920, tmap=tm)
print("   build_ass:", p.exists(), len(p.read_text()))
print("   srt:", SUB.to_sots if False else SUB.to_srt(cues)[:60].replace("\n","|"))
print("   vtt:", SUB.to_vtt(cues)[:40].replace("\n","|"))
print("   sync:", SUB.sync_report(cues, tm))

# llm
from corteia.config import Config
cfgp = T/"cfg.json"
cfg = Config(cfgp); cfg.update({"api.provider":"off","api.key":"","api.model":"gpt-4o-mini"}, save=False)
c = llm.LLMClient(cfg)
print("E) status off:", c.status().as_dict(), "usable():", c.usable())
cfg.update({"api.provider":"openai","api.key":""}, save=False)
print("   openai sem chave:", c.status().as_dict())
cfg.update({"api.provider":"openai","api.key":"sk-x"}, save=False)
print("   com chave:", c.status().as_dict())
items = [("id%d"%i, "x"*1500, 30.0) for i in range(12)]
b = llm._batch(items, 9000)
print("F) batches:", [len(x) for x in b], "custos:", [sum(min(len(t[1]),1400)+40 for t in chunk) for chunk in b])
print("   todos ids:", sorted(i for ch in b for i in [x[0] for x in ch]) == sorted(x[0] for x in items))
rows = llm._parse_rows(json.dumps({"results":[{"id":"a","score":85,"title":"T","why":"w"},{"id":"zzz","score":1,"title":"x","why":"y"}]}), {"a","b"})
print("G) parse_rows:", rows)
out={}; llm._ingest(out, rows[0]); print("   ingest 85 ->", out)
o2={}; llm._ingest(o2, {"id":"b","score":8}); print("   ingest 8 ->", o2)
o3={}; llm._ingest(o3, {"id":"b","score":0.75}); print("   ingest .75 ->", o3)
try: llm._parse_rows('{"results":[{"id":"nope","score":1}]}', {"a"})
except Exception as e: print("   sem ids ->", type(e).__name__, e)
print("H) fence:", repr(llm._strip_fence("```json\n{\"a\":1}\n```")))
print("I) http:", llm._http_meaning(401,"",c.status())[:60], "|", llm._http_meaning(404,"groq",c.status())[:60], "|", llm._http_meaning(429,"",c.status())[:50], "|", llm._http_meaning(503,"",c.status()))

# models round-trip
r = Recent(id="1",name="n",path="/tmp/x.json",updated_at=1.0,sources=1,clips=2,exported=0,duration=3.0,thumb="")
print("J) Recent.to_dict:", r.to_dict())
proj = Project(name="P", created_at=1.0, updated_at=2.0)
proj.sources.append(_mk if False else __import__("corteia.models",fromlist=["SourceVideo"]).SourceVideo(
    path="/tmp/a.mp4", info=media.probe(T/"speech.mp4"), transcript=tr, scenes=[Scene(0,3)],
    silences=[Silence(4,7)], faces=[], thumb="/tmp/t.jpg", stages_done=["probe"], origin="file"))
cl = Clip(source_path="/tmp/a.mp4", start=1.0, end=20.0, title="T", score=ScoreBreakdown(total=71.5, signals={"hook":0.5}, weights={"hook":0.15}, llm_used=True, llm_reason="x"))
cl.edit = ClipEdit(aspect=AspectRatio.VERTICAL_9_16, subtitles=SubtitleSettings(style=SubtitleStyle.VIRAL), manual_text_edits={0:"abc"}, remove_silences=False)
proj.clips.append(cl)
proj.export_settings = ExportSettings(resolution=Resolution.R_720, aspect=AspectRatio.SQUARE_1_1, filename_pattern="{index:02d}-{title}")
proj.exports.append(ExportJob(clip_id=cl.id, source_path="/tmp/a.mp4", out_path="/tmp/o.mp4", state=JobState.DONE, settings=proj.export_settings))
back = Project.from_dict(json.loads(json.dumps(proj.to_dict())))
same = back.to_dict() == proj.to_dict()
print("K) round-trip:", same, "| clips:", len(back.clips), back.clips[0].score.total, back.clips[0].edit.manual_text_edits, back.clips[0].edit.remove_silences)
print("   exports:", back.exports[0].state, back.exports[0].settings.resolution)
print("   sources:", back.sources[0].info.width, len(back.sources[0].transcript.segments), back.sources[0].stages_done)
if not same:
    a_,b_=proj.to_dict(),back.to_dict()
    for k in a_:
        if a_[k]!=b_[k]: print("   DIFF",k, str(a_[k])[:100], "||", str(b_[k])[:100])
