from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import base64
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

try:
    import clona_voce_personale as core
    logger.info("clona_voce_personale imported successfully")
except Exception as exc:
    logger.error(f"Failed to import clona_voce_personale: {exc}", exc_info=True)
    core = None

BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent
OUTPUT_DIR = PROJECT_DIR / "output"
API_OUTPUT_DIR = OUTPUT_DIR / "api"
SCRIPT_PATH = BASE_DIR / "clona_voce_personale.py"
MAX_WORKERS = int(os.getenv("CLONAVOCE_MAX_WORKERS", "2"))
JOB_TTL_SECONDS = int(os.getenv("CLONAVOCE_JOB_TTL_SECONDS", "86400"))
API_KEY = os.getenv("CLONAVOCE_API_KEY", "").strip()
REMOTE_XTTS_URL = os.getenv("CLONAVOCE_REMOTE_XTTS_URL", "").strip()
REMOTE_XTTS_KEY = os.getenv("CLONAVOCE_REMOTE_XTTS_KEY", "").strip()
REMOTE_XTTS_TIMEOUT = int(os.getenv("CLONAVOCE_REMOTE_XTTS_TIMEOUT_SECONDS", "180"))

API_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class JobState:
    id: str
    created_at: float
    status: str = "queued"
    profile: str = ""
    display_name: str = ""
    text_preview: str = ""
    text_full: str = ""
    language: str = ""
    audio_format: str = "mp3"
    output_path: str = ""
    started_at: float | None = None
    finished_at: float | None = None
    return_code: int | None = None
    stdout_tail: str = ""
    stderr_tail: str = ""
    error: str = ""


class SynthesizeRequest(BaseModel):
    profile: str = Field(..., min_length=1)
    text: str = Field(..., min_length=1)
    confirmation_token: str = Field(..., min_length=1)
    engine: str = Field(default="auto", pattern="^(auto|pyttsx3|xtts)$")
    language: str = Field(default="it")
    mood: str = Field(default="neutro")
    preset: str = Field(default="professionale")
    accent: str = Field(default="italiano_standard")
    speed: float = Field(default=1.0, ge=0.5, le=2.0)
    pitch: int = Field(default=0, ge=-24, le=24)
    volume: float = Field(default=0.0, ge=-24.0, le=24.0)
    format: str = Field(default="mp3", pattern="^(mp3|wav)$")


class InitProfileRequest(BaseModel):
    profile: str = Field(..., min_length=1)
    display_name: str = Field(..., min_length=1)


class CreateProfileRequest(BaseModel):
    display_name: str = Field(..., min_length=1)


class ProfileDisplayNameUpdateRequest(BaseModel):
    display_name: str = Field(..., min_length=1)


class AddSampleRequest(BaseModel):
    profile: str = Field(..., min_length=1)
    sample_path: str = Field(..., min_length=1)


class ProfileDefaultsUpdateRequest(BaseModel):
    engine: str | None = None
    language: str | None = None
    mood: str | None = None
    preset: str | None = None
    accent: str | None = None
    speed: float | None = Field(default=None, ge=0.5, le=2.0)
    pitch: int | None = Field(default=None, ge=-24, le=24)
    volume: float | None = Field(default=None, ge=-24.0, le=24.0)
    format: str | None = None


app = FastAPI(title="ClonaVoce Service", version="1.0.0")
executor = ThreadPoolExecutor(max_workers=max(1, MAX_WORKERS))
jobs: dict[str, JobState] = {}
jobs_lock = threading.Lock()
transcribe_lock = threading.Lock()
_whisper_models: dict[str, Any] = {}


def _tail_text(text: str, max_chars: int = 6000) -> str:
    text = str(text or "")
    if len(text) <= max_chars:
        return text
    return text[-max_chars:]


def _slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "-", str(value or "").strip().lower()).strip("-") or "profile"


def _preview_text(text: str, max_chars: int = 96) -> str:
    normalized = re.sub(r"\s+", " ", str(text or "").strip())
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max_chars - 1].rstrip() + "…"


def _cleanup_jobs() -> None:
    now = time.time()
    stale_ids: list[str] = []
    with jobs_lock:
        for job_id, state in jobs.items():
            if state.finished_at and (now - state.finished_at) > JOB_TTL_SECONDS:
                stale_ids.append(job_id)
        for job_id in stale_ids:
            state = jobs.pop(job_id, None)
            if not state:
                continue
            try:
                path = Path(state.output_path)
                if path.exists() and path.is_file():
                    path.unlink(missing_ok=True)
            except Exception:
                pass


