from __future__ import annotations

import argparse
import base64
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

import clona_voce_personale as core

REMOTE_KEY = os.getenv("CLONAVOCE_REMOTE_XTTS_KEY", "").strip()
MAX_SAMPLES = int(os.getenv("CLONAVOCE_REMOTE_MAX_SAMPLES", "4"))


class SampleItem(BaseModel):
    filename: str = Field(..., min_length=1)
    content_b64: str = Field(..., min_length=1)


class RemoteSynthesizeRequest(BaseModel):
    profile: str = Field(..., min_length=1)
    text: str = Field(..., min_length=1)
    language: str = Field(default="it")
    mood: str = Field(default="neutro")
    speed: float = Field(default=1.0, ge=0.5, le=2.0)
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
def health() -> dict[str, bool]:
    return {"ok": True}


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

    if not payload.samples:
        raise HTTPException(status_code=400, detail="Nessun sample inviato")

    samples = payload.samples[: max(1, MAX_SAMPLES)]
    profile = core.slugify(payload.profile)

    with tempfile.TemporaryDirectory(prefix="clonavoce_remote_xtts_") as tmp_dir:
        tmp = Path(tmp_dir)
        profiles_dir = tmp / "profiles"
        output_dir = tmp / "output"
        profile_dir = profiles_dir / profile
        samples_dir = profile_dir / "samples"
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
                preset="professionale",
                accent="italiano_standard",
                speed=payload.speed,
                pitch=0,
                volume=0.0,
                confirmation_token=token,
                progress_callback=None,
            )
            code = core.command_synthesize(synth_args)
            if code != 0 or not out_path.exists():
                raise RuntimeError("Sintesi XTTS remota non riuscita")

            audio_b64 = base64.b64encode(out_path.read_bytes()).decode("ascii")
            return {
                "ok": "true",
                "format": payload.format,
                "audio_b64": audio_b64,
            }
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))
        finally:
            core.PROFILES_DIR = old_profiles_dir
            core.OUTPUT_DIR = old_output_dir


if __name__ == "__main__":
    import uvicorn

    host = os.getenv("CLONAVOCE_REMOTE_HOST", "127.0.0.1")
    port = int(os.getenv("CLONAVOCE_REMOTE_PORT", "8010"))
    uvicorn.run(app, host=host, port=port)
