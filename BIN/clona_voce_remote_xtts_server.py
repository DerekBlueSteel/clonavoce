from __future__ import annotations

import argparse
import base64
import json
import os
import tempfile
from pathlib import Path

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


@app.get("/health")
def health() -> dict[str, bool]:
    return {"ok": True}


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
