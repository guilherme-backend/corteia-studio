"""Sonda descartável: confirma o que a UI vai chamar de fato (timeline, crop, ASS, export)."""
from __future__ import annotations

import json
import time
import traceback
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from corteia.context import AppContext
from corteia.core import render
from corteia.core import subtitles as SUB
from corteia.models import Clip, SubtitleSettings, SubtitleStyle

PROJ = "/home/ubuntu/.config/corteia-studio/projects/de9e5e637f5a.json"
SRC = "/tmp/asr_test.mp4"


def main() -> int:
    ctx = AppContext()
    p = ctx.load_project(Path(PROJ))
    print("projeto", p.name, "clips", len(p.clips))
    clip = ctx.project.clips[0]
    print("clip", clip.id, clip.start, clip.end, "aspect", clip.edit.aspect, "subs", clip.edit.subtitles)

    # --- timeline
    tmap = ctx.pipeline.timeline_for(clip)
    print("tmap ranges", tmap.ranges[:6], "duration", round(tmap.duration, 3), "removed", tmap.removed)
    print("tmap has removals attr?", hasattr(tmap, "removals"))

    # --- crop plan
    t0 = time.time()
    plan = ctx.pipeline.crop_plan(clip, clip.edit.aspect)
    print("crop_plan em %.1fs mode=%s pts=%s note=%r" % (time.time() - t0, plan.mode, len(plan.points), plan.note))
    print("primeiros pontos", plan.points[:3])

    # --- ASS
    settings = SUB.preset(SubtitleStyle.VIRAL)
    w, h = render.output_size(ctx.project.export_settings, clip.edit.aspect)
    dest = Path("/tmp/probe.ass")
    SUB.build_ass(ctx.transcript_for(SRC), clip.start, clip.end, settings, dest,
                  clip_w=w, clip_h=h, edits=clip.edit.manual_text_edits, tmap=tmap)
    print("ass size", dest.stat().st_size, "clip size", w, h)
    cs = SUB.cues(ctx.transcript_for(SRC), clip.start, clip.end, settings,
                 clip.edit.manual_text_edits, tmap)
    print("cues", len(cs), [c.text()[:28] for c in cs[:4]])

    # --- estimate
    from corteia.core.exporter import estimate
    print("estimate", estimate(ctx))

    # --- export one (real)
    clip.status = clip.status.__class__.APPROVED
    ctx.project.export_settings.out_dir = "/tmp/ui_smoke_out"
    t0 = time.time()
    try:
        out = ctx.exporter.export_one(clip.id)
        print("export ok em %.1fs ->" % (time.time() - t0), out, Path(out).stat().st_size)
    except Exception:
        traceback.print_exc()

    # --- preview_render fallback
    try:
        render.preview_render(clip, Path("/tmp/probe_preview.mp4"), seconds=3.0)
        print("preview_render ok")
    except NameError as exc:
        print("preview_render NameError:", exc)
        from corteia.models import ExportSettings, Resolution
        sub = Clip(**{k: v for k, v in clip.to_dict().items() if k != "edit"})
        sub.edit = clip.edit
        sub.end = min(clip.end, clip.start + 3.0)
        from corteia.core.timeline import TimelineMap
        info = __import__("corteia.core.media", fromlist=["x"]).probe(Path(SRC))
        st = ExportSettings(resolution=Resolution.R_720, fps=min(30.0, info.fps or 30.0),
                            aspect=clip.edit.aspect, quality_crf=32, preset="ultrafast")
        render.render_clip(sub, source=Path(SRC), info=info, transcript=None, settings=st,
                           plan=None, tmap=TimelineMap.full(clip.start, sub.end),
                           out=Path("/tmp/probe_preview2.mp4"), proxy=True)
        print("render_clip proxy ok", Path("/tmp/probe_preview2.mp4").stat().st_size)
    print("stats", json.dumps(ctx.project.stats()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
