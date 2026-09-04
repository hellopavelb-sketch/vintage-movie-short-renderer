import asyncio
import json
import os
import random
import ipaddress
import socket
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlparse

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
VERSION = "1.3.1"
RENDER_LOCK = threading.Lock()

HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; VintageMovieShortRenderer/1.2)",
    "Accept": "*/*",
}

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


def write_status(path: Path, **data):
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def run(cmd):
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr[-12000:])
    return p


def probe_text(path: Path) -> str:
    p = subprocess.run(
        [FFMPEG, "-hide_banner", "-i", str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return p.stderr


def probe_duration(path: Path) -> float:
    text = probe_text(path)
    m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", text)
    if not m:
        raise RuntimeError(f"Could not detect duration for {path.name}")
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def has_audio(path: Path) -> bool:
    return " Audio: " in probe_text(path)


def validate_public_media_url(url: str):
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise RuntimeError("Only public http/https media URLs are supported")
    if parsed.port not in (None, 80, 443):
        raise RuntimeError("Only standard HTTP/HTTPS ports are allowed")
    host = parsed.hostname.lower()
    if host in {"localhost"} or host.endswith((".local", ".internal", ".localhost")):
        raise RuntimeError("Private/local hosts are not allowed")
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except Exception as e:
        raise RuntimeError(f"Could not resolve media host: {e}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            raise RuntimeError("Private/local IP addresses are not allowed")


async def preflight_url(url: str, label: str):
    timeout = httpx.Timeout(25.0, connect=12.0)
    headers = {**HTTP_HEADERS, "Range": "bytes=0-65535"}
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=timeout, headers=headers) as client:
            async with client.stream("GET", url) as r:
                if r.status_code >= 400:
                    raise RuntimeError(f"{label} URL returned HTTP {r.status_code}")
                first = b""
                async for chunk in r.aiter_bytes(65536):
                    first += chunk
                    if len(first) >= 1024:
                        break
                if len(first) < 256:
                    raise RuntimeError(f"{label} URL returned too little data")
                return {
                    "ok": True,
                    "status_code": r.status_code,
                    "content_type": r.headers.get("content-type"),
                    "final_url": str(r.url),
                }
    except Exception as e:
        raise RuntimeError(f"{label} preflight failed: {e}") from e


async def download(url: str, dst: Path, label: str):
    timeout = httpx.Timeout(240.0, connect=30.0)
    last_error = None
    for attempt in range(1, 4):
        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=timeout,
                headers=HTTP_HEADERS,
            ) as client:
                async with client.stream("GET", url) as r:
                    r.raise_for_status()
                    with dst.open("wb") as f:
                        async for chunk in r.aiter_bytes(1024 * 1024):
                            f.write(chunk)
            if not dst.exists() or dst.stat().st_size < 1024:
                raise RuntimeError("downloaded file is empty or too small")
            return
        except Exception as e:
            last_error = e
            try:
                dst.unlink(missing_ok=True)
            except Exception:
                pass
            if attempt < 3:
                time.sleep(1.5 * attempt)
    raise RuntimeError(f"{label} download failed after 3 attempts: {last_error}")


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
            d.ellipse((x - r, y - r, x + r, y + r), fill=(226, 213, 188, a))

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
    pairs = [
        ("НОВОЕ КИНО", "ЧТО НУЖНО ЗНАТЬ"),
        ("ГЛАВНЫЙ ФАКТ", "ЗА 30 СЕКУНД"),
        ("ПОЧЕМУ ЭТО", "ИНТЕРЕСНО"),
        ("СТОИТ ВКЛЮЧАТЬ?", "РЕШАТЬ ТЕБЕ"),
    ]
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
        d = min(
            max(0.10, float(e) - float(s)),
            target - used,
            max(0.10, trailer_duration - s),
        )
        fixed.append((s, s + d))
        used += d

    if used < target:
        d = target - used
        s = max(0.0, min(trailer_duration - d - 0.1, trailer_duration * 0.70))
        fixed.append((s, min(trailer_duration, s + d)))

    return fixed


def build_render(job_id: str, req: RenderRequest, presenter: Path, trailer: Path, started: float):
    job = ROOT / job_id
    status_path = job / "status.json"

    pdur = probe_duration(presenter)
    tdur = probe_duration(trailer)
    if not has_audio(presenter):
        raise RuntimeError("Presenter video has no audio track")

    trailer_has_audio = has_audio(trailer)
    target = max(1.0, min(req.duration or pdur, pdur, 60.0))
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

    write_status(status_path, status="rendering")
    output_name = Path(req.output_name).name or "final_short.mp4"
    out = job / output_name

    cmd = [
        FFMPEG, "-y",
        "-i", str(trailer),
        "-i", str(presenter),
        "-framerate", "24", "-loop", "1", "-i", str(master),
    ]
    for p in title_paths:
        cmd += ["-framerate", "24", "-loop", "1", "-i", str(p)]

    n = len(clips)
    fc = [f"[0:v]split={n}" + "".join(f"[tv{i}]" for i in range(n))]
    if trailer_has_audio:
        fc.append(f"[0:a]asplit={n}" + "".join(f"[ta{i}]" for i in range(n)))

    concat_parts = []
    for i, (s, e) in enumerate(clips):
        fc.append(
            f"[tv{i}]trim=start={s:.3f}:end={e:.3f},setpts=PTS-STARTPTS,"
            "scale=840:796:force_original_aspect_ratio=increase,crop=840:796,fps=24,"
            "eq=contrast=1.06:brightness=-0.018:saturation=0.82,vignette=PI/5"
            f"[v{i}]"
        )
        if trailer_has_audio:
            fc.append(
                f"[ta{i}]atrim=start={s:.3f}:end={e:.3f},asetpts=PTS-STARTPTS[a{i}]"
            )
            concat_parts += [f"[v{i}]", f"[a{i}]"]
        else:
            concat_parts += [f"[v{i}]"]

    if trailer_has_audio:
        fc.append("".join(concat_parts) + f"concat=n={n}:v=1:a=1[trv][tra]")
    else:
        fc.append("".join(concat_parts) + f"concat=n={n}:v=1:a=0[trv]")
        fc.append(f"anullsrc=r=48000:cl=stereo:d={target:.3f}[tra]")

    fc.append(
        f"[1:v]trim=duration={target:.3f},setpts=PTS-STARTPTS,"
        "scale=840:580:force_original_aspect_ratio=increase,crop=840:580,fps=24,"
        "eq=contrast=1.045:brightness=-0.012:saturation=0.84,vignette=PI/6[pv]"
    )
    fc.append(f"color=c=black:s=1080x1920:r=24:d={target:.3f}[bg]")
    fc.append("[bg][trv]overlay=x=118:y=62:shortest=1[s1]")
    fc.append("[s1][pv]overlay=x=118:y=1248:shortest=1[s2]")
    fc.append("[2:v]format=rgba[master]")
    fc.append("[s2][master]overlay=0:0:shortest=1[s3]")

    prev = "s3"
    for i, card in enumerate(titles):
        inp = 3 + i
        out_name = f"s{4 + i}"
        fc.append(f"[{inp}:v]format=rgba[t{i}]")
        fc.append(
            f"[{prev}][t{i}]overlay=0:0:"
            f"enable='between(t,{card.start:.3f},{card.end:.3f})'[{out_name}]"
        )
        prev = out_name

    fc.append(f"[{prev}]format=yuv420p[vout]")
    fc.append(
        f"[1:a]atrim=duration={target:.3f},asetpts=PTS-STARTPTS,"
        f"highpass=f=70,acompressor=threshold=0.08:ratio=2.5:attack=8:release=120,"
        f"volume={req.voice_volume:.3f}[voice]"
    )
    fc.append(
        f"[tra]atrim=duration={target:.3f},asetpts=PTS-STARTPTS,"
        f"volume={req.trailer_volume:.3f},lowpass=f=12000,"
        f"afade=t=in:st=0:d=0.18,"
        f"afade=t=out:st={max(0.0, target - 0.35):.3f}:d=0.35[bed]"
    )
    fc.append("[voice][bed]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95[aout]")

    cmd += [
        "-filter_complex", ";".join(fc),
        "-map", "[vout]",
        "-map", "[aout]",
        "-t", f"{target:.3f}",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "22",
        "-c:a", "aac",
        "-b:a", "160k",
        "-movflags", "+faststart",
        str(out),
    ]

    run(cmd)

    if not out.exists() or out.stat().st_size < 10_000:
        raise RuntimeError("FFmpeg finished but final MP4 is missing or too small")

    elapsed = round(time.time() - started, 2)
    write_status(
        status_path,
        status="completed",
        duration=target,
        elapsed=elapsed,
        bytes=out.stat().st_size,
        output_name=output_name,
        download_url=f"/download/{job_id}",
    )
    print(f"JOB {job_id} completed in {elapsed}s ({out.stat().st_size} bytes)", flush=True)


def render_job(job_id: str, req: RenderRequest):
    job = ROOT / job_id
    status_path = job / "status.json"
    started = time.time()

    with RENDER_LOCK:
        try:
            write_status(status_path, status="downloading")
            presenter = job / "presenter.mp4"
            trailer = job / "trailer.mp4"
            asyncio.run(download(req.presenter_url, presenter, "presenter"))
            asyncio.run(download(req.trailer_url, trailer, "trailer"))
            build_render(job_id, req, presenter, trailer, started)
        except Exception as e:
            elapsed = round(time.time() - started, 2)
            write_status(status_path, status="failed", elapsed=elapsed, error=str(e))
            print(f"JOB {job_id} failed after {elapsed}s: {e}", flush=True)


def selftest_job(job_id: str):
    job = ROOT / job_id
    status_path = job / "status.json"
    started = time.time()

    with RENDER_LOCK:
        try:
            write_status(status_path, status="generating_test_media")
            trailer = job / "trailer.mp4"
            presenter = job / "presenter.mp4"

            run([
                FFMPEG, "-y",
                "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=24",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
                "-t", "6",
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "96k",
                str(trailer),
            ])

            run([
                FFMPEG, "-y",
                "-f", "lavfi", "-i", "color=c=0x2b2b2b:size=1920x1080:rate=24",
                "-f", "lavfi", "-i", "sine=frequency=700:sample_rate=48000",
                "-vf", "drawbox=x=650:y=180:w=620:h=720:color=0x6a5a45@0.85:t=fill",
                "-t", "6",
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "96k",
                str(presenter),
            ])

            req = RenderRequest(
                presenter_url="internal://presenter",
                trailer_url="internal://trailer",
                duration=5.0,
                trailer_volume=0.12,
                voice_volume=1.0,
                output_name="final_short.mp4",
                titles=[
                    TitleCard(start=0, end=2.5, line1="SELF TEST", line2="RENDER OK?"),
                    TitleCard(start=2.5, end=5.0, line1="PIPELINE", line2="FINAL MP4"),
                ],
            )
            build_render(job_id, req, presenter, trailer, started)
        except Exception as e:
            elapsed = round(time.time() - started, 2)
            write_status(status_path, status="failed", elapsed=elapsed, error=str(e))
            print(f"SELFTEST {job_id} failed after {elapsed}s: {e}", flush=True)


@app.get("/")
def root():
    return {
        "ok": True,
        "service": "vintage-movie-short-renderer",
        "version": VERSION,
    }


@app.get("/health")
def health():
    return {"ok": True, "version": VERSION, "queue_locked": RENDER_LOCK.locked()}

@app.get("/check-url")
async def check_url(url: str = Query(...)):
    try:
        validate_public_media_url(url)
        return await preflight_url(url, "media")
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e))


