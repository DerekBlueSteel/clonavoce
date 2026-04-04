from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

import clona_voce_personale as core

REMOTE_KEY = os.getenv("CLONAVOCE_REMOTE_XTTS_KEY", "").strip()
MAX_SAMPLES = int(os.getenv("CLONAVOCE_REMOTE_MAX_SAMPLES", "4"))
# Quanti secondi tenere i job completati in memoria prima di scartarli
JOB_TTL_SECONDS = int(os.getenv("CLONAVOCE_REMOTE_JOB_TTL_SECONDS", "600"))

# Job store in-memory per sintesi async
_remote_jobs: dict[str, dict[str, Any]] = {}
_remote_jobs_lock = threading.Lock()

# Serializza le sintesi: GPU e dir-globals sono risorse condivise
_synthesis_lock = threading.Lock()


class SampleItem(BaseModel):
    filename: str = Field(..., min_length=1)
    content_b64: str = Field(..., min_length=1)


class RemoteSynthesizeRequest(BaseModel):
    profile: str = Field(..., min_length=1)
    text: str = Field(..., min_length=1)
    language: str = Field(default="it")
    mood: str = Field(default="neutro")
    preset: str = Field(default="professionale")
    accent: str = Field(default="italiano_standard")
    speed: float = Field(default=1.0, ge=0.5, le=2.0)
    pitch: int = Field(default=0, ge=-24, le=24)
    volume: float = Field(default=0.0, ge=-24.0, le=24.0)
    format: str = Field(default="mp3", pattern="^(mp3|wav)$")
    samples: list[SampleItem] = Field(default_factory=list)


app = FastAPI(title="ClonaVoce Remote XTTS", version="1.0.0")


def _check_key(x_remote_key: str | None) -> None:
    if not REMOTE_KEY:
        return
    if not x_remote_key or x_remote_key.strip() != REMOTE_KEY:
        raise HTTPException(status_code=401, detail="Remote key non valida")


def _safe_max(value: int, minimum: int, maximum: int) -> int:
    try:
        num = int(value)
    except Exception:
        num = minimum
    return max(minimum, min(maximum, num))


def _cleanup_old_jobs() -> None:
    """Rimuove job scaduti e le loro directory temporanee."""
    now = time.time()
    to_delete: list[str] = []
    with _remote_jobs_lock:
        for job_id, job in _remote_jobs.items():
            if (now - float(job.get("created_at", 0))) > JOB_TTL_SECONDS:
                to_delete.append(job_id)
        for job_id in to_delete:
            job = _remote_jobs.pop(job_id, None)
            if job and job.get("tmp_dir"):
                shutil.rmtree(job["tmp_dir"], ignore_errors=True)


def _do_synthesis(job_id: str, payload: RemoteSynthesizeRequest, tmp: Path) -> None:
    """Esegue la sintesi nel thread di background. Tiene _synthesis_lock già acquisito."""
    samples = payload.samples[: max(1, MAX_SAMPLES)]
    profile = core.slugify(payload.profile)

    profiles_dir = tmp / "profiles"
    output_dir = tmp / "output"
    profile_dir_path = profiles_dir / profile
    samples_dir = profile_dir_path / "samples"
    samples_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    old_profiles_dir = core.PROFILES_DIR
    old_output_dir = core.OUTPUT_DIR
    try:
        core.PROFILES_DIR = profiles_dir
        core.OUTPUT_DIR = output_dir

        init_args = argparse.Namespace(profile=profile, display_name=profile, i_am_the_speaker=True)
        core.command_init_profile(init_args)

        for index, item in enumerate(samples, start=1):
            raw = base64.b64decode(item.content_b64)
            ext = Path(item.filename).suffix.lower() or ".wav"
            if ext not in {".wav", ".ogg"}:
                ext = ".wav"
            sample_path = tmp / f"incoming_{index:02d}{ext}"
            sample_path.write_bytes(raw)
            add_args = argparse.Namespace(profile=profile, wav=str(sample_path))
            core.command_add_sample(add_args)

        profile_data = core.load_profile(profile)
        token = str((profile_data.get("consent", {}) or {}).get("confirmation_token", "")).strip()
        if not token:
            raise RuntimeError("Token profilo non disponibile")

        out_path = tmp / f"remote_out.{payload.format}"
        synth_args = argparse.Namespace(
            profile=profile,
            text=payload.text,
            text_file=None,
            out=str(out_path),
            format=payload.format,
            engine="xtts",
            language=payload.language,
            mood=payload.mood,
            preset=payload.preset,
            accent=payload.accent,
            speed=payload.speed,
            pitch=payload.pitch,
            volume=payload.volume,
            confirmation_token=token,
            progress_callback=None,
        )
        code = core.command_synthesize(synth_args)
        if code != 0 or not out_path.exists():
            raise RuntimeError("Sintesi XTTS non ha prodotto output")

        audio_b64 = base64.b64encode(out_path.read_bytes()).decode("ascii")
        with _remote_jobs_lock:
            job = _remote_jobs.get(job_id)
            if job:
                job["status"] = "done"
                job["audio_b64"] = audio_b64
                job["format"] = payload.format
                job["finished_at"] = time.time()
    finally:
        core.PROFILES_DIR = old_profiles_dir
        core.OUTPUT_DIR = old_output_dir


