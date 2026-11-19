from __future__ import annotations

import json
import math
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import wave
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import URLError, HTTPError
from urllib.request import Request, urlopen

import numpy as np

try:
    import tkinter as tk
    from tkinter import ttk, messagebox
except ImportError:  # headless test environments
    tk = None  # type: ignore

APP_NAME = "AutoclipperZero"
APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "AutoclipperZeroData"
RECORDINGS_DIR = DATA_DIR / "recordings"
OUTPUT_DIR = DATA_DIR / "ready_to_upload"
TEMP_DIR = DATA_DIR / "temp"
STATE_FILE = DATA_DIR / "state.json"
CONFIG_FILE = APP_DIR / "config.json"
DEFAULT_URL = "https://www.twitch.tv/zerkaa"
SAMPLE_RATE = 16000
VOD_RESCAN_SECONDS = 30 * 60
VOD_RE = re.compile(r"twitch\.tv/videos/(\d+)", re.I)
URL_RE = re.compile(r"^https?://(www\.|m\.)?twitch\.tv/(videos/\d+|[A-Za-z0-9_]+)/?(\?.*)?$", re.I)

for _folder in (DATA_DIR, RECORDINGS_DIR, OUTPUT_DIR, TEMP_DIR):
    _folder.mkdir(parents=True, exist_ok=True)

DEFAULT_CONFIG: dict[str, Any] = {
    "channel_url": DEFAULT_URL,
    "source_mode": "vods",          # "live" or "vods"
    "quality": "1080p60,1080p,best",
    "segment_minutes": 5,
    "clip_seconds": 42,
    "clips_per_segment": 2,
    "vod_count": 2,                  # how many recent past broadcasts to scan
    "max_clips_per_vod": 12,         # 0 = unlimited
    "whisper_model": "medium",
    "whisper_device": "auto",        # "auto", "cpu" or "cuda"
    "ollama_url": "http://127.0.0.1:11434",
    "ollama_model": "qwen3.5:4b",
    "retention_hours": 48,
    "language": "en",
}


def load_config() -> dict[str, Any]:
    cfg = DEFAULT_CONFIG.copy()
    if CONFIG_FILE.exists():
        try:
            saved = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                cfg.update(saved)
        except Exception:
            pass
    if cfg.get("quality") == "best":
        cfg["quality"] = "1080p60,1080p,best"
    if not cfg.get("whisper_model"):
        cfg["whisper_model"] = "medium"
    if not cfg.get("ollama_model"):
        cfg["ollama_model"] = "qwen3.5:4b"
    if cfg.get("source_mode") not in ("live", "vods"):
        cfg["source_mode"] = "vods"
    return cfg


def save_config(cfg: dict[str, Any]) -> None:
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def which(name: str) -> str | None:
    return shutil.which(name)


def run_quiet(args: list[str], timeout: int = 12) -> tuple[int, str]:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout, errors="replace")
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as exc:
        return 1, str(exc)


def run_capture(args: list[str], timeout: int = 120) -> tuple[int, str, str]:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout, errors="replace")
        return p.returncode, p.stdout or "", p.stderr or ""
    except Exception as exc:
        return 1, "", str(exc)


def ffmpeg_path() -> str | None:
    return which("ffmpeg.exe") or which("ffmpeg")


def ffprobe_path() -> str | None:
    return which("ffprobe.exe") or which("ffprobe")


def safe_filename(value: str, max_len: int = 60) -> str:
    value = re.sub(r"[^A-Za-z0-9 _-]+", "", value).strip()
    value = re.sub(r"\s+", "_", value)
    return (value[:max_len].strip("_- ") or "Twitch_Highlight")


def log_line(message: str) -> str:
    return f"[{datetime.now().strftime('%H:%M:%S')}] {message}"


def hms(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def twitch_time(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 3600}h{(s % 3600) // 60}m{s % 60}s"


def channel_from_url(url: str) -> str:
    m = re.search(r"twitch\.tv/([A-Za-z0-9_]+)", url, re.I)
    name = m.group(1) if m else "twitch"
    return "twitch" if name.lower() == "videos" else name


def read_state() -> dict[str, Any]:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("processed", {})
            data.setdefault("clips", [])
            data.setdefault("vods", {})
            return data
    except Exception:
        pass
    return {"processed": {}, "clips": [], "vods": {}}


def write_state(state: dict[str, Any]) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


def pump_pipe(pipe, callback):
    try:
        for raw in iter(pipe.readline, b""):
            text = raw.decode("utf-8", errors="replace").strip()
            if text:
                callback(text)
    except Exception:
        pass
    finally:
        try:
            pipe.close()
        except Exception:
            pass


# ----------------------------------------------------------------------------
# Audio helpers. Audio is decoded by FFmpeg and handed to Whisper as a NumPy
# array, so faster-whisper never has to open the video file through PyAV.
# (That PyAV call was what raised "unexpected keyword argument 'metadata_errors'".)
# ----------------------------------------------------------------------------
def extract_audio(media: Path, ffmpeg: str, wav_path: Path) -> np.ndarray | None:
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(media),
           "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le", str(wav_path)]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=1200)
        if p.returncode != 0 or not wav_path.exists():
            return None
        with wave.open(str(wav_path), "rb") as wav:
            if wav.getsampwidth() != 2 or wav.getnchannels() != 1:
                return None
            raw = wav.readframes(wav.getnframes())
        return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    except Exception:
        return None
    finally:
        try:
            wav_path.unlink(missing_ok=True)
        except Exception:
            pass


def energy_profile(audio: np.ndarray) -> list[float]:
    """One RMS dBFS value per second."""
    n = len(audio) // SAMPLE_RATE
    if n <= 0:
        return []
    frames = audio[: n * SAMPLE_RATE].reshape(n, SAMPLE_RATE)
    rms = np.sqrt(np.mean(frames * frames, axis=1))
    db = 20.0 * np.log10(np.maximum(rms, 1e-6))
    return np.clip(db, -90.0, 0.0).tolist()


def add_cuda_dll_dirs() -> None:
    """Make pip-installed NVIDIA CUDA/cuDNN DLLs visible to CTranslate2 on Windows."""
    for base in list(sys.path):
        nvidia = Path(base) / "nvidia"
        if not nvidia.is_dir():
            continue
        for sub in nvidia.iterdir():
            bin_dir = sub / "bin"
            if bin_dir.is_dir():
                try:
                    if hasattr(os, "add_dll_directory"):
                        os.add_dll_directory(str(bin_dir))
                except Exception:
                    pass
                os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")


