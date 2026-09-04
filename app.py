import asyncio
import json
import os
import random
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import List, Optional

import httpx
import imageio_ffmpeg
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse
from PIL import Image, ImageDraw, ImageFont
from pydantic import BaseModel, Field

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
ROOT = Path(os.environ.get("JOBS_DIR", "/tmp/short-render-jobs"))
ROOT.mkdir(parents=True, exist_ok=True)
TOKEN = os.environ.get("RENDER_TOKEN", "").strip()
RENDER_LOCK = threading.Lock()
VERSION = "1.2.0"

app = FastAPI(title="Vintage Movie Short Renderer", version=VERSION)


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


async def check_remote_url(url: str) -> dict:
    if not url.lower().startswith(("http://", "https://")):
        raise HTTPException(status_code=422, detail="Only http/https media URLs are supported")
    timeout = httpx.Timeout(20.0, connect=10.0)
    headers = {"Range": "bytes=0-2047", "User-Agent": "Mozilla/5.0 RenderPreflight/1.0"}
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as client:
            async with client.stream("GET", url, headers=headers) as r:
                if r.status_code >= 400:
                    raise HTTPException(status_code=422, detail=f"Media URL returned HTTP {r.status_code}")
                first = b""
                async for chunk in r.aiter_bytes(2048):
                    first = chunk
                    break
                ctype = r.headers.get("content-type", "")
                return {"ok": True, "status": r.status_code, "content_type": ctype, "received_bytes": len(first)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Media URL is not reachable: {e}")


async def download(url: str, dst: Path):
    timeout = httpx.Timeout(180.0, connect=30.0)
    headers = {"User-Agent": "Mozilla/5.0 VintageShortRenderer/1.2"}
    async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as client:
        async with client.stream("GET", url, headers=headers) as r:
            r.raise_for_status()
            with dst.open("wb") as f:
                async for chunk in r.aiter_bytes(1024 * 1024):
                    f.write(chunk)
    if not dst.exists() or dst.stat().st_size < 1024:
        raise RuntimeError(f"Downloaded media is empty or too small: {dst.name}")


def font(size: int):
    for p in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]:
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


