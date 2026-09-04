import asyncio
import json
import os
import random
import re
import subprocess
import uuid
from pathlib import Path
from typing import List, Optional

import httpx
import imageio_ffmpeg
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from PIL import Image, ImageDraw, ImageFont
from pydantic import BaseModel, Field

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
ROOT = Path(os.environ.get("JOBS_DIR", "/tmp/short-render-jobs"))
ROOT.mkdir(parents=True, exist_ok=True)
TOKEN = os.environ.get("RENDER_TOKEN", "").strip()

app = FastAPI(title="Vintage Movie Short Renderer", version="1.0.0")


class Clip(BaseModel):
    start: float
    end: float


class TitleCard(BaseModel):
    start: float
    end: float
    line1: str
    line2: str


class RenderRequest(BaseModel):
    presenter_url: str
    trailer_url: str
    duration: Optional[float] = None
    clips: Optional[List[Clip]] = None
    titles: Optional[List[TitleCard]] = None
    trailer_volume: float = Field(default=0.18, ge=0.0, le=1.0)
    voice_volume: float = Field(default=1.0, ge=0.0, le=2.0)
    output_name: str = "final_short.mp4"


def require_auth(authorization: Optional[str]):
    if TOKEN and authorization != f"Bearer {TOKEN}":
        raise HTTPException(status_code=401, detail="Unauthorized")


def run(cmd):
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr[-9000:])
    return p