def _run_remote_synthesis(job_id: str, payload: RemoteSynthesizeRequest) -> None:
    """Entry point thread di background per ogni job di sintesi."""
    with _remote_jobs_lock:
        job = _remote_jobs.get(job_id)
        if job is None:
            return
        job["status"] = "running"

    tmp_dir_path = tempfile.mkdtemp(prefix="clonavoce_rxtts_")
    with _remote_jobs_lock:
        if job_id in _remote_jobs:
            _remote_jobs[job_id]["tmp_dir"] = tmp_dir_path

    try:
        # _synthesis_lock serializza accesso a GPU e module-globals core.PROFILES_DIR
        with _synthesis_lock:
            _do_synthesis(job_id, payload, Path(tmp_dir_path))
    except Exception as exc:
        with _remote_jobs_lock:
            job = _remote_jobs.get(job_id)
            if job:
                job["status"] = "failed"
                job["error"] = str(exc)
                job["finished_at"] = time.time()


def _collect_profile_samples_export(profile_data: dict[str, Any], pdir: Path, max_samples: int, max_sample_bytes: int) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    samples_dir = pdir / "samples"

    ordered_names: list[str] = []
    for item in profile_data.get("samples", []) if isinstance(profile_data, dict) else []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("filename") or "").strip()
        if not name:
            continue
        basename = Path(name).name
        if basename in seen:
            continue
        seen.add(basename)
        ordered_names.append(basename)

    if samples_dir.exists():
        for sample_file in sorted(samples_dir.iterdir()):
            if not sample_file.is_file():
                continue
            if sample_file.name in seen:
                continue
            seen.add(sample_file.name)
            ordered_names.append(sample_file.name)

    for name in ordered_names[:max_samples]:
        sample_path = samples_dir / name
        if not sample_path.exists() or not sample_path.is_file():
            continue
        if sample_path.stat().st_size > max_sample_bytes:
            continue
        raw = sample_path.read_bytes()
        out.append(
            {
                "filename": sample_path.name,
                "content_b64": base64.b64encode(raw).decode("ascii"),
            }
        )
    return out


@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, "async_jobs": True}


@app.get("/profiles/export")
def export_profiles(
    max_profiles: int = 30,
    max_samples_per_profile: int = 8,
    max_sample_mb: int = 12,
    x_remote_key: str | None = Header(default=None, alias="X-Remote-Key"),
) -> dict[str, Any]:
    _check_key(x_remote_key)
    core.ensure_dirs()

    max_profiles = _safe_max(max_profiles, 1, 200)
    max_samples_per_profile = _safe_max(max_samples_per_profile, 1, 30)
    max_sample_bytes = _safe_max(max_sample_mb, 1, 50) * 1024 * 1024

    names = core.list_profiles()[:max_profiles]
    profiles: list[dict[str, Any]] = []
    for name in names:
        try:
            data = core.load_profile(name)
        except Exception:
            continue
        consent = data.get("consent", {}) if isinstance(data, dict) else {}
        pdir = core.profile_dir(name)
        samples = _collect_profile_samples_export(data, pdir, max_samples_per_profile, max_sample_bytes)
        profiles.append(
            {
                "profile": str(data.get("profile") or name),
                "display_name": str(data.get("display_name") or name),
                "speaker_confirmed": bool(consent.get("speaker_confirmed", True)),
                "confirmation_token": str(consent.get("confirmation_token") or ""),
                "defaults": data.get("defaults", {}) if isinstance(data.get("defaults"), dict) else {},
                "samples": samples,
            }
        )

    return {
        "profiles": profiles,
        "count": len(profiles),
    }


@app.post("/synthesize")
def synthesize(payload: RemoteSynthesizeRequest, x_remote_key: str | None = Header(default=None, alias="X-Remote-Key")) -> dict[str, str]:
    _check_key(x_remote_key)
    _cleanup_old_jobs()

    if not payload.samples:
        raise HTTPException(status_code=400, detail="Nessun sample inviato")

    job_id = secrets.token_hex(12)
    with _remote_jobs_lock:
        _remote_jobs[job_id] = {
            "id": job_id,
            "status": "queued",
            "created_at": time.time(),
            "finished_at": None,
            "audio_b64": "",
            "format": payload.format,
            "error": "",
            "tmp_dir": "",
        }

    t = threading.Thread(target=_run_remote_synthesis, args=(job_id, payload), daemon=True)
    t.start()

    return {"job_id": job_id, "async": "true", "status": "queued"}


@app.get("/jobs/{job_id}")
def get_remote_job(job_id: str, x_remote_key: str | None = Header(default=None, alias="X-Remote-Key")) -> dict[str, Any]:
    _check_key(x_remote_key)
    with _remote_jobs_lock:
        job = dict(_remote_jobs.get(job_id) or {})
    if not job:
        raise HTTPException(status_code=404, detail="Job non trovato")
    result: dict[str, Any] = {
        "job_id": job["id"],
        "status": job["status"],
        "error": job.get("error", ""),
    }
    if job["status"] == "done":
        result["audio_b64"] = job["audio_b64"]
        result["format"] = job["format"]
    return result


if __name__ == "__main__":
    import uvicorn

    host = os.getenv("CLONAVOCE_REMOTE_HOST", "127.0.0.1")
    port = int(os.getenv("CLONAVOCE_REMOTE_PORT", "8010"))
    uvicorn.run(app, host=host, port=port)
