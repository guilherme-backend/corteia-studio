"""Reproducible ASR smoke test for CorteIA Studio (real audio, real numbers).

Builds /tmp/asr_test.mp4 (espeak-ng PT-BR, 8 sentences, two pitches alternating)
and exercises transcribe()/diarize()/cache/apply_text_edits/words_for_range.
Run:  . .venv/bin/activate && python scripts/_smoke_asr.py
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from corteia.core import transcribe as tr            # noqa: E402
from corteia.models import Transcript, format_ts      # noqa: E402

VIDEO = Path("/tmp/asr_test.mp4")
LINES = [
    (30, "Bom dia a todos, sejam muito bem vindos ao nosso canal."),
    (70, "Hoje vamos falar sobre cortes automaticos com inteligencia artificial."),
    (30, "Voce sabia que um video longo pode virar dez cortes prontos para as redes?"),
    (70, "Sim, e o processo inteiro leva menos de cinco minutos."),
    (30, "Primeiro o audio e transcrito palavra por palavra."),
    (70, "Depois o programa detecta os silencios e escolhe os melhores momentos."),
    (30, "Isso significa mais alcance sem nenhum trabalho manual."),
    (70, "Incrivel, eu nao imaginava que fosse tao simples assim."),
]


def sh(cmd: list[str]) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"command failed: {' '.join(cmd)}\n{r.stderr[-600:]}")
    return r.stdout + r.stderr


def build_media(force: bool = False) -> float:
    if VIDEO.exists() and not force:
        return duration(VIDEO)
    tmp = Path("/tmp/asr_media")
    tmp.mkdir(exist_ok=True)
    for i, (pitch, text) in enumerate(LINES):
        sh(["espeak-ng", "-v", "pt-br", "-s", "160", "-p", str(pitch), text,
            "-w", str(tmp / f"raw{i}.wav")])
        sh(["ffmpeg", "-y", "-v", "error", "-i", str(tmp / f"raw{i}.wav"),
            "-af", "apad=pad_dur=0.38", "-ar", "44100", "-ac", "2",
            str(tmp / f"pad{i}.wav")])
    (tmp / "list.txt").write_text(
        "".join(f"file '{tmp}/pad{i}.wav'\n" for i in range(len(LINES))))
    sh(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
        "-i", str(tmp / "list.txt"), "-c", "copy", str(tmp / "all.wav")])
    sh(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
        "-i", "color=c=black:s=640x360:r=25", "-i", str(tmp / "all.wav"),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-c:a", "aac",
        "-b:a", "128k", "-shortest", "-pix_fmt", "yuv420p", str(VIDEO)])
    return duration(VIDEO)


def duration(p: Path) -> float:
    out = sh(["ffprobe", "-v", "error", "-show_entries", "format=duration",
              "-of", "csv=p=0", str(p)])
    return float(out.strip() or 0.0)


def real_speech_end(p: Path, db: float = -40.0, dur: float = 0.0) -> tuple[float, list[str]]:
    """End of real speech energy = last ``silence_start`` (astats cross-check).

    ``silence_end`` marks where speech *resumes*, so using it would compare a
    word end against the wrong instant.
    """
    err = sh(["ffmpeg", "-hide_banner", "-i", str(p), "-af",
              f"silencedetect=noise={db}dB:d=0.15", "-f", "null", "-"])
    starts = [float(x) for x in re.findall(r"silence_start:\s*([-0-9.]+)", err)]
    ends = [float(x) for x in re.findall(r"silence_end:\s*([-0-9.]+)", err)]
    truth = max(starts) if starts else dur
    ast = sh(["ffmpeg", "-hide_banner", "-ss", f"{max(0.0, truth - 1.5):.3f}", "-t", "2.0",
              "-i", str(p), "-af", "astats=metadata=1:reset=1", "-f", "null", "-"])
    rms = [float(x) for x in re.findall(r"rms_level_db=([-0-9.]+)", ast)]
    dbg = [f"silence_start={x:.3f}" for x in starts[-2:]] + [f"silence_end={x:.3f}" for x in ends[-2:]]
    dbg.append(f"astats rms dB por quadro de 1s: {[round(v, 1) for v in rms[:4]]}")
    return truth, dbg


def main() -> int:
    dur = build_media(force="--force-media" in sys.argv)
    print(f"[media] {VIDEO} dur={dur:.3f}s 640x360 preto + 8 falas PT-BR "
          f"espeak-ng pitches {[p for p, _ in LINES]}")
    print(f"[hardware] device/compute = {tr.ctranslate2_device()} threads={tr._cpu_threads()}")
    print(f"[auto] recomendacao p/ {dur:.0f}s neste PC = {tr.recommended_model(dur)}")

    transcripts = {}
    for name in ("tiny", "base"):
        events: list[tuple[float, str]] = []
        t0 = time.monotonic()
        T = tr.transcribe(VIDEO, model=name, language="pt",
                          on_progress=lambda p, m: events.append((round(p, 1), m)),
                          reuse_cache=False)
        wall = time.monotonic() - t0
        transcripts[name] = T
        rtf = wall / dur if dur else 0.0
        print(f"\n=== transcribe model={name}: wall={wall:.2f}s audio={dur:.2f}s "
              f"RTF={rtf:.3f}x segmentos={len(T.segments)} palavras={len(T.words())} "
              f"lang={T.language}")
        print(f"    progresso: {len(events)} eventos | 1o={events[0] if events else None} "
              f"| ultimo={events[-1] if events else None}")
        for i, s in enumerate(T.segments):
            print(f"    [{i}] {format_ts(s.start)}-{format_ts(s.end)} "
                  f"({s.start:.2f}-{s.end:.2f}s) :: {s.text}")
            print(f"        palavras: " + " ".join(
                f"{w.text}@{w.start:.2f}-{w.end:.2f}(p{w.prob:.2f})" for w in s.words[:8])
                + (" ..." if len(s.words) > 8 else ""))
            print(f"        join_words(words)==text: {tr.join_words(s.words) == s.text} "
                  f"({len(s.words)} palavras)")
        json.dump(T.to_dict(), open(f"/tmp/smoke_{name}.json", "w"), ensure_ascii=False)

    B = transcripts["base"]
    truth, dbg = real_speech_end(VIDEO, -40.0, dur)
    wend = B.segments[-1].words[-1].end
    send = B.segments[-1].end
    print(f"\n=== sync: fim real de fala (silencedetect -40dB) = {truth:.3f}s | "
          f"words[-1].end = {wend:.3f}s | delta = {wend - truth:+.3f}s | "
          f"dentro de +/-0,35s: {abs(wend - truth) <= 0.35}")
    print(f"    seg.end = {send:.3f}s | delta = {send - truth:+.3f}s | silencio cauda: {dbg}")

    T = Transcript.from_dict(json.loads(json.dumps(B.to_dict())))
    notes: list[str] = []
    t0 = time.monotonic()
    D = tr.diarize(T, VIDEO, on_note=notes.append)
    seq = [s.speaker for s in D.segments]
    truthy = [0, 1, 0, 1, 0, 1, 0, 1][:len(seq)]
    hits = sum(1 for a, b in zip(seq, truthy) if a == b)
    f0 = [round(tr.voice_features(tr.extract_pcm(VIDEO, s.words[0].start, s.words[-1].end)).f0, 1)
          for s in D.segments if s.words]
    print(f"\n=== diarize ({time.monotonic() - t0:.2f}s): falantes="
          f"{sorted({x for x in seq if x is not None})}")
    print(f"    previsto : {seq}")
    print(f"    verdade  : {truthy}  -> {hits}/{len(truthy)} segmentos corretos")
    print(f"    F0 (Hz)  : {f0}")
    print(f"    tempos   : {tr.speaker_counts(D)}")
    for n in notes:
        print(f"    note: {n}")

    t0 = time.monotonic()
    C = tr.transcribe(VIDEO, model="base", language="pt")
    print(f"\n=== cache: 2a chamada = {time.monotonic() - t0:.4f}s | "
          f"mesmos tempos={[x.start for x in C.segments] == [x.start for x in B.segments]} "
          f"falantes_no_cache={[x.speaker for x in C.segments]} | arq={tr.cache_file(VIDEO, 'base', 'pt')} "
          f"| is_downloaded(tiny,base)={tr.is_downloaded('tiny')},{tr.is_downloaded('base')}")

    E = Transcript.from_dict(json.loads(json.dumps(B.to_dict())))
    n_before = len(E.segments[0].words)
    span_before = (B.segments[0].words[0].start, B.segments[0].words[-1].end)
    E = tr.apply_text_edits(E, {0: "Bom dia pessoal, bem vindos ao canal oficial da CorteIA",
                                3: "Sim, leva menos de cinco minutos"})
    same_bounds = all(E.segments[i].start == B.segments[i].start for i in range(len(B.segments)))
    print(f"\n=== edits: seg0 {n_before} -> {len(E.segments[0].words)} palavras | span "
          f"{span_before} -> ({E.segments[0].words[0].start:.2f},"
          f"{E.segments[0].words[-1].end:.2f}) | limites dos 8 segmentos intactos: {same_bounds}")
    print("    novo texto seg0:", E.segments[0].text)
    W = tr.words_for_range(B, 5.0, 9.0)
    print(f"\n=== words_for_range(5,9): {len(W)} palavras | ordenadas="
          f"{all(W[i].start <= W[i + 1].start for i in range(len(W) - 1))} | clamp="
          f"{all(w.start >= 5.0 and w.end <= 9.0 for w in W)} | {[(w.text, round(w.start, 2)) for w in W[:6]]}")
    print(f"    text_between(5,9): {B.text_between(5.0, 9.0)!r}")
    print(f"\n=== ETA: {tr.estimate_duration_hours('base', 3600)}")
    print(f"          ETA auto: {tr.estimate_duration_hours('auto', 3600)}")
    perf = Path(tr.perf_file())
    print(f"    perf.json: {json.loads(perf.read_text())['models'] if perf.exists() else None}")
    print(f"\n=== unload_model: {tr.unload_model()} modelo(s) liberado(s); loaded={tr.loaded_model()!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