def _auth(api_key: str | None = Header(default=None, alias="X-API-Key")) -> None:
    if not API_KEY:
        return
    if not api_key or api_key != API_KEY:
        raise HTTPException(status_code=401, detail="API key non valida")


def _job_to_dict(state: JobState) -> dict[str, Any]:
    return {
        "id": state.id,
        "status": state.status,
        "profile": state.profile,
        "display_name": state.display_name,
        "text_preview": state.text_preview,
        "text_full": state.text_full,
        "language": state.language,
        "format": state.audio_format,
        "created_at": state.created_at,
        "started_at": state.started_at,
        "finished_at": state.finished_at,
        "return_code": state.return_code,
        "output_path": state.output_path,
        "download_url": f"/jobs/{state.id}/download" if state.status == "done" and state.output_path else None,
        "stdout_tail": state.stdout_tail,
        "stderr_tail": state.stderr_tail,
        "error": state.error,
    }


def _profile_defaults_template() -> dict[str, Any]:
    return {
        "engine": "auto",
        "language": "it",
        "mood": "neutro",
        "preset": "professionale",
        "accent": "italiano_standard",
        "speed": 1.0,
        "pitch": 0,
        "volume": 0.0,
        "format": "mp3",
    }


def _sanitize_profile_defaults(raw: dict[str, Any] | None) -> dict[str, Any]:
    defaults = _profile_defaults_template()
    if not isinstance(raw, dict):
        return defaults

    engine = str(raw.get("engine") or "").strip().lower()
    if engine in {"auto", "pyttsx3", "xtts"}:
        defaults["engine"] = engine

    language = str(raw.get("language") or "").strip().lower()
    if language:
        defaults["language"] = language

    mood = str(raw.get("mood") or "").strip().lower()
    if mood:
        defaults["mood"] = mood

    preset = str(raw.get("preset") or "").strip().lower()
    if preset:
        defaults["preset"] = preset

    accent = str(raw.get("accent") or "").strip().lower()
    if accent:
        defaults["accent"] = accent

    try:
        speed = float(raw.get("speed"))
        if 0.5 <= speed <= 2.0:
            defaults["speed"] = speed
    except Exception:
        pass

    try:
        pitch = int(raw.get("pitch"))
        if -24 <= pitch <= 24:
            defaults["pitch"] = pitch
    except Exception:
        pass

    try:
        volume = float(raw.get("volume"))
        if -24.0 <= volume <= 24.0:
            defaults["volume"] = volume
    except Exception:
        pass

    audio_format = str(raw.get("format") or "").strip().lower()
    if audio_format in {"mp3", "wav"}:
        defaults["format"] = audio_format

    return defaults


def _generate_profile_id(display_name: str) -> str:
    # Keep profile IDs opaque and stable across display name changes.
    if not core:
        return f"p{secrets.token_hex(8)}"
    for _ in range(64):
        candidate = f"p{secrets.token_hex(8)}"
        try:
            if not core.profile_file(candidate).exists():
                return candidate
        except Exception:
            continue
    return f"p{int(time.time())}{secrets.token_hex(3)}"