def group_windows(segments: list[Any], duration: float, clip_seconds: int, energy: list[float]) -> list[dict[str, Any]]:
    windows: list[dict[str, Any]] = []
    step = max(10, clip_seconds // 3)
    start = 0.0
    idx = 0
    while start < duration - 8:
        end = min(duration, start + clip_seconds)
        pieces = [s for s in segments if float(s.start) < end and float(s.end) > start]
        text = " ".join(str(s.text).strip() for s in pieces).strip()
        if len(text) >= 12:
            lo = max(0, int(start))
            hi = min(len(energy), int(math.ceil(end)))
            e = energy[lo:hi]
            if e:
                avg = sum(e) / len(e)
                peak = max(e)
                audio_score = max(0, min(100, (avg + 55) * 1.8 + max(0, peak + 25) * 0.45))
            else:
                audio_score = 0.0
            windows.append({"id": idx, "start": round(start, 2), "end": round(end, 2),
                            "text": text[:650], "audio_score": round(audio_score, 1)})
            idx += 1
        start += step
    return windows


def heuristic_score(text: str, audio_score: float) -> float:
    lower = text.lower()
    phrases = ["oh my god", "no way", "what the", "let's go", "lets go", "are you serious",
               "that was", "i can't believe", "bro", "wait", "wow", "holy", "insane",
               "actually", "help me", "why", "how did", "that's crazy", "thats crazy",
               "got him", "clutch", "one hp", "lmao", "hahaha", "laugh"]
    hits = sum(1 for phrase in phrases if phrase in lower)
    punctuation = min(12, text.count("!") * 3 + text.count("?") * 1.5)
    length_bonus = 5 if 35 <= len(text) <= 320 else 0
    return max(0.0, min(100.0, 22 + hits * 7 + punctuation + length_bonus + audio_score * 0.35))


def call_ollama(cfg: dict[str, Any], windows: list[dict[str, Any]], log) -> list[dict[str, Any]] | None:
    if not windows:
        return None
    endpoint = cfg.get("ollama_url", DEFAULT_CONFIG["ollama_url"]).rstrip("/") + "/api/chat"
    compact = [{"id": w["id"], "time": f'{int(w["start"])}-{int(w["end"])} sec',
                "audio_activity_0_100": w["audio_score"], "transcript": w["text"]} for w in windows]
    prompt = (
        "You are selecting short-form Twitch highlights. Rank the supplied candidate windows for short video value. "
        "Prefer genuinely funny, surprising, exciting, skillful, emotional or memorable moments that make sense without long context. "
        "Penalize silence, routine gameplay, menus, repeated filler and incomplete setup. Audio activity is only supporting evidence; do not equate loudness with quality. "
        "Return ONLY valid JSON: an array of objects with keys id, score (0-100 integer), title (short clean title, max 65 chars), reason (max 120 chars). "
        "Do not invent events. Include every candidate id.\nCANDIDATES:\n" + json.dumps(compact, ensure_ascii=False)
    )
    body = {"model": cfg.get("ollama_model", "qwen3.5:4b"),
            "messages": [{"role": "user", "content": prompt}],
            "stream": False, "format": "json", "think": False,
            "options": {"temperature": 0.2, "num_predict": 2500, "num_ctx": 8192}}
    try:
        req = Request(endpoint, data=json.dumps(body).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(req, timeout=240) as response:
            data = json.loads(response.read().decode("utf-8"))
        content = data.get("message", {}).get("content", "")
        parsed = json.loads(content)
        if isinstance(parsed, dict):
            parsed = parsed.get("clips", parsed.get("results", parsed.get("candidates", [])))
        if isinstance(parsed, list):
            return parsed
    except (URLError, HTTPError, TimeoutError, json.JSONDecodeError, KeyError, ValueError) as exc:
        log(f"Ollama unavailable or returned unusable output; using local fallback scoring ({exc}).")
    except Exception as exc:
        log(f"Ollama scoring failed; using local fallback scoring ({exc}).")
    return None


def ass_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    cs = int(round(seconds * 100))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, c = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{c:02d}"


def make_ass(words: list[tuple[float, float, str]], clip_start: float, clip_end: float, ass_path: Path, centered: bool = False) -> int:
    selected = []
    for start, end, word in words:
        if end <= clip_start or start >= clip_end:
            continue
        word = word.replace("{", "(").replace("}", ")").replace("\\", "").strip()
        if not word:
            continue
        selected.append((max(clip_start, start) - clip_start, min(clip_end, end) - clip_start, word))
    lines = [
        "[Script Info]", "ScriptType: v4.00+", "PlayResX: 1080", "PlayResY: 1920", "WrapStyle: 2", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding",
        f"Style: Default,Arial,64,&H00FFFFFF,&H0000D7FF,&H00101010,&H90000000,-1,0,0,0,100,100,1,0,1,4,2,{5 if centered else 2},70,70,{0 if centered else 250},1", "",
        "[Events]", "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text"
    ]
    grouped: list[list[tuple[float, float, str]]] = []
    current: list[tuple[float, float, str]] = []
    for item in selected:
        if current and (len(current) >= 5 or item[0] - current[-1][1] > 0.65):
            grouped.append(current)
            current = []
        current.append(item)
    if current:
        grouped.append(current)
    for group in grouped:
        event_start = max(0.0, group[0][0] - 0.04)
        event_end = min(clip_end - clip_start, group[-1][1] + 0.25)
        if event_end <= event_start:
            continue
        chunks = []
        for start, end, word in group:
            duration_cs = max(5, int(round(max(0.05, end - start) * 100)))
            chunks.append(f"{{\\kf{duration_cs}}}{word}")
        text_parts = [" ".join(chunks[i:i + 3]) for i in range(0, len(chunks), 3)]
        caption_text = r"\N".join(text_parts)
        lines.append(f"Dialogue: 0,{ass_time(event_start)},{ass_time(event_end)},Default,,0,0,0,,{caption_text}")
    ass_path.write_text("\n".join(lines), encoding="utf-8-sig")
    return len(grouped)


@dataclass
class ClipCandidate:
    start: float
    end: float
    score: float
    title: str
    reason: str
    transcript: str


@dataclass
class SourceContext:
    label: str                 # channel display name
    tag: str                   # unique piece used in output file names
    base_offset: float = 0.0   # seconds into the VOD where this chunk starts
    vod_url: str | None = None


class AutoclipperEngine:
    def __init__(self, cfg: dict[str, Any], log_callback, status_callback):
        self.cfg = cfg
        self.log = log_callback
        self.status = status_callback
        self.stop_event = threading.Event()
        self.capture_thread: threading.Thread | None = None
        self.process_thread: threading.Thread | None = None
        self.proc_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.stream_proc: subprocess.Popen | None = None
        self.ffmpeg_proc: subprocess.Popen | None = None
        self.recorder_active = False
        self.whisper = None
        self.encoder = "libx264"
        state = read_state()
        self.processed: dict[str, Any] = state["processed"]
        self.clip_history: list[dict[str, Any]] = state["clips"]
        self.vods: dict[str, Any] = state["vods"]
        self.failures: dict[str, int] = {}
        self.channel = channel_from_url(cfg.get("channel_url", DEFAULT_URL))
        self.prefix = safe_filename(self.channel, 30)

    # ------------------------------------------------------------------ state
    def save_state(self):
        with self.state_lock:
            write_state({"processed": self.processed, "clips": self.clip_history, "vods": self.vods})

    # -------------------------------------------------------------- lifecycle
    def start(self):
        self.stop_event.clear()
        ffmpeg = ffmpeg_path()
        if ffmpeg:
            encoders = run_quiet([ffmpeg, "-hide_banner", "-encoders"], timeout=20)[1]
            self.encoder = "h264_nvenc" if "h264_nvenc" in encoders else "libx264"
        self.log(f"Video encoder: {self.encoder}.")
        if self.cfg.get("source_mode") == "vods":
            self.capture_thread = threading.Thread(target=self.vod_loop, daemon=True)
            self.process_thread = None
        else:
            self.capture_thread = threading.Thread(target=self.capture_loop, daemon=True)
            self.process_thread = threading.Thread(target=self.processing_loop, daemon=True)
        self.capture_thread.start()
        if self.process_thread:
            self.process_thread.start()
        self.status("Monitoring" if self.cfg.get("source_mode") == "live" else "Scanning broadcasts")

    def stop(self):
        self.stop_event.set()
        with self.proc_lock:
            for proc in (self.stream_proc, self.ffmpeg_proc):
                if proc and proc.poll() is None:
                    try:
                        proc.terminate()
                    except Exception:
                        pass
        self.status("Stopping…")

    # ------------------------------------------------------------- live mode
    def capture_loop(self):
        ffmpeg = ffmpeg_path()
        if not ffmpeg:
            self.log("FFmpeg was not found. Run Setup.ps1, then reopen the app.")
            self.status("Setup required")
            self.stop_event.set()
            return
        self.log(f"Requested capture quality: {self.cfg.get('quality')} (falls back if unavailable).")
        self.log("Starting capture. If the channel is offline, Streamlink keeps retrying; "
                 "a 'Channel is live' line appears once recording really begins.")
        while not self.stop_event.is_set():
            stamp = "%Y-%m-%d_%H-%M-%S"
            output_pattern = str(RECORDINGS_DIR / f"{self.prefix}_{stamp}.mkv")
            stream_cmd = [sys.executable, "-m", "streamlink", "--loglevel", "warning",
                          "--retry-streams", "30", "--retry-max", "0", "--stdout",
                          self.cfg.get("channel_url", DEFAULT_URL), self.cfg.get("quality", "1080p60,1080p,best")]
            ff_cmd = [ffmpeg, "-hide_banner", "-loglevel", "warning", "-i", "pipe:0",
                      "-map", "0:v:0", "-map", "0:a?", "-c", "copy", "-f", "segment",
                      "-segment_time", str(max(60, int(self.cfg.get("segment_minutes", 5)) * 60)),
                      "-reset_timestamps", "1", "-strftime", "1", "-segment_format", "matroska", output_pattern]
            try:
                stream = subprocess.Popen(stream_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
                if stream.stdout is None:
                    raise RuntimeError("Streamlink did not create a media output pipe")
                ff = subprocess.Popen(ff_cmd, stdin=stream.stdout, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.PIPE, bufsize=0)
                stream.stdout.close()
                with self.proc_lock:
                    self.stream_proc, self.ffmpeg_proc = stream, ff
                self.recorder_active = True
                launched = time.time()
                announced = False
                threading.Thread(target=pump_pipe, args=(stream.stderr, lambda s: self.log("Streamlink: " + s)), daemon=True).start()
                threading.Thread(target=pump_pipe, args=(ff.stderr, lambda s: self.log("FFmpeg: " + s)), daemon=True).start()
                while not self.stop_event.is_set() and stream.poll() is None and ff.poll() is None:
                    time.sleep(0.5)
                    if not announced:
                        for f in RECORDINGS_DIR.glob(f"{self.prefix}_*.mkv"):
                            try:
                                if f.stat().st_mtime >= launched - 2:
                                    announced = True
                                    self.log(f"Channel is live. Recording to {f.name}")
                                    break
                            except OSError:
                                pass
                if stream.poll() is None:
                    try:
                        stream.terminate()
                    except Exception:
                        pass
                try:
                    ff.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    ff.kill()
                try:
                    stream.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    stream.kill()
                with self.proc_lock:
                    self.stream_proc = None
                    self.ffmpeg_proc = None
                self.recorder_active = False
                if self.stop_event.is_set():
                    break
                self.log("Capture ended or disconnected. Reconnecting in 15 seconds…")
                self.stop_event.wait(15)
            except FileNotFoundError as exc:
                self.log(f"Missing dependency: {exc}")
                self.stop_event.set()
                break
            except Exception as exc:
                self.recorder_active = False
                self.log(f"Capture error: {exc}. Retrying in 20 seconds.")
                self.stop_event.wait(20)
        self.recorder_active = False
        self.log("Capture stopped.")
        self.status("Stopped")

    def processing_loop(self):
        self.log("Highlight processor is ready; it will analyze each completed recording segment.")
        while not self.stop_event.wait(8):
            try:
                self._process_available_segments()
                self._cleanup_old_recordings()
            except Exception as exc:
                self.log(f"Processor warning: {exc}")
        while self.recorder_active:
            time.sleep(1)
        try:
            self._process_available_segments(force_all=True)
        except Exception as exc:
            self.log(f"Final processing pass warning: {exc}")
        self.log("Highlight processor stopped.")
        self.status("Stopped")

    def _process_available_segments(self, force_all: bool = False):
        files = sorted(RECORDINGS_DIR.glob("*.mkv"), key=lambda p: p.stat().st_mtime)
        if not files:
            return
        active_newest = files[-1] if self.recorder_active else None
        for media in files:
            key = str(media.resolve())
            if key in self.processed:
                continue
            if active_newest and media == active_newest and not force_all:
                continue
            try:
                stat = media.stat()
                if not force_all and time.time() - stat.st_mtime < 12:
                    continue
                stamp = datetime.fromtimestamp(stat.st_mtime).strftime("%Y%m%d_%H%M%S")
                ctx = SourceContext(label=self.channel, tag=stamp)
                self.process_segment(media, ctx)
                self.processed[key] = {"processed": datetime.now().isoformat(timespec="seconds"), "size": stat.st_size}
                self.save_state()
            except Exception as exc:
                # Give up after two failures instead of retrying every 8 seconds forever.
                self.failures[key] = self.failures.get(key, 0) + 1
                if self.failures[key] >= 2:
                    self.log(f"Giving up on {media.name} after 2 failed attempts. Reason: {exc}")
                    self.processed[key] = {"processed": datetime.now().isoformat(timespec="seconds"),
                                           "failed": str(exc)[:200]}
                    self.save_state()
                continue

    def _cleanup_old_recordings(self):
        retention_hours = max(1, int(self.cfg.get("retention_hours", 48)))
        cutoff = time.time() - retention_hours * 3600
        for media in RECORDINGS_DIR.glob("*.mkv"):
            try:
                key = str(media.resolve())
                if media.stat().st_mtime < cutoff and key in self.processed:
                    media.unlink(missing_ok=True)
                    self.processed.pop(key, None)
                    self.save_state()
            except Exception:
                pass

    # -------------------------------------------------------------- VOD mode
    def vod_loop(self):
        ffmpeg = ffmpeg_path()
        if not ffmpeg:
            self.log("FFmpeg was not found. Run Setup.ps1, then reopen the app.")
            self.status("Setup required")
            self.stop_event.set()
            return
        single = bool(VOD_RE.search(self.cfg.get("channel_url", "")))
        self.log("Past-broadcast mode: AutoclipperZero downloads each broadcast in chunks, "
                 "clips each chunk, then deletes the chunk.")
        while not self.stop_event.is_set():
            try:
                vods = self._list_vods()
            except Exception as exc:
                self.log(f"Could not list past broadcasts: {exc}")
                vods = []
            if not vods:
                self.log("No past broadcasts found.")
            for vod in vods:
                if self.stop_event.is_set():
                    break
                if self.vods.get(vod["id"], {}).get("done"):
                    continue
                try:
                    self._process_vod(vod)
                except Exception as exc:
                    self.log(f"Broadcast {vod['id']} failed: {exc}")
            if self.stop_event.is_set():
                break
            if single:
                self.log("Finished the selected broadcast.")
                self.stop_event.set()
                break
            self.status("Watching for new broadcasts")
            self.log("All scanned broadcasts are done. Checking for new ones in 30 minutes.")
            self.stop_event.wait(VOD_RESCAN_SECONDS)
            self.status("Scanning broadcasts")
        self.log("Stopped.")
        self.status("Stopped")

    def _vod_info(self, url: str) -> dict[str, Any]:
        code, out, err = run_capture([sys.executable, "-m", "yt_dlp", "--no-playlist", "--no-warnings",
                                      "--print", "%(uploader)s|||%(duration)s|||%(title)s", url], timeout=90)
        info: dict[str, Any] = {}
        if code == 0 and out.strip():
            parts = out.strip().splitlines()[-1].split("|||", 2)
            if len(parts) == 3:
                info["uploader"] = None if parts[0] in ("NA", "") else parts[0]
                try:
                    info["duration"] = float(parts[1])
                except ValueError:
                    info["duration"] = None
                info["title"] = parts[2]
        return info

    def _list_vods(self) -> list[dict[str, Any]]:
        url = self.cfg.get("channel_url", DEFAULT_URL).strip()
        m = VOD_RE.search(url)
        if m:
            vid = m.group(1)
            vurl = f"https://www.twitch.tv/videos/{vid}"
            info = self._vod_info(vurl)
            return [{"id": vid, "url": vurl, "title": info.get("title") or f"Broadcast {vid}",
                     "duration": info.get("duration"), "label": info.get("uploader") or self.channel}]
        count = max(1, int(self.cfg.get("vod_count", 2)))
        listing = f"https://www.twitch.tv/{self.channel}/videos?filter=archives&sort=time"
        self.log(f"Looking up the latest {count} past broadcast(s) for {self.channel}…")
        code, out, err = run_capture([sys.executable, "-m", "yt_dlp", "--flat-playlist", "-J", "--no-warnings",
                                      "--playlist-end", str(count), listing], timeout=180)
        if code != 0 or "{" not in out:
            raise RuntimeError((err or out or "yt-dlp returned nothing")[-300:])
        data = json.loads(out[out.index("{"):])
        vods = []
        for entry in data.get("entries") or []:
            if not entry:
                continue
            blob = f"{entry.get('url') or ''} {entry.get('webpage_url') or ''}"
            m2 = re.search(r"videos/(\d+)", blob) or re.search(r"(\d{6,})", str(entry.get("id", "")))
            if not m2:
                continue
            vid = m2.group(1)
            vods.append({"id": vid, "url": f"https://www.twitch.tv/videos/{vid}",
                         "title": entry.get("title") or f"Broadcast {vid}",
                         "duration": entry.get("duration"), "label": self.channel})
        return vods[:count]

    def _duration_flag(self) -> str:
        """Newer Streamlink renamed --hls-duration to --stream-segmented-duration."""
        if not hasattr(self, "_dur_flag"):
            help_text = run_quiet([sys.executable, "-m", "streamlink", "--help"], timeout=30)[1]
            self._dur_flag = "--stream-segmented-duration" if "--stream-segmented-duration" in help_text else "--hls-duration"
        return self._dur_flag

    def _download_chunk(self, url: str, start: float, length: float, out: Path) -> bool:
        ffmpeg = ffmpeg_path()
        if not ffmpeg:
            return False
        stream_cmd = [sys.executable, "-m", "streamlink", "--loglevel", "warning", "--stdout",
                      "--hls-start-offset", hms(start), self._duration_flag(), hms(length),
                      url, self.cfg.get("quality", "1080p60,1080p,best")]
        ff_cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "warning", "-i", "pipe:0",
                  "-map", "0:v:0", "-map", "0:a?", "-c", "copy", str(out)]
        problems: list[str] = []
        stream = subprocess.Popen(stream_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        ff = subprocess.Popen(ff_cmd, stdin=stream.stdout, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, bufsize=0)
        if stream.stdout:
            stream.stdout.close()
        with self.proc_lock:
            self.stream_proc, self.ffmpeg_proc = stream, ff
        threading.Thread(target=pump_pipe, args=(stream.stderr, problems.append), daemon=True).start()
        threading.Thread(target=pump_pipe, args=(ff.stderr, problems.append), daemon=True).start()
        try:
            while ff.poll() is None:
                if self.stop_event.is_set():
                    break
                time.sleep(0.5)
            if self.stop_event.is_set():
                for p in (stream, ff):
                    if p.poll() is None:
                        p.terminate()
                return False
            if stream.poll() is None:
                try:
                    stream.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    stream.kill()
        finally:
            with self.proc_lock:
                self.stream_proc = None
                self.ffmpeg_proc = None
        ok = out.exists() and out.stat().st_size > 100_000
        if not ok and problems:
            self.log("Download problem: " + " | ".join(problems[-3:])[:400])
        return ok

    def _process_vod(self, vod: dict[str, Any]):
        vid, url = vod["id"], vod["url"]
        st = self.vods.setdefault(vid, {"title": vod.get("title", ""), "next_chunk": 0, "clips": 0, "done": False})
        chunk_len = max(60, int(self.cfg.get("segment_minutes", 5)) * 60)
        max_clips = int(self.cfg.get("max_clips_per_vod", 0))
        total = vod.get("duration")
        if not total:
            total = self._vod_info(url).get("duration")
        label = vod.get("label") or self.channel
        self.log(f"Broadcast {vid}: {vod.get('title', '')[:70]}"
                 + (f" ({hms(total)} long)" if total else ""))
        idx = int(st.get("next_chunk", 0))
        fails = 0
        while not self.stop_event.is_set():
            if max_clips and st["clips"] >= max_clips:
                self.log(f"Reached {max_clips} clips for broadcast {vid}; moving on.")
                st["done"] = True
                break
            start = idx * chunk_len
            if total and start >= total - 18:
                st["done"] = True
                break
            out = RECORDINGS_DIR / f"{self.prefix}_vod{vid}_p{idx:03d}.mkv"
            out.unlink(missing_ok=True)
            self.log(f"Downloading {hms(start)} – {hms(start + chunk_len)} of broadcast {vid}…")
            self.status(f"Downloading broadcast {vid}")
            ok = self._download_chunk(url, start, chunk_len, out)
            if self.stop_event.is_set():
                out.unlink(missing_ok=True)
                break
            if not ok:
                fails += 1
                out.unlink(missing_ok=True)
                if fails >= 2:
                    if idx == 0:
                        self.log(f"Broadcast {vid} could not be downloaded (deleted, expired or subscriber-only?). Skipping it.")
                        st["done"] = True
                        break
                    # A failed chunk near the end usually means the broadcast is over.
                    self.log(f"Chunk {idx} failed twice; treating broadcast {vid} as finished.")
                    st["done"] = True
                    break
                continue
            fails = 0
            self.status(f"Analyzing broadcast {vid}")
            ctx = SourceContext(label=label, tag=f"vod{vid}_{int(start):05d}s", base_offset=start, vod_url=url)
            try:
                made = self.process_segment(out, ctx)
                st["clips"] += made
            except Exception as exc:
                self.log(f"Chunk {idx} of broadcast {vid} failed: {exc}")
            out.unlink(missing_ok=True)
            idx += 1
            st["next_chunk"] = idx
            self.save_state()
        self.save_state()
        self.status("Scanning broadcasts")

    # -------------------------------------------------------------- analysis
    def _load_whisper(self):
        if self.whisper is not None:
            return self.whisper
        model_name = self.cfg.get("whisper_model", "medium")
        device_pref = self.cfg.get("whisper_device", "auto")
        from faster_whisper import WhisperModel
        if device_pref in ("auto", "cuda"):
            self.log(f"Loading Whisper model '{model_name}' on the GPU (the first run downloads it)…")
            try:
                add_cuda_dll_dirs()
                model = WhisperModel(model_name, device="cuda", compute_type="float16")
                # Smoke test: missing CUDA/cuDNN DLLs usually only fail when the model first runs.
                segs, _ = model.transcribe(np.zeros(SAMPLE_RATE * 2, dtype=np.float32), language=self.cfg.get("language", "en"))
                list(segs)
                self.whisper = model
                self.log("Whisper is running on the GPU (CUDA).")
                return model
            except Exception as exc:
                self.log(f"GPU transcription is not available ({str(exc)[:160]}); using CPU instead.")
        self.log(f"Loading Whisper model '{model_name}' on the CPU (int8). The first run downloads it…")
        threads = min(8, max(4, (os.cpu_count() or 4) - 2))
        self.whisper = WhisperModel(model_name, device="cpu", compute_type="int8", cpu_threads=threads)
        return self.whisper

    def _transcribe(self, audio: np.ndarray):
        model = self._load_whisper()
        segments, _info = model.transcribe(audio, language=self.cfg.get("language", "en"), beam_size=3,
                                           vad_filter=True, word_timestamps=True,
                                           condition_on_previous_text=False)
        result = list(segments)
        words: list[tuple[float, float, str]] = []
        for seg in result:
            for w in (getattr(seg, "words", None) or []):
                words.append((float(w.start), float(w.end), str(w.word)))
        return result, words

    def _select_candidates(self, label: str, duration: float, segments: list[Any], energy: list[float]) -> list[ClipCandidate]:
        clip_seconds = max(15, min(60, int(self.cfg.get("clip_seconds", 42))))
        windows = group_windows(segments, duration, clip_seconds, energy)
        if not windows:
            self.log("No usable speech found in this segment; skipping it.")
            return []
        pool = sorted(windows, key=lambda w: heuristic_score(w["text"], w["audio_score"]), reverse=True)[:14]
        ai_results = call_ollama(self.cfg, pool, self.log)
        ai_by_id = {}
        for item in (ai_results or []):
            try:
                ai_by_id[int(item.get("id"))] = item
            except Exception:
                continue
        candidates: list[ClipCandidate] = []
        for w in windows:
            transcript = w["text"]
            fallback = heuristic_score(transcript, w["audio_score"])
            ai = ai_by_id.get(w["id"])
            if ai:
                try:
                    ai_score = max(0, min(100, float(ai.get("score", fallback))))
                except Exception:
                    ai_score = fallback
                score = ai_score * 0.82 + w["audio_score"] * 0.08 + fallback * 0.10
                title = str(ai.get("title", "")).strip()[:65]
                reason = str(ai.get("reason", "AI-ranked highlight")).strip()[:120]
            else:
                score = fallback
                title = ""
                reason = "Transcript and audio activity score"
            if not title:
                title = re.sub(r"\s+", " ", transcript).strip(" .!?-")[:60] or f"{label} Twitch highlight"
                if len(transcript) > 60:
                    title = title.rsplit(" ", 1)[0] + "…"
            candidates.append(ClipCandidate(w["start"], min(duration, w["start"] + clip_seconds),
                                            score, title, reason, transcript))
        candidates.sort(key=lambda c: c.score, reverse=True)
        chosen: list[ClipCandidate] = []
        max_clips = max(1, min(5, int(self.cfg.get("clips_per_segment", 2))))
        for c in candidates:
            if c.score < 38:
                continue
            if any(abs(c.start - old.start) < clip_seconds * 0.62 for old in chosen):
                continue
            chosen.append(c)
            if len(chosen) >= max_clips:
                break
        return chosen

    def _build_render_cmd(self, ffmpeg, source, start, length, ass_name, has_captions, video_args, out) -> list[str]:
        caption = f",ass={ass_name}" if has_captions else ""
        # True 9:16: scale so the height fills 1920, then crop the centre 1080 pixels.
        graph = ("[0:v]scale=1080:1920:force_original_aspect_ratio=increase,"
                 f"crop=1080:1920,setsar=1{caption}[v]")
        return [ffmpeg, "-y", "-hide_banner", "-loglevel", "warning",
                "-ss", f"{start:.2f}", "-i", str(source), "-t", f"{length:.2f}",
                "-filter_complex", graph, "-map", "[v]", "-map", "0:a:0?", *video_args,
                "-pix_fmt", "yuv420p", "-r", "30", "-c:a", "aac", "-b:a", "192k",
                "-af", "loudnorm=I=-16:TP=-1.5:LRA=11", "-movflags", "+faststart", str(out)]

    def _render_clip(self, source: Path, candidate: ClipCandidate, words, ordinal: int,
                     segment_duration: float, ctx: SourceContext) -> Path | None:
        ffmpeg = ffmpeg_path()
        if not ffmpeg:
            return None
        start = max(0.0, min(candidate.start, max(0.0, segment_duration - 1)))
        length = max(12.0, min(60.0, candidate.end - candidate.start))
        title = safe_filename(candidate.title)
        base = f"{safe_filename(ctx.label, 24)}_{ctx.tag}_{ordinal:02d}_{title}"
        out = OUTPUT_DIR / f"{base}.mp4"
        ass = TEMP_DIR / f"{base}.ass"
        meta = OUTPUT_DIR / f"{base}.txt"
        caption_count = make_ass(words, start, min(segment_duration, start + length), ass)
        nvenc_args = ["-c:v", "h264_nvenc", "-preset", "p5", "-cq", "23", "-b:v", "0"]
        cpu_args = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "21"]
        video_args = nvenc_args if self.encoder == "h264_nvenc" else cpu_args
        self.log(f"Rendering: {candidate.title} ({candidate.score:.0f}/100).")
        # Run in TEMP_DIR so the subtitle file can be referenced by a plain file name
        # (avoids Windows drive-letter escaping problems inside FFmpeg filters).
        cmd = self._build_render_cmd(ffmpeg, source, start, length, ass.name, caption_count > 0, video_args, out)
        p = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=1800, cwd=str(TEMP_DIR))
        if p.returncode != 0 and self.encoder == "h264_nvenc":
            self.log("NVENC could not render this clip; retrying with libx264 on CPU…")
            cmd = self._build_render_cmd(ffmpeg, source, start, length, ass.name, caption_count > 0, cpu_args, out)
            p = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=2400, cwd=str(TEMP_DIR))
            if p.returncode == 0:
                self.encoder = "libx264"
                self.log("Switched video rendering to libx264 after the NVENC fallback.")
        if p.returncode != 0 or not out.exists() or out.stat().st_size < 100_000:
            self.log("Clip render failed: " + (p.stderr or "No output from FFmpeg")[-1200:])
            out.unlink(missing_ok=True)
            ass.unlink(missing_ok=True)
            return None
        abs_start = ctx.base_offset + start
        hashtag = re.sub(r"[^A-Za-z0-9]", "", ctx.label) or "Twitch"
        kind = "broadcast" if ctx.vod_url else "livestream"
        description = (f"A moment from {ctx.label}'s Twitch {kind}.\n\n{candidate.reason}\n\n"
                       f"#{hashtag} #Twitch #Gaming #Shorts #StreamClips")
        source_line = f"SOURCE: {source.name}"
        if ctx.vod_url:
            source_line = f"SOURCE: {ctx.vod_url}?t={twitch_time(abs_start)}"
        meta.write_text(
            f"TITLE: {candidate.title}\n\nDESCRIPTION:\n{description}\n\n"
            f"{source_line}\nSOURCE OFFSET: {hms(abs_start)} - {hms(abs_start + length)}\n"
            f"HIGHLIGHT SCORE: {candidate.score:.0f}/100\nREASON: {candidate.reason}\n\nTRANSCRIPT:\n{candidate.transcript}\n",
            encoding="utf-8")
        ass.unlink(missing_ok=True)
        self.log(f"Finished: {out.name}")
        self.clip_history.append({"file": str(out), "title": candidate.title,
                                  "created": datetime.now().isoformat(timespec="seconds")})
        return out

    def process_segment(self, media: Path, ctx: SourceContext) -> int:
        ffmpeg = ffmpeg_path()
        if not ffmpeg:
            raise RuntimeError("FFmpeg not found")
        self.log(f"Analyzing: {media.name}")
        audio = extract_audio(media, ffmpeg, TEMP_DIR / (media.stem + "_audio.wav"))
        if audio is None or len(audio) == 0:
            raise RuntimeError("could not extract audio (FFmpeg failed or the file has no audio track)")
        duration = len(audio) / SAMPLE_RATE
        if duration < 18:
            self.log(f"Recording too short ({duration:.1f}s); skipping.")
            return 0
        segments, words = self._transcribe(audio)
        if not segments:
            self.log("Whisper found no speech in this segment.")
            return 0
        energy = energy_profile(audio)
        candidates = self._select_candidates(ctx.label, duration, segments, energy)
        if not candidates:
            self.log("No highlight passed the selection threshold.")
            return 0
        made = 0
        for idx, candidate in enumerate(candidates, start=1):
            if self._render_clip(media, candidate, words, idx, duration, ctx):
                made += 1
        self.log(f"Processed {media.name}: {made} finished clip(s).")
        self.save_state()
        return made