def probe_duration(path: Path) -> float:
    p = subprocess.run([FFMPEG, "-hide_banner", "-i", str(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", p.stderr)
    if not m:
        raise RuntimeError(f"Could not detect duration for {path.name}")
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


async def download(url: str, dst: Path):
    timeout = httpx.Timeout(180.0, connect=30.0)
    async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as client:
        async with client.stream("GET", url) as r:
            r.raise_for_status()
            with dst.open("wb") as f:
                async for chunk in r.aiter_bytes(1024 * 1024):
                    f.write(chunk)


def font(size: int):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]
    for p in candidates:
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def fit_font(draw: ImageDraw.ImageDraw, text: str, max_w: int, max_size: int, min_size: int):
    for size in range(max_size, min_size - 1, -2):
        f = font(size)
        b = draw.textbbox((0, 0), text, font=f)
        if b[2] - b[0] <= max_w:
            return f
    return font(min_size)


def make_master_overlay(path: Path):
    W, H = 1080, 1920
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rectangle((0, 0, 118, H), fill=(6, 6, 5, 255))
    d.rectangle((958, 0, W, H), fill=(6, 6, 5, 255))
    d.rectangle((118, 858, 958, 1248), fill=(7, 7, 6, 255))
    cream = (229, 216, 191, 230)
    gold = (143, 92, 28, 210)
    for y in range(52, 1880, 124):
        d.rounded_rectangle((28, y, 84, y + 72), radius=9, fill=cream)
        d.rounded_rectangle((996, y, 1052, y + 72), radius=9, fill=cream)
    for box in [(106, 50, 970, 870), (106, 1236, 970, 1840)]:
        d.rounded_rectangle(box, radius=34, outline=(170, 156, 132, 235), width=8)
        inner = (box[0] + 8, box[1] + 8, box[2] - 8, box[3] - 8)
        d.rounded_rectangle(inner, radius=28, outline=(45, 42, 37, 255), width=5)
    d.line((146, 938, 315, 938), fill=gold, width=2)
    d.line((765, 938, 934, 938), fill=gold, width=2)
    d.line((128, 864, 952, 864), fill=(110, 78, 35, 180), width=2)
    d.line((128, 1242, 952, 1242), fill=(110, 78, 35, 180), width=2)
    rnd = random.Random(20260904)
    for _ in range(850):
        x = rnd.randint(0, W - 1)
        y = rnd.randint(0, H - 1)
        if 118 < x < 958 and (62 < y < 858 or 1248 < y < 1828):
            continue
        a = rnd.randint(10, 48)
        if rnd.random() < 0.7:
            dx = rnd.randint(-5, 5)
            dy = rnd.randint(3, 25)
            d.line((x, y, x + dx, y + dy), fill=(226, 213, 188, a), width=1)
        else:
            r = rnd.randint(1, 3)
            d.ellipse((x-r, y-r, x+r, y+r), fill=(226, 213, 188, a))
    f = font(20)
    for y, txt in [(250, "6"), (720, "3"), (1420, "8")]:
        d.text((52, y), txt, font=f, fill=(196, 161, 98, 180))
        d.text((1010, y + 18), txt, font=f, fill=(196, 161, 98, 180))
    im.save(path)


def make_title(card: TitleCard, path: Path):
    W, H = 1080, 1920
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    gold = (203, 137, 34, 255)
    cream = (236, 222, 196, 255)
    line1 = card.line1.upper().strip()
    line2 = card.line2.upper().strip()
    f1 = fit_font(d, line1, 640, 58, 30)
    f2 = fit_font(d, line2, 760, 102, 50)
    def draw_center(text, y, f, fill):
        b = d.textbbox((0, 0), text, font=f)
        tw, th = b[2] - b[0], b[3] - b[1]
        x = (W - tw) // 2
        d.text((x, y - th // 2 - b[1]), text, font=f, fill=fill)
        return x, tw
    x1, w1 = draw_center(line1, 950, f1, gold)
    draw_center(line2, 1072, f2, cream)
    d.line((146, 952, max(146, x1 - 24), 952), fill=gold, width=2)
    d.line((min(934, x1 + w1 + 24), 952, 934, 952), fill=gold, width=2)
    im.save(path)


def auto_clips(trailer_duration: float, target_duration: float):
    chunk = 3.2
    n = max(1, int(target_duration // chunk))
    usable_start = min(8.0, max(0.0, trailer_duration * 0.05))
    usable_end = max(usable_start + chunk, trailer_duration - 8.0)
    span = max(0.0, usable_end - usable_start - chunk)
    out = []
    remaining = target_duration
    for i in range(n):
        if remaining <= 0:
            break
        s = usable_start + span * (i / max(1, n - 1))
        dur = min(chunk, remaining)
        out.append((s, min(trailer_duration, s + dur)))
        remaining -= dur
    if remaining > 0:
        s = max(usable_start, usable_end - remaining)
        out.append((s, min(trailer_duration, s + remaining)))
    return out


def default_titles(duration: float):
    pairs = [("НОВОЕ КИНО", "ЧТО НУЖНО ЗНАТЬ"), ("ГЛАВНЫЙ ФАКТ", "ЗА 30 СЕКУНД"), ("ПОЧЕМУ ЭТО", "ИНТЕРЕСНО"), ("СТОИТ ВКЛЮЧАТЬ?", "РЕШАТЬ ТЕБЕ")]
    step = duration / len(pairs)
    out = []
    for i, (a, b) in enumerate(pairs):
        out.append(TitleCard(start=i * step, end=(i + 1) * step, line1=a, line2=b))
    out[-1].end = duration
    return out


def normalize_clips(clips, target, trailer_duration):
    fixed = []
    used = 0.0
    for s, e in clips:
        if used >= target:
            break
        s = max(0.0, min(float(s), max(0.0, trailer_duration - 0.1)))
        d = min(max(0.10, float(e) - float(s)), target - used, max(0.10, trailer_duration - s))
        fixed.append((s, s + d))
        used += d
    if used < target:
        d = target - used
        s = max(0.0, min(trailer_duration - d - 0.1, trailer_duration * 0.70))
        fixed.append((s, min(trailer_duration, s + d)))
    return fixed


def render_job(job_id: str, req: RenderRequest):
    job = ROOT / job_id
    status_path = job / "status.json"
    try:
        status_path.write_text(json.dumps({"status": "downloading"}), encoding="utf-8")
        presenter = job / "presenter.mp4"
        trailer = job / "trailer.mp4"
        asyncio.run(download(req.presenter_url, presenter))
        asyncio.run(download(req.trailer_url, trailer))
        pdur = probe_duration(presenter)
        tdur = probe_duration(trailer)
        target = max(1.0, min(req.duration or pdur, pdur))
        clips = [(c.start, c.end) for c in req.clips] if req.clips else auto_clips(tdur, target)
        clips = normalize_clips(clips, target, tdur)
        titles = req.titles or default_titles(target)
        master = job / "master.png"
        make_master_overlay(master)
        title_paths = []
        for i, card in enumerate(titles):
            p = job / f"title_{i}.png"
            make_title(card, p)
            title_paths.append(p)
        status_path.write_text(json.dumps({"status": "rendering"}), encoding="utf-8")
        out = job / req.output_name
        cmd = [FFMPEG, "-y", "-i", str(trailer), "-i", str(presenter), "-loop", "1", "-i", str(master)]
        for p in title_paths:
            cmd += ["-loop", "1", "-i", str(p)]
        n = len(clips)
        fc = []
        fc.append(f"[0:v]split={n}" + "".join(f"[tv{i}]" for i in range(n)))
        fc.append(f"[0:a]asplit={n}" + "".join(f"[ta{i}]" for i in range(n)))
        concat_parts = []
        for i, (s, e) in enumerate(clips):
            fc.append(f"[tv{i}]trim=start={s:.3f}:end={e:.3f},setpts=PTS-STARTPTS,scale=-2:796,crop=840:796,fps=25,eq=contrast=1.06:brightness=-0.018:saturation=0.82,vignette=PI/5[v{i}]")
            fc.append(f"[ta{i}]atrim=start={s:.3f}:end={e:.3f},asetpts=PTS-STARTPTS[a{i}]")
            concat_parts += [f"[v{i}]", f"[a{i}]"]
        fc.append("".join(concat_parts) + f"concat=n={n}:v=1:a=1[trv][tra]")
        fc.append(f"[1:v]trim=duration={target:.3f},setpts=PTS-STARTPTS,scale=-2:580,crop=840:580,fps=25,eq=contrast=1.045:brightness=-0.012:saturation=0.84,vignette=PI/6[pv]")
        fc.append(f"color=c=black:s=1080x1920:d={target:.3f}[bg]")
        fc.append("[bg][trv]overlay=x=118:y=62:shortest=1[s1]")
        fc.append("[s1][pv]overlay=x=118:y=1248:shortest=1[s2]")
        fc.append("[2:v]format=rgba[master]")
        fc.append("[s2][master]overlay=0:0:shortest=1[s3]")
        prev = "s3"
        for i, card in enumerate(titles):
            inp = 3 + i
            out_name = f"s{4 + i}"
            fc.append(f"[{inp}:v]format=rgba[t{i}]")
            fc.append(f"[{prev}][t{i}]overlay=0:0:enable='between(t,{card.start:.3f},{card.end:.3f})'[{out_name}]")
            prev = out_name
        fc.append(f"[{prev}]format=yuv420p[vout]")
        fc.append(f"[1:a]atrim=duration={target:.3f},asetpts=PTS-STARTPTS,highpass=f=70,acompressor=threshold=0.08:ratio=2.5:attack=8:release=120,volume={req.voice_volume:.3f}[voice]")
        fc.append(f"[tra]volume={req.trailer_volume:.3f},lowpass=f=12000,afade=t=in:st=0:d=0.18,afade=t=out:st={max(0.0, target-0.35):.3f}:d=0.35[bed]")
        fc.append("[voice][bed]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95[aout]")
        cmd += ["-filter_complex", ";".join(fc), "-map", "[vout]", "-map", "[aout]", "-t", f"{target:.3f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(out)]
        run(cmd)
        status_path.write_text(json.dumps({"status": "completed", "duration": target, "download_url": f"/download/{job_id}"}), encoding="utf-8")
    except Exception as e:
        status_path.write_text(json.dumps({"status": "failed", "error": str(e)}), encoding="utf-8")


@app.get("/")
def root():
    return {"ok": True, "service": "vintage-movie-short-renderer"}


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/render")
async def render(req: RenderRequest, bg: BackgroundTasks, authorization: Optional[str] = Header(default=None)):
    require_auth(authorization)
    job_id = uuid.uuid4().hex
    job = ROOT / job_id
    job.mkdir(parents=True, exist_ok=True)
    (job / "request.json").write_text(req.model_dump_json(indent=2), encoding="utf-8")
    (job / "status.json").write_text(json.dumps({"status": "queued"}), encoding="utf-8")
    bg.add_task(render_job, job_id, req)
    return {"job_id": job_id, "status_url": f"/status/{job_id}", "download_url": f"/download/{job_id}"}


@app.get("/status/{job_id}")
def status(job_id: str, authorization: Optional[str] = Header(default=None)):
    require_auth(authorization)
    p = ROOT / job_id / "status.json"
    if not p.exists():
        raise HTTPException(404, "Job not found")
    return json.loads(p.read_text(encoding="utf-8"))


@app.get("/download/{job_id}")
def download_result(job_id: str, authorization: Optional[str] = Header(default=None)):
    require_auth(authorization)
    p = ROOT / job_id / "final_short.mp4"
    if not p.exists():
        raise HTTPException(409, "Video is not ready")
    return FileResponse(p, media_type="video/mp4", filename="final_short.mp4")