@app.get("/diagnostics")
def diagnostics(authorization: Optional[str] = Header(default=None)):
    require_auth(authorization)
    usage = shutil.disk_usage(ROOT)
    encoders = subprocess.run(
        [FFMPEG, "-hide_banner", "-encoders"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ).stdout
    return {
        "ok": True,
        "version": VERSION,
        "ffmpeg": FFMPEG,
        "libx264": "libx264" in encoders,
        "aac": " AAC " in encoders or " aac " in encoders,
        "disk_free_mb": round(usage.free / 1024 / 1024, 1),
    }


@app.post("/preflight")
async def preflight(req: RenderRequest, authorization: Optional[str] = Header(default=None)):
    require_auth(authorization)
    try:
        presenter, trailer = await asyncio.gather(
            preflight_url(req.presenter_url, "presenter"),
            preflight_url(req.trailer_url, "trailer"),
        )
        return {"ok": True, "presenter": presenter, "trailer": trailer}
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e))


@app.post("/render")
async def render(
    req: RenderRequest,
    bg: BackgroundTasks,
    authorization: Optional[str] = Header(default=None),
):
    require_auth(authorization)

    try:
        await asyncio.gather(
            preflight_url(req.presenter_url, "presenter"),
            preflight_url(req.trailer_url, "trailer"),
        )
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e))

    job_id = uuid.uuid4().hex
    job = ROOT / job_id
    job.mkdir(parents=True, exist_ok=True)
    (job / "request.json").write_text(req.model_dump_json(indent=2), encoding="utf-8")
    write_status(job / "status.json", status="queued")
    bg.add_task(render_job, job_id, req)

    return {
        "job_id": job_id,
        "status_url": f"/status/{job_id}",
        "download_url": f"/download/{job_id}",
    }