# ============================================================================
# GUI
# ============================================================================
class AutoclipperApp:
    def __init__(self, root):
        self.root = root
        self.cfg = load_config()
        self.engine: AutoclipperEngine | None = None
        self.log_queue: queue.Queue[str] = queue.Queue()
        self.status_queue: queue.Queue[str] = queue.Queue()
        self.status_var = tk.StringVar(value="Idle")
        self.mode_labels = {"vods": "Past broadcasts (VODs)", "live": "Live streams (record while live)"}
        self.quality_labels = {
            "1080p60,1080p,best": "1080p preferred (fallback to best)",
            "best": "Best available",
            "720p60,720p,best": "720p preferred (fallback to best)",
        }
        self.device_labels = {"auto": "Auto (GPU if possible)", "cpu": "CPU only", "cuda": "GPU (CUDA)"}
        c = self.cfg
        self.channel_var = tk.StringVar(value=c["channel_url"])
        self.mode_var = tk.StringVar(value=self.mode_labels.get(c["source_mode"], self.mode_labels["vods"]))
        self.quality_var = tk.StringVar(value=self.quality_labels.get(c["quality"], self.quality_labels["1080p60,1080p,best"]))
        self.segment_var = tk.StringVar(value=str(c["segment_minutes"]))
        self.clip_var = tk.StringVar(value=str(c["clip_seconds"]))
        self.count_var = tk.StringVar(value=str(c["clips_per_segment"]))
        self.vodcount_var = tk.StringVar(value=str(c["vod_count"]))
        self.maxvod_var = tk.StringVar(value=str(c["max_clips_per_vod"]))
        self.whisper_var = tk.StringVar(value=c["whisper_model"])
        self.device_var = tk.StringVar(value=self.device_labels.get(c["whisper_device"], self.device_labels["auto"]))
        self.ollama_var = tk.StringVar(value=c["ollama_model"])
        self.retention_var = tk.StringVar(value=str(c["retention_hours"]))
        self._build()
        self._refresh_logs()
        self.refresh_clip_list()
        self._periodic_clip_refresh()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _build(self):
        self.root.title(f"{APP_NAME} — Twitch Highlight Studio")
        self.root.geometry("1000x900")
        self.root.minsize(860, 760)
        self.root.configure(bg="#10151e")
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("TFrame", background="#10151e")
        style.configure("Card.TFrame", background="#182231")
        style.configure("TLabel", background="#10151e", foreground="#e8edf5", font=("Segoe UI", 10))
        style.configure("Muted.TLabel", background="#10151e", foreground="#9aa8ba", font=("Segoe UI", 9))
        style.configure("Card.TLabel", background="#182231", foreground="#e8edf5", font=("Segoe UI", 10))
        style.configure("Title.TLabel", background="#10151e", foreground="#ffffff", font=("Segoe UI", 21, "bold"))
        style.configure("TButton", font=("Segoe UI", 10), padding=(12, 7))
        style.configure("Accent.TButton", background="#7c3aed", foreground="#ffffff")
        style.map("Accent.TButton", background=[("active", "#6d28d9")])
        style.configure("Treeview", background="#182231", foreground="#e8edf5", fieldbackground="#182231", rowheight=25)
        style.configure("Treeview.Heading", background="#253247", foreground="#ffffff")

        outer = ttk.Frame(self.root, padding=18)
        outer.pack(fill="both", expand=True)
        header = ttk.Frame(outer)
        header.pack(fill="x")
        ttk.Label(header, text=APP_NAME, style="Title.TLabel").pack(side="left")
        ttk.Label(header, text="LOCAL TWITCH HIGHLIGHT STUDIO", style="Muted.TLabel").pack(side="left", padx=14, pady=(10, 0))
        tk.Label(header, textvariable=self.status_var, bg="#283548", fg="#d8e5f7",
                 font=("Segoe UI", 9, "bold"), padx=12, pady=6).pack(side="right")
        ttk.Label(outer, text="Point it at a channel or a past broadcast, let local AI find the moments, and get upload-ready vertical clips.",
                  style="Muted.TLabel").pack(anchor="w", pady=(4, 14))

        card = ttk.Frame(outer, style="Card.TFrame", padding=14)
        card.pack(fill="x", pady=(0, 12))
        ttk.Label(card, text="SOURCE", style="Card.TLabel", font=("Segoe UI", 9, "bold")).grid(row=0, column=0, sticky="w", columnspan=4, pady=(0, 8))
        self._field(card, "Twitch channel URL, or a single broadcast link (twitch.tv/videos/123…)", self.channel_var, 1, 0, 4, 34)
        self._combo(card, "Mode", self.mode_var, list(self.mode_labels.values()), 3, 0)
        self._combo(card, "Capture quality", self.quality_var, list(self.quality_labels.values()), 3, 1)
        self._combo(card, "Chunk / segment length (min)", self.segment_var, ["3", "5", "8", "10", "15"], 3, 2)
        self._combo(card, "Clip length (seconds)", self.clip_var, ["20", "30", "35", "42", "45", "50", "55", "60"], 3, 3)
        self._combo(card, "Clips per chunk", self.count_var, ["1", "2", "3", "4"], 5, 0)
        self._combo(card, "Past broadcasts to scan", self.vodcount_var, ["1", "2", "3", "5", "10"], 5, 1)
        self._combo(card, "Max clips per broadcast (0 = no limit)", self.maxvod_var, ["0", "5", "8", "12", "20", "40"], 5, 2)

        ai = ttk.Frame(outer, style="Card.TFrame", padding=14)
        ai.pack(fill="x", pady=(0, 12))
        ttk.Label(ai, text="LOCAL AI SETTINGS", style="Card.TLabel", font=("Segoe UI", 9, "bold")).grid(row=0, column=0, sticky="w", columnspan=4, pady=(0, 8))
        self._combo(ai, "Whisper model", self.whisper_var, ["small", "medium", "large-v3-turbo", "large-v3"], 1, 0)
        self._combo(ai, "Whisper device", self.device_var, list(self.device_labels.values()), 1, 1)
        self._field(ai, "Ollama model (optional)", self.ollama_var, 1, 2, 1, 22)
        self._combo(ai, "Keep processed live files (hours)", self.retention_var, ["12", "24", "48", "72", "168"], 1, 3)
        ttk.Label(ai, text="Auto uses your NVIDIA GPU for transcription when the CUDA libraries are installed, otherwise the CPU. "
                           "Ollama is used when running; otherwise a built-in scorer ranks moments.",
                  style="Muted.TLabel").grid(row=3, column=0, columnspan=4, sticky="w", pady=(8, 0))

        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=(0, 12))
        self.start_btn = ttk.Button(actions, text="▶  Start", style="Accent.TButton", command=self.start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(actions, text="■  Stop", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=8)
        ttk.Button(actions, text="Open finished clips", command=lambda: self.open_folder(OUTPUT_DIR)).pack(side="left", padx=(12, 4))
        ttk.Button(actions, text="Open recordings", command=lambda: self.open_folder(RECORDINGS_DIR)).pack(side="left", padx=4)
        ttk.Button(actions, text="Check setup", command=self.check_setup).pack(side="right")

        clips_card = ttk.Frame(outer, style="Card.TFrame", padding=12)
        clips_card.pack(fill="both", expand=True, pady=(0, 12))
        top = ttk.Frame(clips_card, style="Card.TFrame")
        top.pack(fill="x", pady=(0, 7))
        ttk.Label(top, text="READY-TO-UPLOAD CLIPS", style="Card.TLabel", font=("Segoe UI", 9, "bold")).pack(side="left")
        ttk.Button(top, text="Refresh", command=self.refresh_clip_list).pack(side="right")
        self.clip_tree = ttk.Treeview(clips_card, columns=("title", "created", "file"), show="headings", height=5)
        self.clip_tree.heading("title", text="Suggested title")
        self.clip_tree.heading("created", text="Created")
        self.clip_tree.heading("file", text="Video file")
        self.clip_tree.column("title", width=300)
        self.clip_tree.column("created", width=140)
        self.clip_tree.column("file", width=350)
        self.clip_tree.pack(fill="both", expand=True)
        self.clip_tree.bind("<Double-1>", self.open_selected_clip)
        ttk.Label(clips_card, text="Double-click a clip to open it. Each clip has a matching .txt with title, description, hashtags and a link to the source moment.",
                  style="Muted.TLabel").pack(anchor="w", pady=(6, 0))

        log_card = ttk.Frame(outer, style="Card.TFrame", padding=12)
        log_card.pack(fill="both", expand=True)
        ttk.Label(log_card, text="ACTIVITY LOG", style="Card.TLabel", font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(0, 6))
        self.log_text = tk.Text(log_card, height=9, bg="#0c1119", fg="#cbd5e1", insertbackground="white",
                                relief="flat", font=("Consolas", 9), wrap="word")
        self.log_text.pack(fill="both", expand=True)
        self.log_text.configure(state="disabled")

    def _field(self, parent, label, variable, row, col, span=1, width=22):
        ttk.Label(parent, text=label, style="Card.TLabel").grid(row=row, column=col, sticky="w", padx=(0, 12), pady=(0, 4), columnspan=span)
        entry = ttk.Entry(parent, textvariable=variable, width=width)
        entry.grid(row=row + 1, column=col, sticky="ew", padx=(0, 12), pady=(0, 7), columnspan=span)
        for i in range(4):
            parent.columnconfigure(i, weight=1)
        return entry

    def _combo(self, parent, label, variable, values, row, col):
        ttk.Label(parent, text=label, style="Card.TLabel").grid(row=row, column=col, sticky="w", padx=(0, 12), pady=(0, 4))
        combo = ttk.Combobox(parent, textvariable=variable, values=values, state="readonly", width=18)
        combo.grid(row=row + 1, column=col, sticky="ew", padx=(0, 12), pady=(0, 7))
        parent.columnconfigure(col, weight=1)
        return combo

    @staticmethod
    def _key_for(labels: dict[str, str], shown: str, default: str) -> str:
        return next((k for k, v in labels.items() if v == shown), default)

    def _collect_config(self) -> dict[str, Any]:
        url = self.channel_var.get().strip()
        if not URL_RE.match(url):
            raise ValueError("Enter a Twitch channel URL such as https://www.twitch.tv/zerkaa\nor a broadcast link such as https://www.twitch.tv/videos/123456789")
        return {
            "channel_url": url,
            "source_mode": self._key_for(self.mode_labels, self.mode_var.get(), "vods"),
            "quality": self._key_for(self.quality_labels, self.quality_var.get(), "1080p60,1080p,best"),
            "segment_minutes": int(self.segment_var.get()),
            "clip_seconds": int(self.clip_var.get()),
            "clips_per_segment": int(self.count_var.get()),
            "vod_count": int(self.vodcount_var.get()),
            "max_clips_per_vod": int(self.maxvod_var.get()),
            "whisper_model": self.whisper_var.get(),
            "whisper_device": self._key_for(self.device_labels, self.device_var.get(), "auto"),
            "ollama_url": self.cfg.get("ollama_url", DEFAULT_CONFIG["ollama_url"]),
            "ollama_model": self.ollama_var.get().strip() or "qwen3.5:4b",
            "retention_hours": int(self.retention_var.get()),
            "language": "en",
        }

    def log(self, message: str):
        self.log_queue.put(log_line(message))

    def update_status(self, value: str):
        self.status_queue.put(value)

    def start(self):
        if self.engine and not self.engine.stop_event.is_set():
            return
        try:
            cfg = self._collect_config()
        except Exception as exc:
            messagebox.showerror("Check settings", str(exc))
            return
        missing = []
        if not ffmpeg_path():
            missing.append("FFmpeg")
        if run_quiet([sys.executable, "-m", "streamlink", "--version"], timeout=20)[0] != 0:
            missing.append("Streamlink")
        if run_quiet([sys.executable, "-m", "yt_dlp", "--version"], timeout=20)[0] != 0:
            missing.append("yt-dlp")
        if missing:
            messagebox.showerror("Setup incomplete", "Missing: " + ", ".join(missing) + f".\nRun Setup.ps1 and reopen {APP_NAME}.")
            return
        save_config(cfg)
        self.cfg = cfg
        self.engine = AutoclipperEngine(cfg, self.log, self.update_status)
        self.engine.start()
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.log(f"{APP_NAME} started. No social media account is connected; finished videos stay on this PC.")

    def stop(self):
        if self.engine:
            self.engine.stop()
        self.stop_btn.configure(state="disabled")
        self.log("Stop requested. Any chunk already being analyzed will finish first.")

    def check_setup(self):
        lines = [f"Python: {sys.version.split()[0]}",
                 f"FFmpeg: {ffmpeg_path() or 'NOT FOUND'}",
                 f"FFprobe: {ffprobe_path() or 'not found (optional)'}"]
        for label, mod in (("Streamlink", "streamlink"), ("yt-dlp", "yt_dlp")):
            code, out = run_quiet([sys.executable, "-m", mod, "--version"], timeout=25)
            lines.append(f"{label}: {'OK ' + out.strip()[:60] if code == 0 else 'NOT INSTALLED'}")
        try:
            from importlib.metadata import version
            lines.append(f"faster-whisper {version('faster-whisper')}, PyAV {version('av')}, NumPy {version('numpy')}")
        except Exception as exc:
            lines.append(f"faster-whisper / PyAV / NumPy: problem ({exc})")
        try:
            from importlib.metadata import version
            version("nvidia-cudnn-cu12")
            lines.append("NVIDIA CUDA libraries: installed (GPU transcription possible)")
        except Exception:
            lines.append("NVIDIA CUDA libraries: not installed (transcription runs on CPU)")
        try:
            req = Request(self.cfg.get("ollama_url", DEFAULT_CONFIG["ollama_url"]) + "/api/tags")
            with urlopen(req, timeout=3) as response:
                payload = json.loads(response.read().decode("utf-8"))
            models = [m.get("name", "") for m in payload.get("models", [])]
            lines.append("Ollama: running" + (" — " + ", ".join(models[:5]) if models else " — no local models listed"))
        except Exception:
            lines.append("Ollama: not detected (fallback scoring will be used)")
        messagebox.showinfo(f"{APP_NAME} setup check", "\n".join(lines))

    def _refresh_logs(self):
        try:
            while True:
                item = self.log_queue.get_nowait()
                self.log_text.configure(state="normal")
                self.log_text.insert("end", item + "\n")
                self.log_text.see("end")
                self.log_text.configure(state="disabled")
        except queue.Empty:
            pass
        try:
            while True:
                self.status_var.set(self.status_queue.get_nowait())
        except queue.Empty:
            pass
        if self.engine and self.engine.stop_event.is_set():
            cap, proc = self.engine.capture_thread, self.engine.process_thread
            if (cap is None or not cap.is_alive()) and (proc is None or not proc.is_alive()):
                self.start_btn.configure(state="normal")
                self.stop_btn.configure(state="disabled")
        self.root.after(350, self._refresh_logs)

    def refresh_clip_list(self):
        for item in self.clip_tree.get_children():
            self.clip_tree.delete(item)
        clips = list(read_state().get("clips", []))
        known = {c.get("file") for c in clips}
        for file in OUTPUT_DIR.glob("*.mp4"):
            if str(file) not in known:
                clips.append({"file": str(file), "title": file.stem,
                              "created": datetime.fromtimestamp(file.stat().st_mtime).isoformat(timespec="seconds")})
        for c in sorted(clips, key=lambda x: x.get("created", ""), reverse=True)[:100]:
            file = Path(c.get("file", ""))
            if file.exists():
                self.clip_tree.insert("", "end", values=(c.get("title", file.stem), c.get("created", "")[:19], str(file)))

    def open_selected_clip(self, _event=None):
        selected = self.clip_tree.selection()
        if selected:
            path = Path(self.clip_tree.item(selected[0], "values")[2])
            if path.exists():
                os.startfile(str(path))

    def open_folder(self, folder: Path):
        folder.mkdir(parents=True, exist_ok=True)
        os.startfile(str(folder))

    def _running(self) -> bool:
        if not self.engine:
            return False
        cap, proc = self.engine.capture_thread, self.engine.process_thread
        return (cap is not None and cap.is_alive()) or (proc is not None and proc.is_alive())

    def on_close(self):
        if self._running():
            if not messagebox.askyesno(f"Stop {APP_NAME}?", f"{APP_NAME} is running. Stop, finish the current chunk, then close?"):
                return
            if not self.engine.stop_event.is_set():
                self.engine.stop()
            self.log("Closing after a clean stop.")
            self.root.title(f"{APP_NAME} — stopping safely…")
            self._wait_for_shutdown()
            return
        self.root.destroy()

    def _wait_for_shutdown(self):
        if not self._running():
            self.root.destroy()
            return
        self.root.after(700, self._wait_for_shutdown)

    def _periodic_clip_refresh(self):
        self.refresh_clip_list()
        self.root.after(5000, self._periodic_clip_refresh)


def main():
    if tk is None:
        print("tkinter is not available in this Python installation.")
        sys.exit(1)
    root = tk.Tk()
    AutoclipperApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