def _extract_samples_for_api(profile_data: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in profile_data.get("samples", []) or []:
        if not isinstance(item, dict):
            continue
        filename = str(item.get("filename") or "").strip()
        if not filename:
            continue
        info = item.get("info", {}) if isinstance(item.get("info"), dict) else {}
        out.append(
            {
                "filename": filename,
                "original_filename": str(item.get("original_filename") or "").strip(),
                "added_at": str(item.get("added_at") or "").strip(),
                "duration_seconds": float(info.get("duration_seconds") or 0.0),
            }
        )
    return out


def _validate_profile_token(profile: str, confirmation_token: str) -> None:
    if not core:
        raise RuntimeError("core module non disponibile")
    data = core.load_profile(profile)
    consent = data.get("consent", {}) if isinstance(data, dict) else {}
    if not consent.get("speaker_confirmed"):
        raise PermissionError("Il profilo non ha consenso confermato")
    expected = str(consent.get("confirmation_token") or "").strip()
    if not expected or str(confirmation_token or "").strip() != expected:
        raise PermissionError("Token di conferma non valido per questo profilo")


def _collect_profile_samples_for_remote(profile: str, max_samples: int = 3) -> list[dict[str, str]]:
    if not core:
        return []
    data = core.load_profile(profile)
    profile_path = core.profile_dir(profile)
    references = core.find_reference_samples(data, profile_path)
    selected = references[:max_samples]
    payload_items: list[dict[str, str]] = []
    for item in selected:
        raw = item.read_bytes()
        payload_items.append(
            {
                "filename": item.name,
                "content_b64": base64.b64encode(raw).decode("ascii"),
            }
        )
    return payload_items


def _try_remote_xtts(payload: SynthesizeRequest, output_path: Path) -> tuple[bool, str]:
    if not REMOTE_XTTS_URL:
        return False, "REMOTE_XTTS_URL non configurato"

    samples = _collect_profile_samples_for_remote(payload.profile)
    if not samples:
        return False, "Nessun sample disponibile per il profilo"

    request_payload = {
        "profile": payload.profile,
        "text": payload.text,
        "language": payload.language,
        "mood": payload.mood,
        "preset": payload.preset,
        "accent": payload.accent,
        "speed": payload.speed,
        "pitch": payload.pitch,
        "volume": payload.volume,
        "format": payload.format,
        "samples": samples,
    }
    body = json.dumps(request_payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if REMOTE_XTTS_KEY:
        headers["X-Remote-Key"] = REMOTE_XTTS_KEY

    req = urllib.request.Request(REMOTE_XTTS_URL, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=max(10, REMOTE_XTTS_TIMEOUT)) as resp:
            resp_body = resp.read().decode("utf-8", errors="replace")
            payload_json = json.loads(resp_body)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        return False, f"HTTP {exc.code}: {detail[:800]}"
    except Exception as exc:
        return False, str(exc)

    audio_b64 = str(payload_json.get("audio_b64") or "").strip()
    if not audio_b64:
        return False, "Risposta remota senza audio_b64"

    try:
        raw = base64.b64decode(audio_b64)
    except Exception as exc:
        return False, f"Base64 non valido: {exc}"
    output_path.write_bytes(raw)
    if not output_path.exists() or output_path.stat().st_size == 0:
        return False, "Output remoto vuoto"
    return True, "ok"


def _prepare_uploaded_sample_for_core(source_path: Path) -> Path:
    suffix = source_path.suffix.lower()
    if suffix in {".wav", ".ogg"}:
        return source_path

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise HTTPException(
            status_code=400,
            detail=(
                "Formato sample non supportato senza conversione. "
                "Usa WAV/OGG oppure installa ffmpeg sul server."
            ),
        )

    converted = source_path.with_suffix(".wav")
    result = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-i",
            str(source_path),
            "-ac",
            "1",
            "-ar",
            "24000",
            str(converted),
        ],
        cwd=str(BASE_DIR),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0 or (not converted.exists()):
        raise HTTPException(
            status_code=400,
            detail=f"Conversione sample fallita: {result.stderr.strip() or 'errore ffmpeg'}",
        )
    return converted


def _choose_uploaded_suffix(filename: str, content_type: str) -> str:
    suffix = Path(filename or "").suffix.lower().strip()
    if suffix:
        return suffix

    ctype = str(content_type or "").lower().strip()
    if "mp4" in ctype or "m4a" in ctype:
        return ".m4a"
    if "opus" in ctype:
        return ".opus"
    if "ogg" in ctype:
        return ".ogg"
    if "webm" in ctype:
        return ".webm"
    if "wav" in ctype:
        return ".wav"
    if "mpeg" in ctype or "mp3" in ctype:
        return ".mp3"
    # Default to a compressed container to force ffmpeg probing/conversion.
    return ".webm"


def _transcribe_audio_file(audio_path: Path, language_hint: str = "") -> tuple[str, str, list[str]]:
    errors: list[str] = []
    language = (language_hint or "").strip() or None

    # 1) Try faster-whisper (best effort).
    try:
        from faster_whisper import WhisperModel  # type: ignore

        model_name = os.getenv("CLONAVOCE_TRANSCRIBE_MODEL", "base")
        with transcribe_lock:
            model = _whisper_models.get(f"fw::{model_name}")
            if model is None:
                model = WhisperModel(model_name, compute_type=os.getenv("CLONAVOCE_TRANSCRIBE_COMPUTE", "int8"))
                _whisper_models[f"fw::{model_name}"] = model
        segments, _info = model.transcribe(str(audio_path), language=language, vad_filter=True)
        text = " ".join((seg.text or "").strip() for seg in segments).strip()
        if text:
            return text, f"faster-whisper:{model_name}", errors
        errors.append("faster-whisper: testo vuoto")
    except Exception as exc:
        errors.append(f"faster-whisper: {exc}")

    # 2) Fallback: openai-whisper package.
    try:
        import whisper  # type: ignore

        model_name = os.getenv("CLONAVOCE_TRANSCRIBE_MODEL", "base")
        with transcribe_lock:
            model = _whisper_models.get(f"ow::{model_name}")
            if model is None:
                model = whisper.load_model(model_name)
                _whisper_models[f"ow::{model_name}"] = model
        result = model.transcribe(str(audio_path), language=language, fp16=False)
        text = str((result or {}).get("text", "")).strip()
        if text:
            return text, f"openai-whisper:{model_name}", errors
        errors.append("openai-whisper: testo vuoto")
    except Exception as exc:
        errors.append(f"openai-whisper: {exc}")

    return "", "", errors


def _run_synthesize_job(job_id: str, payload: SynthesizeRequest) -> None:
    with jobs_lock:
        state = jobs[job_id]
        state.status = "running"
        state.started_at = time.time()

    safe_profile = _slug(payload.profile)
    output_ext = payload.format.lower()
    output_path = API_OUTPUT_DIR / f"{safe_profile}_{job_id}.{output_ext}"

    try_remote = bool(REMOTE_XTTS_URL) and payload.engine in {"auto", "xtts"}
    remote_attempted = False
    remote_error = ""
    allow_local_xtts_fallback_after_remote = os.getenv(
        "CLONAVOCE_ALLOW_LOCAL_XTTS_FALLBACK_AFTER_REMOTE", "0"
    ).strip() == "1"
    if try_remote:
        remote_attempted = True
        try:
            _validate_profile_token(payload.profile, payload.confirmation_token)
            ok, msg = _try_remote_xtts(payload, output_path)
            if ok:
                with jobs_lock:
                    state = jobs[job_id]
                    state.return_code = 0
                    state.stdout_tail = ""
                    state.stderr_tail = "[remote-xtts] sintesi completata via endpoint remoto"
                    state.finished_at = time.time()
                    state.status = "done"
                    state.output_path = str(output_path)
                return
            remote_error = str(msg or "errore remoto sconosciuto")
            logger.warning("Remote XTTS fallita, fallback locale: %s", msg)
        except Exception as exc:
            remote_error = str(exc)
            logger.warning("Remote XTTS non disponibile, fallback locale: %s", exc)

    # Per evitare falsi negativi "TTS mancante" su backend Render, di default NON
    # facciamo fallback locale quando il remoto e' configurato ma fallisce.
    if remote_attempted and remote_error and not allow_local_xtts_fallback_after_remote:
        with jobs_lock:
            state = jobs[job_id]
            state.finished_at = time.time()
            state.status = "failed"
            state.error = f"Sintesi XTTS remota non riuscita: {remote_error}"
        return

    cmd = [
        sys.executable,
        str(SCRIPT_PATH),
        "synthesize",
        "--profile",
        payload.profile,
        "--text",
        payload.text,
        "--engine",
        payload.engine,
        "--language",
        payload.language,
        "--mood",
        payload.mood,
        "--preset",
        payload.preset,
        "--accent",
        payload.accent,
        "--speed",
        str(payload.speed),
        "--pitch",
        str(payload.pitch),
        "--volume",
        str(payload.volume),
        "--confirmation-token",
        payload.confirmation_token,
        "--out",
        str(output_path),
    ]

    try:
        completed = subprocess.run(
            cmd,
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )

        # Optional fallback: only enabled explicitly to avoid surprising low-quality voice output.
        allow_low_quality_fallback = os.getenv("CLONAVOCE_ALLOW_LOW_QUALITY_FALLBACK", "0").strip() == "1"
        combined_err = f"{completed.stdout}\n{completed.stderr}".lower()
        needs_tts_fallback = (
            allow_low_quality_fallback
            and
            completed.returncode != 0
            and "no module named 'tts'" in combined_err
            and payload.engine in {"auto", "xtts"}
        )
        if needs_tts_fallback:
            fallback_cmd = list(cmd)
            try:
                engine_idx = fallback_cmd.index("--engine") + 1
                fallback_cmd[engine_idx] = "pyttsx3"
            except Exception:
                pass
            fallback = subprocess.run(
                fallback_cmd,
                cwd=str(BASE_DIR),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            if fallback.returncode == 0 and output_path.exists():
                completed = fallback
                completed.stderr = (completed.stderr or "") + "\n[fallback] XTTS non disponibile, usato pyttsx3"

        with jobs_lock:
            state = jobs[job_id]
            state.return_code = int(completed.returncode)
            state.stdout_tail = _tail_text(completed.stdout)
            state.stderr_tail = _tail_text(completed.stderr)
            state.finished_at = time.time()
            if completed.returncode == 0 and output_path.exists():
                state.status = "done"
                state.output_path = str(output_path)
            else:
                state.status = "failed"
                low = f"{completed.stdout}\n{completed.stderr}".lower()
                if "no module named 'tts'" in low:
                    if remote_attempted and remote_error:
                        state.error = f"Sintesi XTTS remota non riuscita: {remote_error}"
                        return
                    state.error = "Sintesi XTTS non disponibile sul server (modulo TTS mancante). Qualita alta non disponibile finche XTTS non viene installato."
                    return
                stderr_snippet = _tail_text(completed.stderr, 800).strip()
                state.error = f"Sintesi fallita (rc={completed.returncode}){': ' + stderr_snippet if stderr_snippet else ''}"
    except Exception as exc:
        with jobs_lock:
            state = jobs[job_id]
            state.finished_at = time.time()
            state.status = "failed"
            state.error = f"Eccezione runtime: {exc}"


@app.on_event("startup")
def _startup() -> None:
    logger.info("Starting ClonaVoce API service...")
    try:
        if core:
            core.ensure_dirs()
            logger.info("Directories ensured")
        else:
            logger.warning("core module not available, skipping ensure_dirs")
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        API_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        logger.info(f"Output directories ready: {API_OUTPUT_DIR}")
        logger.info(f"API Key configured: {bool(API_KEY)}")
        logger.info("Startup complete")
    except Exception as exc:
        logger.error(f"Startup failed: {exc}", exc_info=True)


@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, "service": "clonavoce"}


@app.get("/health/private")
def health_private(_: None = Depends(_auth)) -> dict[str, Any]:
    _cleanup_jobs()
    return {
        "ok": True,
        "service": "clonavoce",
        "workers": MAX_WORKERS,
        "jobs": len(jobs),
        "remote_xtts_configured": bool(REMOTE_XTTS_URL),
        "remote_xtts_timeout_seconds": REMOTE_XTTS_TIMEOUT,
        "remote_xtts_url_preview": REMOTE_XTTS_URL[:120] if REMOTE_XTTS_URL else "",
    }


@app.get("/profiles")
def list_profiles() -> dict[str, Any]:
    if not core:
        raise HTTPException(status_code=503, detail="core module not available")
    _cleanup_jobs()
    items: list[dict[str, Any]] = []
    for name in core.list_profiles():
        try:
            data = core.load_profile(name)
        except Exception as exc:
            logger.warning(f"Failed to load profile {name}: {exc}")
            continue
        consent = data.get("consent", {}) if isinstance(data, dict) else {}
        items.append(
            {
                "profile": data.get("profile", name),
                "display_name": data.get("display_name", name),
                "speaker_confirmed": bool(consent.get("speaker_confirmed")),
                "sample_count": len(data.get("samples", [])),
                "samples": _extract_samples_for_api(data),
                "defaults": _sanitize_profile_defaults(data.get("defaults")),
            }
        )
    return {"profiles": items}


@app.post("/profiles/create")
def create_profile(payload: CreateProfileRequest) -> dict[str, Any]:
    if not core:
        raise HTTPException(status_code=503, detail="core module not available")
    display_name = str(payload.display_name or "").strip()
    if not display_name:
        raise HTTPException(status_code=400, detail="display_name obbligatorio")

    profile_id = _generate_profile_id(display_name)
    try:
        args = core.argparse.Namespace(
            profile=profile_id,
            display_name=display_name,
            i_am_the_speaker=True,
        )
        code = core.command_init_profile(args)
        if code != 0:
            raise HTTPException(status_code=400, detail="Impossibile creare il profilo")
        data = core.load_profile(profile_id)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("create_profile failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Errore interno: {exc}")

    return {
        "created": True,
        "profile": data.get("profile", profile_id),
        "display_name": data.get("display_name", display_name),
        "confirmation_token": (data.get("consent", {}) or {}).get("confirmation_token"),
    }


@app.get("/profiles/{profile}")
def profile_detail(profile: str) -> dict[str, Any]:
    if not core:
        raise HTTPException(status_code=503, detail="core module not available")
    profile_clean = _slug(profile)
    try:
        data = core.load_profile(profile_clean)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Profilo non trovato")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    consent = data.get("consent", {}) if isinstance(data, dict) else {}
    return {
        "profile": data.get("profile", profile_clean),
        "display_name": data.get("display_name", profile_clean),
        "speaker_confirmed": bool(consent.get("speaker_confirmed")),
        "sample_count": len(data.get("samples", [])),
        "samples": _extract_samples_for_api(data),
        "defaults": _sanitize_profile_defaults(data.get("defaults")),
    }


@app.post("/profiles/{profile}/defaults")
def update_profile_defaults(profile: str, payload: ProfileDefaultsUpdateRequest) -> dict[str, Any]:
    if not core:
        raise HTTPException(status_code=503, detail="core module not available")
    profile_clean = _slug(profile)
    try:
        data = core.load_profile(profile_clean)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Profilo non trovato")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    current = _sanitize_profile_defaults(data.get("defaults"))
    incoming = payload.model_dump(exclude_none=True)
    merged = _sanitize_profile_defaults({**current, **incoming})
    data["defaults"] = merged
    try:
        core.save_profile(profile_clean, data)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Salvataggio default fallito: {exc}")

    return {
        "updated": True,
        "profile": profile_clean,
        "defaults": merged,
    }


@app.post("/profiles/{profile}/display-name")
def update_profile_display_name(profile: str, payload: ProfileDisplayNameUpdateRequest) -> dict[str, Any]:
    if not core:
        raise HTTPException(status_code=503, detail="core module not available")
    profile_clean = _slug(profile)
    display_name = str(payload.display_name or "").strip()
    if not display_name:
        raise HTTPException(status_code=400, detail="display_name obbligatorio")

    try:
        data = core.load_profile(profile_clean)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Profilo non trovato")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    data["display_name"] = display_name
    try:
        core.save_profile(profile_clean, data)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Impossibile aggiornare nome profilo: {exc}")

    return {
        "updated": True,
        "profile": profile_clean,
        "display_name": display_name,
    }


@app.delete("/profiles/{profile}")
def delete_profile(profile: str) -> dict[str, Any]:
    if not core:
        raise HTTPException(status_code=503, detail="core module not available")
    profile_clean = _slug(profile)

    try:
        core.load_profile(profile_clean)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Profilo non trovato")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    profile_path = core.profile_dir(profile_clean)
    if profile_path.exists() and profile_path.is_dir():
        try:
            shutil.rmtree(profile_path)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Impossibile eliminare profilo: {exc}")

    removed_job_ids: list[str] = []
    with jobs_lock:
        to_remove = [jid for jid, state in jobs.items() if state.profile == profile_clean]
        for jid in to_remove:
            state = jobs.pop(jid, None)
            if not state:
                continue
            removed_job_ids.append(jid)
            path = Path(state.output_path) if state.output_path else None
            if path and path.exists() and path.is_file():
                try:
                    path.unlink(missing_ok=True)
                except Exception:
                    pass

    return {
        "deleted": True,
        "profile": profile_clean,
        "jobs_deleted": len(removed_job_ids),
    }


@app.delete("/profiles/{profile}/samples/{sample_filename}")
def delete_profile_sample(profile: str, sample_filename: str) -> dict[str, Any]:
    if not core:
        raise HTTPException(status_code=503, detail="core module not available")
    profile_clean = _slug(profile)
    sample_name = Path(str(sample_filename or "")).name.strip()
    if not sample_name:
        raise HTTPException(status_code=400, detail="Nome campione non valido")

    try:
        data_before = core.load_profile(profile_clean)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Profilo non trovato")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    samples_before = data_before.get("samples", []) if isinstance(data_before, dict) else []
    exists = any(str(item.get("filename") or "") == sample_name for item in samples_before if isinstance(item, dict))
    if not exists:
        raise HTTPException(status_code=404, detail="Campione non trovato")

    if not hasattr(core, "command_remove_sample"):
        raise HTTPException(status_code=501, detail="Rimozione campioni non supportata da questo backend")

    try:
        args = core.argparse.Namespace(
            profile=profile_clean,
            sample=[sample_name],
            all=False,
            keep_files=False,
        )
        code = core.command_remove_sample(args)
        if code != 0:
            raise HTTPException(status_code=400, detail="Impossibile rimuovere campione")
        data_after = core.load_profile(profile_clean)
    except HTTPException:
        raise
    except (ValueError, RuntimeError, FileNotFoundError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.error("delete_profile_sample failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Errore interno durante eliminazione campione")

    return {
        "deleted": True,
        "profile": profile_clean,
        "sample": sample_name,
        "sample_count": len(data_after.get("samples", [])),
        "samples": _extract_samples_for_api(data_after),
    }


@app.post("/profiles/init")
def init_profile(payload: InitProfileRequest) -> dict[str, Any]:
    if not core:
        raise HTTPException(status_code=503, detail="core module not available")
    try:
        args = core.argparse.Namespace(
            profile=payload.profile,
            display_name=payload.display_name,
            i_am_the_speaker=True,
        )
        code = core.command_init_profile(args)
        if code != 0:
            logger.error(f"command_init_profile failed with code {code} for profile {payload.profile}")
            raise HTTPException(status_code=400, detail="Impossibile creare il profilo")
        data = core.load_profile(payload.profile)
        logger.info(f"Profile {payload.profile} created successfully")
        return {
            "created": True,
            "profile": data.get("profile", payload.profile),
            "display_name": data.get("display_name", payload.display_name),
            "confirmation_token": (data.get("consent", {}) or {}).get("confirmation_token"),
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"init_profile failed: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Errore interno: {exc}")


@app.post("/profiles/add-sample")
def add_sample(payload: AddSampleRequest) -> dict[str, Any]:
    sample_path = Path(payload.sample_path).expanduser().resolve()
    if not sample_path.exists() or not sample_path.is_file():
        raise HTTPException(status_code=400, detail="sample_path non valido")
    try:
        args = core.argparse.Namespace(profile=payload.profile, wav=str(sample_path))
        code = core.command_add_sample(args)
        if code != 0:
            raise HTTPException(status_code=400, detail="Impossibile aggiungere campione")
        data = core.load_profile(payload.profile)
        return {
            "added": True,
            "profile": data.get("profile", payload.profile),
            "sample_count": len(data.get("samples", [])),
        }
    except HTTPException:
        raise
    except (ValueError, RuntimeError, FileNotFoundError) as exc:
        # Validation/import failures from core are client-correctable and should not be 500.
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.error(f"add_sample failed: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail="Errore interno durante aggiunta campione")


@app.post("/profiles/add-sample-upload")
async def add_sample_upload(
    profile: str = Form(...),
    sample: UploadFile = File(...),
    auto_transcribe: bool = Form(True),
    language_hint: str = Form("it"),
) -> dict[str, Any]:
    if not core:
        raise HTTPException(status_code=503, detail="core module not available")

    profile_clean = str(profile or "").strip()
    if not profile_clean:
        raise HTTPException(status_code=400, detail="Profilo non valido")

    suffix = _choose_uploaded_suffix(sample.filename or "", sample.content_type or "")

    with tempfile.TemporaryDirectory(prefix="clonavoce_upload_") as tmp_dir:
        uploaded_path = Path(tmp_dir) / f"mobile_sample{suffix}"
        content = await sample.read()
        if not content:
            raise HTTPException(status_code=400, detail="File sample vuoto")
        uploaded_path.write_bytes(content)

        try:
            prepared_path = _prepare_uploaded_sample_for_core(uploaded_path)
            args = core.argparse.Namespace(profile=profile_clean, wav=str(prepared_path))
            code = core.command_add_sample(args)
            if code != 0:
                raise HTTPException(status_code=400, detail="Impossibile aggiungere campione")

            data = core.load_profile(profile_clean)
            result: dict[str, Any] = {
                "added": True,
                "profile": data.get("profile", profile_clean),
                "sample_count": len(data.get("samples", [])),
            }

            if auto_transcribe:
                text, engine, errors = _transcribe_audio_file(prepared_path, language_hint=language_hint)
                result["transcription_text"] = text
                result["transcription_engine"] = engine
                result["transcription_errors"] = errors

            return result
        except HTTPException:
            raise
        except (ValueError, RuntimeError, FileNotFoundError) as exc:
            # Typical input/import constraints from core should be surfaced as 400.
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            logger.error(
                "add_sample_upload failed for profile=%s filename=%s content_type=%s: %s",
                profile_clean,
                sample.filename,
                sample.content_type,
                exc,
                exc_info=True,
            )
            raise HTTPException(status_code=500, detail="Errore interno durante upload sample")


@app.post("/synthesize")
def synthesize(payload: SynthesizeRequest) -> dict[str, Any]:
    _cleanup_jobs()
    job_id = secrets.token_hex(12)
    display_name = payload.profile
    if core:
        try:
            data = core.load_profile(payload.profile)
            display_name = str(data.get("display_name") or payload.profile)
        except Exception:
            display_name = payload.profile
    state = JobState(
        id=job_id,
        created_at=time.time(),
        status="queued",
        profile=payload.profile,
        display_name=display_name,
        text_preview=_preview_text(payload.text),
        text_full=str(payload.text or "").strip(),
        language=str(payload.language or "").strip(),
        audio_format=str(payload.format or "mp3").strip().lower() or "mp3",
    )
    with jobs_lock:
        jobs[job_id] = state
    executor.submit(_run_synthesize_job, job_id, payload)
    return {
        "accepted": True,
        "job_id": job_id,
        "status_url": f"/jobs/{job_id}",
        "download_url": f"/jobs/{job_id}/download",
    }


@app.get("/jobs/{job_id}")
def job_status(job_id: str) -> dict[str, Any]:
    _cleanup_jobs()
    with jobs_lock:
        state = jobs.get(job_id)
        if not state:
            raise HTTPException(status_code=404, detail="Job non trovato")
        return _job_to_dict(state)


@app.get("/jobs/{job_id}/download")
def job_download(job_id: str) -> FileResponse:
    _cleanup_jobs()
    with jobs_lock:
        state = jobs.get(job_id)
        if not state:
            raise HTTPException(status_code=404, detail="Job non trovato")
        if state.status != "done" or not state.output_path:
            raise HTTPException(status_code=409, detail="Output non pronto")
        path = Path(state.output_path)
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="File output mancante")
    media_type = "audio/mpeg" if path.suffix.lower() == ".mp3" else "audio/wav"
    return FileResponse(str(path), media_type=media_type, filename=path.name)


@app.get("/jobs")
def list_jobs() -> dict[str, Any]:
    _cleanup_jobs()
    with jobs_lock:
        items = [_job_to_dict(state) for state in sorted(jobs.values(), key=lambda x: x.created_at, reverse=True)]
    return {"jobs": items}


@app.delete("/jobs/{job_id}")
def delete_job(job_id: str) -> dict[str, Any]:
    _cleanup_jobs()
    with jobs_lock:
        state = jobs.pop(job_id, None)
    if not state:
        raise HTTPException(status_code=404, detail="Job non trovato")

    deleted_file = False
    try:
        if state.output_path:
            path = Path(state.output_path)
            if path.exists() and path.is_file():
                path.unlink(missing_ok=True)
                deleted_file = True
    except Exception:
        pass

    return {"deleted": True, "job_id": job_id, "output_deleted": deleted_file}


@app.delete("/jobs")
def delete_all_jobs() -> dict[str, Any]:
    _cleanup_jobs()
    with jobs_lock:
        states = list(jobs.values())
        jobs.clear()

    deleted_files = 0
    for state in states:
        try:
            if state.output_path:
                path = Path(state.output_path)
                if path.exists() and path.is_file():
                    path.unlink(missing_ok=True)
                    deleted_files += 1
        except Exception:
            pass

    return {
        "deleted": True,
        "jobs_deleted": len(states),
        "output_files_deleted": deleted_files,
    }