@app.get("/selftest")
def selftest_quick(authorization: Optional[str] = Header(default=None)):
    require_auth(authorization)
    p = ROOT / "selftest_quick.mp4"
    started = time.time()
    run([
        FFMPEG, "-y",
        "-f", "lavfi", "-i", "color=c=black:s=360x640:r=24:d=1",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
        "-shortest",
        "-c:v", "libx264", "-preset", "ultrafast", "-threads", "1",
        "-c:a", "aac",
        str(p),
    ])
    return {
        "ok": p.exists() and p.stat().st_size > 1000,
        "bytes": p.stat().st_size if p.exists() else 0,
        "elapsed": round(time.time() - started, 2),
        "version": VERSION,
    }


@app.post("/selftest/full")
def selftest_full(
    bg: BackgroundTasks,
    authorization: Optional[str] = Header(default=None),
):
    require_auth(authorization)
    job_id = uuid.uuid4().hex
    job = ROOT / job_id
    job.mkdir(parents=True, exist_ok=True)
    write_status(job / "status.json", status="queued")
    bg.add_task(selftest_job, job_id)
    return {
        "job_id": job_id,
        "status_url": f"/status/{job_id}",
        "download_url": f"/download/{job_id}",
    }


@app.get("/status/{job_id}")
def status(job_id: str, authorization: Optional[str] = Header(default=None)):
    require_auth(authorization)
    p = ROOT / job_id / "status.json"
    if not p.exists():
        raise HTTPException(404, "Job not found")
    data = json.loads(p.read_text(encoding="utf-8"))
    if data.get("status") == "failed":
        raise HTTPException(status_code=422, detail=data)
    return data


@app.get("/download/{job_id}")
def download_result(job_id: str, authorization: Optional[str] = Header(default=None)):
    require_auth(authorization)
    job = ROOT / job_id
    status_path = job / "status.json"

    deadline = time.time() + 300
    while time.time() < deadline:
        if status_path.exists():
            data = json.loads(status_path.read_text(encoding="utf-8"))
            if data.get("status") == "failed":
                raise HTTPException(status_code=422, detail=data)
            output_name = Path(data.get("output_name", "final_short.mp4")).name
        else:
            output_name = "final_short.mp4"

        output = job / output_name
        if output.exists() and output.stat().st_size > 10_000:
            return FileResponse(output, media_type="video/mp4", filename=output_name)

        time.sleep(2)

    raise HTTPException(
        status_code=408,
        detail="Video is still rendering; retry download shortly",
    )