def make_overlay(titles: List[TitleCard], target: float, path: Path):
    W, H = 1080, 1920
    frames_dir = path.parent / "title_frames"
    frames_dir.mkdir(exist_ok=True)

    base = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(base)
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
    d.line((128, 864, 952, 864), fill=(110, 78, 35, 180), width=2)
    d.line((128, 1242, 952, 1242), fill=(110, 78, 35, 180), width=2)
    rnd = random.Random(20260904)
    for _ in range(500):
        x, y = rnd.randint(0, W - 1), rnd.randint(0, H - 1)
        if 118 < x < 958 and (62 < y < 858 or 1248 < y < 1828):
            continue
        a = rnd.randint(10, 38)
        d.line((x, y, x + rnd.randint(-4, 4), y + rnd.randint(3, 18)), fill=(226, 213, 188, a), width=1)

    fps = 24
    total_frames = max(1, int(target * fps + 0.5))
    for frame_idx in range(total_frames):
        t = frame_idx / fps
        im = base.copy()
        td = ImageDraw.Draw(im)
        active = next((c for c in titles if c.start <= t <= c.end), None)
        if active:
            line1 = active.line1.upper().strip()
            line2 = active.line2.upper().strip()
            f1 = fit_font(td, line1, 640, 58, 30)
            f2 = fit_font(td, line2, 760, 102, 50)
            for text, y, f, fill in [(line1, 950, f1, (203, 137, 34, 255)), (line2, 1072, f2, (236, 222, 196, 255))]:
                b = td.textbbox((0, 0), text, font=f)
                tw, th = b[2] - b[0], b[3] - b[1]
                td.text(((W - tw) // 2, y - th // 2 - b[1]), text, font=f, fill=fill)
        im.save(frames_dir / f"frame_{frame_idx:05d}.png")

    run([
        FFMPEG, "-y", "-framerate", str(fps), "-i", str(frames_dir / "frame_%05d.png"),
        "-t", f"{target:.3f}", "-c:v", "qtrle", "-pix_fmt", "argb", str(path)
    ])


def auto_clips(trailer_duration: float, target_duration: float):
    chunk = 3.2
    n = max(1, int(target_duration // chunk))
    usable_start = min(8.0, max(0.0, trailer_duration * 0.05))
    usable_end = max(usable_start + chunk, trailer_duration - 8.0)
    span = max(0.0, usable_end - usable_start - chunk)
    out, remaining = [], target_duration
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
    pairs = [
        ("НОВОЕ КИНО", "ЧТО НУЖНО ЗНАТЬ"),
        ("ГЛАВНЫЙ ФАКТ", "ЗА 30 СЕКУНД"),
        ("ПОЧЕМУ ЭТО", "ИНТЕРЕСНО"),
        ("СТОИТ ВКЛЮЧАТЬ?", "РЕШАТЬ ТЕБЕ"),
    ]
    step = duration / len(pairs)
    out = [TitleCard(start=i * step, end=(i + 1) * step, line1=a, line2=b) for i, (a, b) in enumerate(pairs)]
    out[-1].end = duration
    return out


def normalize_clips(clips, target, trailer_duration):
    fixed, used = [], 0.0
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


def build_trailer_cut(trailer: Path, clips, job: Path) -> Path:
    parts = []
    for i, (s, e) in enumerate(clips):
        part = job / f"clip_{i:02d}.mp4"
        dur = max(0.1, e - s)
        run([
            FFMPEG, "-y", "-ss", f"{s:.3f}", "-i", str(trailer), "-t", f"{dur:.3f}",
            "-an", "-vf", "scale=840:796:force_original_aspect_ratio=increase,crop=840:796,fps=24,eq=contrast=1.06:brightness=-0.018:saturation=0.82",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23", "-threads", "1", "-pix_fmt", "yuv420p", str(part)
        ])
        parts.append(part)
    concat_file = job / "concat.txt"
    concat_file.write_text("\n".join(f"file '{p.name}'" for p in parts), encoding="utf-8")
    out = job / "trailer_cut.mp4"
    run([FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", str(concat_file), "-c", "copy", str(out)])
    return out


def render_job(job_id: str, req: RenderRequest):
    job = ROOT / job_id
    status_path = job / "status.json"
    started = time.time()
    with RENDER_LOCK:
        try:
            status_path.write_text(json.dumps({"status": "downloading"}), encoding="utf-8")
            presenter, trailer = job / "presenter.mp4", job / "trailer.mp4"
            asyncio.run(download(req.presenter_url, presenter))
            asyncio.run(download(req.trailer_url, trailer))

            pdur, tdur = probe_duration(presenter), probe_duration(trailer)
            target = max(1.0, min(req.duration or pdur, pdur, 60.0))
            raw_clips = [(c.start, c.end) for c in req.clips] if req.clips else auto_clips(tdur, target)
            clips = normalize_clips(raw_clips, target, tdur)
            titles = req.titles or default_titles(target)

            status_path.write_text(json.dumps({"status": "preparing"}), encoding="utf-8")
            trailer_cut = build_trailer_cut(trailer, clips, job)
            overlay = job / "overlay.mov"
            make_overlay(titles, target, overlay)

            status_path.write_text(json.dumps({"status": "rendering"}), encoding="utf-8")
            out = job / Path(req.output_name).name
            fc = (
                f"[1:v]trim=duration={target:.3f},setpts=PTS-STARTPTS,"
                "scale=840:580:force_original_aspect_ratio=increase,crop=840:580,fps=24,"
                "eq=contrast=1.045:brightness=-0.012:saturation=0.84[pv];"
                f"color=c=black:s=1080x1920:d={target:.3f}:r=24[bg];"
                "[bg][0:v]overlay=x=118:y=62:shortest=1[s1];"
                "[s1][pv]overlay=x=118:y=1248:shortest=1[s2];"
                "[2:v]format=rgba[ov];[s2][ov]overlay=0:0:shortest=1,format=yuv420p[vout];"
                f"[1:a]atrim=duration={target:.3f},asetpts=PTS-STARTPTS,highpass=f=70,"
                f"acompressor=threshold=0.08:ratio=2.5:attack=8:release=120,volume={req.voice_volume:.3f},alimiter=limit=0.95[aout]"
            )
            run([
                FFMPEG, "-y", "-i", str(trailer_cut), "-i", str(presenter), "-i", str(overlay),
                "-filter_complex_threads", "1", "-filter_threads", "1", "-filter_complex", fc,
                "-map", "[vout]", "-map", "[aout]", "-t", f"{target:.3f}",
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "22", "-threads", "1",
                "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(out)
            ])
            if not out.exists() or out.stat().st_size < 10_000:
                raise RuntimeError("FFmpeg completed but final MP4 is missing or too small")
            elapsed = round(time.time() - started, 2)
            status_path.write_text(json.dumps({"status": "completed", "duration": target, "elapsed": elapsed, "bytes": out.stat().st_size, "download_url": f"/download/{job_id}"}), encoding="utf-8")
            print(f"JOB {job_id} completed in {elapsed}s ({out.stat().st_size} bytes)", flush=True)
        except Exception as e:
            elapsed = round(time.time() - started, 2)
            status_path.write_text(json.dumps({"status": "failed", "elapsed": elapsed, "error": str(e)}), encoding="utf-8")
            print(f"JOB {job_id} failed after {elapsed}s: {e}", flush=True)


@app.get("/")
def root():
    return {"ok": True, "service": "vintage-movie-short-renderer", "version": VERSION}


@app.get("/health")
def health():
    return {"ok": True, "version": VERSION, "queue_locked": RENDER_LOCK.locked()}


@app.get("/check-url")
async def check_url(url: str = Query(...), authorization: Optional[str] = Header(default=None)):
    require_auth(authorization)
    return await check_remote_url(url)


@app.get("/selftest")
def selftest(authorization: Optional[str] = Header(default=None)):
    require_auth(authorization)
    p = ROOT / "selftest.mp4"
    started = time.time()
    run([
        FFMPEG, "-y", "-f", "lavfi", "-i", "color=c=black:s=360x640:r=24:d=1",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-shortest",
        "-c:v", "libx264", "-preset", "ultrafast", "-threads", "1", "-c:a", "aac", str(p)
    ])
    return {"ok": p.exists() and p.stat().st_size > 1000, "bytes": p.stat().st_size if p.exists() else 0, "elapsed": round(time.time() - started, 2), "version": VERSION}


@app.post("/render")
async def render(req: RenderRequest, bg: BackgroundTasks, authorization: Optional[str] = Header(default=None)):
    require_auth(authorization)
    await check_remote_url(req.trailer_url)
    await check_remote_url(req.presenter_url)
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
    job = ROOT / job_id
    status_path = job / "status.json"
    deadline = time.time() + 360
    while time.time() < deadline:
        if status_path.exists():
            data = json.loads(status_path.read_text(encoding="utf-8"))
            if data.get("status") == "failed":
                raise HTTPException(status_code=500, detail=data.get("error", "Render failed"))
            if data.get("status") == "completed":
                output = job / "final_short.mp4"
                if output.exists() and output.stat().st_size > 0:
                    return FileResponse(output, media_type="video/mp4", filename="final_short.mp4")
        time.sleep(2)
    raise HTTPException(status_code=408, detail="Video is still queued or rendering; retry download shortly")
