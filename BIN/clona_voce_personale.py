from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import random
import re
import shutil
import struct
import sys
import tempfile
import textwrap
import threading
import time
import unicodedata
import warnings
import wave
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
_profiles_dir_env = os.environ.get("CLONAVOCE_PROFILES_DIR", "").strip()
PROFILES_DIR = Path(_profiles_dir_env) if _profiles_dir_env else BASE_DIR / "profiles"
OUTPUT_DIR = BASE_DIR.parent / "output"  # Cartella output a livello ClonaVoce
XTTS_MPL_CACHE_DIR = BASE_DIR.parent / ".mplcache"
MAX_TEXT_LENGTH = 0  # 0 = nessun limite
SUPPORTED_SAMPLE_EXTENSIONS = {".wav", ".ogg"}
MAX_SAMPLE_SECONDS = 30.0
ProgressCallback = Callable[[float, str], None]
XTTS_MODEL_NAME = "tts_models/multilingual/multi-dataset/xtts_v2"
_XTTS_MODEL_INSTANCE = None
_XTTS_MODEL_LOCK = threading.Lock()
MOOD_SPEED = {
    "neutro": 1.00,
    "felice": 1.08,
    "triste": 0.90,
    "arrabbiato": 1.15,
    "calmo": 0.92,
    "energico": 1.12,
}
MOOD_ALIASES = {
    "neutral": "neutro",
    "happy": "felice",
    "sad": "triste",
    "angry": "arrabbiato",
    "calm": "calmo",
    "energetic": "energico",
}

LANGUAGE_FULL_NAMES = {
    "it": "Italiano",
    "en": "Inglese",
    "es": "Spagnolo",
    "fr": "Francese",
    "de": "Tedesco",
    "pt": "Portoghese",
    "pl": "Polacco",
    "tr": "Turco",
    "ru": "Russo",
    "nl": "Olandese",
    "cs": "Ceco",
    "ar": "Arabo",
    "zh-cn": "Cinese (Semplificato)",
    "ja": "Giapponese",
    "ko": "Coreano",
    "hu": "Ungherese",
    "th": "Tailandese",
}
LANGUAGE_NAME_TO_CODE = {name.lower(): code for code, name in LANGUAGE_FULL_NAMES.items()}

# Preset vocali: {speed_multiplier, pitch_shift_semitones, volume_db}
VOICE_PRESETS = {
    "professionale": {"speed": 1.00, "pitch": 0, "volume": 0},
    "sussurrante": {"speed": 0.80, "pitch": -3, "volume": -8},
    "energico": {"speed": 1.25, "pitch": 2, "volume": 4},
    "arrogante": {"speed": 0.95, "pitch": 3, "volume": 3},
    "stanco": {"speed": 0.75, "pitch": -2, "volume": -5},
}

VOICE_PRESET_ALIASES = {
    "default": "professionale",
    "whisper": "sussurrante",
    "energetic": "energico",
    "arrogant": "arrogante",
    "tired": "stanco",
}

# Inflessioni dialettali e accenti: {language, language_name, pitch_shift_semitones, speed_multiplier, volume_db}
ACCENT_PRESETS = {
    # Dialetti italiani
    "italiano_standard": {"language": "it", "language_name": "Italiano", "pitch": 0, "speed": 1.00, "volume": 0},
    "napoletano": {"language": "it", "language_name": "Italiano", "pitch": 2, "speed": 1.05, "volume": 1.5},
    "siciliano": {"language": "it", "language_name": "Italiano", "pitch": 1, "speed": 0.98, "volume": 0},
    "romana": {"language": "it", "language_name": "Italiano", "pitch": 1, "speed": 1.02, "volume": 1},
    "toscana": {"language": "it", "language_name": "Italiano", "pitch": -1, "speed": 1.00, "volume": 0},
    "lombarda": {"language": "it", "language_name": "Italiano", "pitch": 0, "speed": 0.95, "volume": -1},
    # Accenti inglesi
    "english_uk": {"language": "en", "language_name": "Inglese", "pitch": -1, "speed": 0.95, "volume": 0},
    "english_us": {"language": "en", "language_name": "Inglese", "pitch": 0, "speed": 1.05, "volume": 0.5},
    "english_irish": {"language": "en", "language_name": "Inglese", "pitch": 1, "speed": 1.00, "volume": 0},
    # Accenti spagnoli
    "spanish_castellano": {"language": "es", "language_name": "Spagnolo", "pitch": 0, "speed": 0.95, "volume": 0},
    "spanish_latin": {"language": "es", "language_name": "Spagnolo", "pitch": 1, "speed": 1.05, "volume": 0.5},
    # Accenti francesi
    "french": {"language": "fr", "language_name": "Francese", "pitch": -1, "speed": 0.90, "volume": -0.5},
    # Accenti tedeschi
    "german": {"language": "de", "language_name": "Tedesco", "pitch": 0, "speed": 0.98, "volume": 0},
    # Accenti portoghesi
    "portuguese_br": {"language": "pt", "language_name": "Portoghese", "pitch": 1, "speed": 1.05, "volume": 0},
    # Accenti tailandesi
    "thai": {"language": "th", "language_name": "Tailandese", "pitch": 1, "speed": 0.90, "volume": 0},
}

ACCENT_PRESET_ALIASES = {
    "standard": "italiano_standard",
    "naples": "napoletano",
    "sicily": "siciliano",
    "rome": "romana",
    "tuscany": "toscana",
    "lombardy": "lombarda",
    "british": "english_uk",
    "american": "english_us",
    "irish": "english_irish",
    "spanish": "spanish_castellano",
    "latin": "spanish_latin",
    "thailand": "thai",
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    if not slug:
        raise ValueError("Nome profilo non valido.")
    return slug


def ensure_dirs() -> None:
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def list_profiles() -> list[str]:
    ensure_dirs()
    def _profile_sort_key(name: str) -> str:
        # Ordina in modo stabile ignorando differenze di maiuscole e accenti.
        normalized = unicodedata.normalize("NFKD", name)
        ascii_like = "".join(ch for ch in normalized if not unicodedata.combining(ch))
        return ascii_like.casefold()

    return sorted(
        (item.name for item in PROFILES_DIR.iterdir() if item.is_dir()),
        key=_profile_sort_key,
    )


def profile_dir(profile: str) -> Path:
    return PROFILES_DIR / slugify(profile)


def profile_file(profile: str) -> Path:
    return profile_dir(profile) / "profile.json"


def load_profile(profile: str) -> dict:
    path = profile_file(profile)
    if not path.exists():
        raise FileNotFoundError(f"Profilo '{profile}' non trovato.")
    return json.loads(path.read_text(encoding="utf-8"))


def save_profile(profile: str, data: dict) -> Path:
    directory = profile_dir(profile)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "profile.json"
    path.write_text(json.dumps(data, indent=2, ensure_ascii=True), encoding="utf-8")
    return path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def wav_info(path: Path) -> dict:
    with wave.open(str(path), "rb") as wav_file:
        frames = wav_file.getnframes()
        rate = wav_file.getframerate()
        duration = frames / float(rate)
        return {
            "channels": wav_file.getnchannels(),
            "sample_width": wav_file.getsampwidth(),
            "framerate": rate,
            "frames": frames,
            "duration_seconds": round(duration, 3),
        }


def soundfile_available() -> bool:
    try:
        import soundfile  # noqa: F401
        return True
    except ImportError:
        return False


def validate_sample_input(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"File audio non trovato: {path}")

    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_SAMPLE_EXTENSIONS:
        allowed = ", ".join(sorted(SUPPORTED_SAMPLE_EXTENSIONS))
        raise ValueError(f"Sono accettati solo file {allowed}.")

    if suffix == ".wav":
        info = wav_info(path)
        info["source_format"] = "wav"
    else:
        if not soundfile_available():
            raise RuntimeError(
                "Supporto OGG non disponibile. Installa il pacchetto soundfile per importare file .ogg."
            )
        import soundfile as sf  # type: ignore

        metadata = sf.info(str(path))
        info = {
            "channels": metadata.channels,
            "sample_width": 2,
            "framerate": metadata.samplerate,
            "frames": metadata.frames,
            "duration_seconds": round(metadata.duration, 3),
            "source_format": "ogg",
        }

    if info["duration_seconds"] < 2.0:
        raise ValueError("Il campione deve durare almeno 2 secondi.")
    return info


def random_segment_plan(total_seconds: float, max_seconds: float = MAX_SAMPLE_SECONDS) -> list[tuple[float, float]]:
    if total_seconds <= max_seconds:
        return [(0.0, total_seconds)]

    # For long audio, pick multiple random windows to keep voice variability while avoiding huge files.
    chunk_len = min(18.0, max_seconds)
    chunk_count = min(6, max(2, int(total_seconds // 30.0) + 1))
    max_start = max(0.0, total_seconds - chunk_len)

    starts: list[float] = []
    for _ in range(chunk_count * 3):
        if len(starts) >= chunk_count:
            break
        candidate = random.uniform(0.0, max_start)
        if all(abs(candidate - existing) >= (chunk_len * 0.55) for existing in starts):
            starts.append(candidate)

    while len(starts) < chunk_count:
        starts.append(random.uniform(0.0, max_start))

    starts.sort()
    return [(start, chunk_len) for start in starts]


def extract_wav_segment(source_wav: Path, destination_wav: Path, start_seconds: float, duration_seconds: float) -> None:
    with wave.open(str(source_wav), "rb") as src:
        params = src.getparams()
        start_frame = int(max(0.0, start_seconds) * params.framerate)
        frame_count = int(max(0.1, duration_seconds) * params.framerate)
        src.setpos(min(start_frame, max(0, params.nframes - 1)))
        frames = src.readframes(frame_count)

    with wave.open(str(destination_wav), "wb") as out:
        out.setparams((params.nchannels, params.sampwidth, params.framerate, 0, params.comptype, params.compname))
        out.writeframes(frames)


def import_sample_as_wav(source: Path, destination: Path) -> None:
    suffix = source.suffix.lower()
    if suffix == ".wav":
        shutil.copy2(source, destination)
        return

    if suffix == ".ogg":
        if not soundfile_available():
            raise RuntimeError(
                "Supporto OGG non disponibile. Installa il pacchetto soundfile per importare file .ogg."
            )
        import soundfile as sf  # type: ignore

        audio_data, sample_rate = sf.read(str(source), always_2d=False)
        sf.write(str(destination), audio_data, sample_rate, format="WAV", subtype="PCM_16")
        return

    allowed = ", ".join(sorted(SUPPORTED_SAMPLE_EXTENSIONS))
    raise ValueError(f"Sono accettati solo file {allowed}.")


def generate_tone(sample_width: int, rate: int, channels: int, seconds: float = 0.18, frequency: int = 1730) -> bytes:
    frame_count = int(rate * seconds)
    amplitude = 0.12
    frames = bytearray()

    for index in range(frame_count):
        value = math.sin(2.0 * math.pi * frequency * index / rate)

        if sample_width == 1:
            sample = int((value * 127.0 * amplitude) + 128)
            packed = struct.pack("<B", max(0, min(255, sample)))
        elif sample_width == 2:
            sample = int(value * 32767.0 * amplitude)
            packed = struct.pack("<h", sample)
        elif sample_width == 4:
            sample = int(value * 2147483647.0 * amplitude)
            packed = struct.pack("<i", sample)
        else:
            raise ValueError(f"Formato WAV non supportato per watermark: {sample_width} byte")

        frames.extend(packed * channels)

    return bytes(frames)


def generate_silence(sample_width: int, rate: int, channels: int, seconds: float = 0.08) -> bytes:
    frame_count = int(rate * seconds)
    if sample_width == 1:
        single_sample = struct.pack("<B", 128)
    elif sample_width == 2:
        single_sample = struct.pack("<h", 0)
    elif sample_width == 4:
        single_sample = struct.pack("<i", 0)
    else:
        raise ValueError(f"Formato WAV non supportato per silenzio: {sample_width} byte")
    return single_sample * channels * frame_count


def apply_audio_watermark(source: Path, destination: Path) -> None:
    with wave.open(str(source), "rb") as src:
        params = src.getparams()
        audio = src.readframes(src.getnframes())

    tone = generate_tone(params.sampwidth, params.framerate, params.nchannels)
    silence = generate_silence(params.sampwidth, params.framerate, params.nchannels)

    with wave.open(str(destination), "wb") as dst:
        dst.setparams(params)
        dst.writeframes(tone)
        dst.writeframes(silence)
        dst.writeframes(audio)
        dst.writeframes(silence)
        dst.writeframes(tone)


def convert_wav_to_mp3(wav_path: Path, mp3_path: Path, bitrate: int = 320) -> None:
    """Converte un file WAV in MP3. Usa pydub se disponibile, altrimenti ffmpeg in PATH."""
    try:
        from pydub import AudioSegment  # type: ignore
        audio = AudioSegment.from_wav(str(wav_path))
        audio.export(str(mp3_path), format="mp3", bitrate=f"{bitrate}k")
        return
    except ImportError:
        pass
    import subprocess
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError(
            "Conversione MP3 non disponibile. "
            "Installa pydub (pip install pydub) con ffmpeg, "
            "oppure aggiungi ffmpeg al PATH di sistema."
        )
    result = subprocess.run(
        [ffmpeg, "-y", "-i", str(wav_path), "-b:a", f"{bitrate}k", str(mp3_path)],
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg ha fallito: {result.stderr.decode(errors='replace')}")


def detect_audio_sample_rate(audio_path: Path) -> int:
    """Rileva il sample rate del file audio con fallback robusto."""
    import subprocess

    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        cmd = [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=sample_rate",
            "-of",
            "default=nokey=1:noprint_wrappers=1",
            str(audio_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            raw = result.stdout.strip()
            if raw.isdigit() and int(raw) > 0:
                return int(raw)

    if audio_path.suffix.lower() == ".wav":
        with wave.open(str(audio_path), "rb") as wav_file:
            rate = int(wav_file.getframerate())
            if rate > 0:
                return rate

    # Fallback sicuro per non bloccare la sintesi in assenza di metadati.
    return 24000

def apply_audio_effects(
    audio_path: Path,
    output_path: Path,
    pitch_semitones: int = 0,
    volume_db: float = 0.0,
) -> None:
    """Applica pitch shift e volume adjustment via ffmpeg."""
    if pitch_semitones == 0 and volume_db == 0.0:
        # No effects needed
        import shutil as _shutil
        _shutil.copy2(audio_path, output_path)
        return

    import subprocess
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError(
            "Effetti audio non disponibili. Installa ffmpeg nel PATH di sistema."
        )

    # Costruisci il filtro audio
    filters = []
    if pitch_semitones != 0:
        # Pitch shift: ogni semitono è un fattore di 2^(1/12)
        pitch_factor = 2 ** (pitch_semitones / 12.0)
        sample_rate = detect_audio_sample_rate(audio_path)
        filters.append(f"asetrate={sample_rate}*{pitch_factor},aresample={sample_rate},atempo=1/{pitch_factor}")
    if volume_db != 0.0:
        filters.append(f"volume={10 ** (volume_db / 20.0)}")

    filter_str = ",".join(filters)
    cmd = [ffmpeg, "-y", "-i", str(audio_path), "-af", filter_str, str(output_path)]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg effetti ha fallito: {result.stderr.decode(errors='replace')}")


def choose_default_output(profile: str, fmt: str = "wav", language: str = "", accent: str = "") -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    ext = "mp3" if fmt.lower() == "mp3" else "wav"
    profile_dir = OUTPUT_DIR / slugify(profile)
    profile_dir.mkdir(parents=True, exist_ok=True)
    
    # Costruisci il nome del file con voce, lingua (estesa), inflessione e timestamp
    name_parts = [slugify(profile)]
    if language:
        lang_code = normalize_language(language)
        lang_label = LANGUAGE_FULL_NAMES.get(lang_code, lang_code)
        name_parts.append(slugify(lang_label))
    if accent:
        name_parts.append(slugify(accent))
    name_parts.append(stamp)
    filename = "_".join(name_parts) + f".{ext}"
    
    return profile_dir / filename


def emit_progress(progress_callback: ProgressCallback | None, percent: float, message: str) -> None:
    if progress_callback is None:
        return
    progress_callback(max(0.0, min(100.0, percent)), message)


def preprocess_text_for_tts(text: str) -> str:
    """Normalizza il testo prima della sintesi vocale.

    - Evita letture letterali di simboli (es. "punto", "virgola", "due punti").
    - Mantiene ? e ! per conservare intonazione di domanda/esclamazione.
    """
    # Normalizza composizione Unicode (accenti) e apostrofi tipografici.
    text = unicodedata.normalize("NFC", text)
    text = text.replace("\u2019", "'").replace("\u2018", "'").replace("\u02bc", "'")

    # Ellissi → virgola (pausa)
    text = re.sub(r'\.{2,}', ',', text)
    # Trattino doppio o lungo / em-dash usato come inciso → pausa
    text = re.sub(r'\s*(?:--|[–—])\s*', ', ', text)
    # Trattino semplice isolato tra spazi (non trattini in parole composte) → pausa
    text = re.sub(r'(?<= )- | -(?= )', ', ', text)
    # Due punti → virgola (evita che SAPI/pyttsx3 li legga come "due punti")
    text = re.sub(r'\s*:\s*', ', ', text)
    # Punto e virgola → virgola
    text = re.sub(r'\s*;\s*', ', ', text)
    # Parentesi tonde e quadre: rimuovi delimitatori ma mantieni il contenuto
    text = re.sub(r'[(){}\[\]]', '', text)
    # Simboli tipografici che potrebbero essere vocalizzati
    text = re.sub(r'[*#_~`^\\|]', '', text)
    # Slash → spazio (es. "e/o" → "e o")
    text = re.sub(r'(?<=\S)/(?=\S)', ' o ', text)
    text = re.sub(r'\s*/\s*', ' ', text)
    # Rimuove virgolette, ma NON gli apostrofi interni alle parole italiane.
    text = re.sub(r'["«»\u201c\u201d]', '', text)
    # Apostrofi isolati (non tra lettere/numeri) -> spazio.
    text = re.sub(r"(?<![0-9A-Za-zÀ-ÖØ-öø-ÿ])'(?![0-9A-Za-zÀ-ÖØ-öø-ÿ])", " ", text)
    # Virgole multiple consecutive → singola virgola
    text = re.sub(r',\s*,+', ',', text)
    # Tratta punteggiatura richiesta come pausa silenziosa, non simbolo da leggere.
    text = text.replace(",", " ")
    # Sostituisce il punto di frase (non decimali tipo 3.14) con spazio.
    text = re.sub(r'(?<!\d)\.(?!\d)', ' ', text)
    # Spazi ridondanti
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def split_text_chunks(text: str, max_chunk_length: int = 220) -> list[str]:
    normalized = re.sub(r"\s+", " ", text).strip()
    normalized = preprocess_text_for_tts(normalized)
    if not normalized:
        return []

    sentences = re.split(r"(?<=[.!?])\s+", normalized)
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue
        if not current:
            current = sentence
            continue
        if len(current) + 1 + len(sentence) <= max_chunk_length:
            current = f"{current} {sentence}"
            continue
        chunks.append(current)
        current = sentence

    if current:
        chunks.append(current)

    final_chunks: list[str] = []
    for chunk in chunks:
        if len(chunk) <= max_chunk_length:
            final_chunks.append(chunk)
            continue
        start = 0
        while start < len(chunk):
            end = min(start + max_chunk_length, len(chunk))
            if end < len(chunk):
                split_at = chunk.rfind(" ", start, end)
                if split_at > start:
                    end = split_at
            final_chunks.append(chunk[start:end].strip())
            start = end

    return [item for item in final_chunks if item]


def concatenate_wav_files(parts: list[Path], destination: Path) -> None:
    if not parts:
        raise RuntimeError("Nessuna traccia audio da concatenare.")

    with wave.open(str(parts[0]), "rb") as first:
        params = first.getparams()
        frames = [first.readframes(first.getnframes())]
        expected_format = (
            params.nchannels,
            params.sampwidth,
            params.framerate,
            params.comptype,
            params.compname,
        )

    for part in parts[1:]:
        with wave.open(str(part), "rb") as handle:
            current_params = handle.getparams()
            current_format = (
                current_params.nchannels,
                current_params.sampwidth,
                current_params.framerate,
                current_params.comptype,
                current_params.compname,
            )
            if current_format != expected_format:
                raise RuntimeError("I segmenti audio prodotti hanno formati WAV incompatibili.")
            frames.append(handle.readframes(handle.getnframes()))

    with wave.open(str(destination), "wb") as out:
        out.setparams(params)
        for frame_block in frames:
            out.writeframes(frame_block)


def read_text_arg(text: str | None, text_file: str | None) -> str:
    if bool(text) == bool(text_file):
        raise ValueError("Usa --text oppure --text-file, non entrambi.")
    if text:
        value = text.strip()
    else:
        value = Path(text_file).read_text(encoding="utf-8").strip()
    if not value:
        raise ValueError("Il testo da sintetizzare e vuoto.")
    if MAX_TEXT_LENGTH > 0 and len(value) > MAX_TEXT_LENGTH:
        raise ValueError(f"Testo troppo lungo. Limite corrente: {MAX_TEXT_LENGTH} caratteri.")
    return value


def normalize_mood(value: str | None) -> str:
    if not value:
        return "neutro"
    mood = value.strip().lower()
    mood = MOOD_ALIASES.get(mood, mood)
    if mood not in MOOD_SPEED:
        allowed = ", ".join(MOOD_SPEED.keys())
        raise ValueError(f"Umore non valido: {value}. Valori supportati: {allowed}")
    return mood


def normalize_preset(value: str | None) -> str:
    if not value:
        return "professionale"
    preset = value.strip().lower()
    preset = VOICE_PRESET_ALIASES.get(preset, preset)
    if preset not in VOICE_PRESETS:
        allowed = ", ".join(VOICE_PRESETS.keys())
        raise ValueError(f"Preset non valido: {value}. Valori supportati: {allowed}")
    return preset


def normalize_language(value: str | None) -> str:
    """Normalizza lingua accettando codice, nome esteso o formato 'Nome (code)'."""
    raw = (value or "it").strip()
    if not raw:
        return "it"

    lowered = raw.lower()
    if lowered in LANGUAGE_FULL_NAMES:
        return lowered

    if lowered in LANGUAGE_NAME_TO_CODE:
        return LANGUAGE_NAME_TO_CODE[lowered]

    match = re.search(r"\(([^)]+)\)$", raw)
    if match:
        code = match.group(1).strip().lower()
        if code in LANGUAGE_FULL_NAMES:
            return code

    allowed = ", ".join(sorted(LANGUAGE_FULL_NAMES.keys()))
    raise ValueError(f"Lingua non valida: {value}. Codici supportati: {allowed}")


def normalize_accent(value: str) -> str:
    """Normalizza e valida il nome dell'accento dialettale."""
    if not value:
        return "italiano_standard"
    accent = value.strip().lower()
    accent = ACCENT_PRESET_ALIASES.get(accent, accent)
    if accent not in ACCENT_PRESETS:
        allowed = ", ".join(ACCENT_PRESETS.keys())
        raise ValueError(f"Accento non valido: {value}. Valori supportati: {allowed}")
    return accent


def parse_dialogue_segments(
    text: str,
    default_profile: str,
    default_language: str,
    default_mood: str,
) -> list[dict]:
    """Parse inline dialogue directives.

    Syntax example:
    {{voice=silvio;mood=felice;language=it}} Ciao!
    {{default}} Torno alle impostazioni di default.
    """
    pattern = re.compile(r"\{\{(.*?)\}\}")
    current = {
        "profile": default_profile,
        "language": default_language,
        "mood": default_mood,
    }
    segments: list[dict] = []
    cursor = 0

    for match in pattern.finditer(text):
        chunk = text[cursor:match.start()].strip()
        if chunk:
            segments.append({
                "text": chunk,
                "profile": current["profile"],
                "language": current["language"],
                "mood": current["mood"],
            })

        directive = match.group(1).strip()
        if directive.lower() == "default":
            current = {
                "profile": default_profile,
                "language": default_language,
                "mood": default_mood,
            }
        else:
            for token in re.split(r"[;,]", directive):
                token = token.strip()
                if not token or "=" not in token:
                    continue
                key, value = token.split("=", 1)
                key = key.strip().lower()
                value = value.strip()
                if key in {"voice", "profile", "voce"}:
                    current["profile"] = slugify(value)
                elif key in {"mood", "umore"}:
                    current["mood"] = normalize_mood(value)
                elif key in {"language", "lang", "lingua"}:
                    current["language"] = value.strip().lower()

        cursor = match.end()

    tail = text[cursor:].strip()
    if tail:
        segments.append({
            "text": tail,
            "profile": current["profile"],
            "language": current["language"],
            "mood": current["mood"],
        })

    if not segments:
        return [{
            "text": text.strip(),
            "profile": default_profile,
            "language": default_language,
            "mood": default_mood,
        }]
    return segments


def find_reference_samples(profile_data: dict, profile_path: Path) -> list[Path]:
    samples = profile_data.get("samples", [])
    if not samples:
        raise ValueError("Nessun campione disponibile nel profilo.")
    references: list[Path] = []
    missing: list[str] = []
    for sample in samples:
        reference = profile_path / "samples" / sample["filename"]
        if reference.exists():
            references.append(reference)
        else:
            missing.append(str(reference))
    if not references:
        raise FileNotFoundError(
            "Nessun campione di riferimento disponibile. Mancanti: " + ", ".join(missing)
        )
    return references


def synthesize_with_pyttsx3(
    text: str,
    destination: Path,
    progress_callback: ProgressCallback | None = None,
    mood: str = "neutro",
    speed_multiplier: float = 1.0,
) -> str:
    try:
        import pyttsx3  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "pyttsx3 non installato. Installa il pacchetto per usare il fallback locale."
        ) from exc

    emit_progress(progress_callback, 2.0, "Inizializzazione motore locale pyttsx3...")
    engine = pyttsx3.init()
    voices = engine.getProperty("voices")
    chosen_voice = None
    for voice in voices:
        languages = getattr(voice, "languages", []) or []
        languages_text = " ".join(
            item.decode(errors="ignore") if isinstance(item, bytes) else str(item)
            for item in languages
        ).lower()
        voice_blob = f"{voice.id} {voice.name} {languages_text}".lower()
        if "ital" in voice_blob or "it_" in voice_blob or "italian" in voice_blob:
            chosen_voice = voice.id
            break

    if chosen_voice:
        engine.setProperty("voice", chosen_voice)
        emit_progress(progress_callback, 5.0, "Lingua italiana rilevata ✓")
    else:
        emit_progress(progress_callback, 5.0, "Uso voce di default")

    rate = int(170 * MOOD_SPEED.get(mood, 1.0) * speed_multiplier)
    engine.setProperty("rate", rate)
    emit_progress(progress_callback, 6.0, f"Velocità impostata: {rate} WPM (umore: {mood})")
    
    chunks = split_text_chunks(text, max_chunk_length=180)
    emit_progress(progress_callback, 8.0, f"Testo diviso in {len(chunks) or 1} segmenti")

    with tempfile.TemporaryDirectory(prefix="clonavoce_pyttsx3_") as tmp_dir:
        part_files: list[Path] = []
        total = max(len(chunks), 1)
        for index, chunk in enumerate(chunks or [text], start=1):
            part_path = Path(tmp_dir) / f"chunk_{index:03d}.wav"
            
            chunk_preview = chunk[:40] + ("..." if len(chunk) > 40 else "")
            emit_progress(progress_callback, 10.0 + (index - 1) / total * 2.0, f"Segmento {index}/{total}: '{chunk_preview}'")
            
            engine.save_to_file(chunk, str(part_path))
            emit_progress(progress_callback, 12.0 + (index - 1) / total * 2.0, f"Sintesi segmento {index}/{total}...")
            engine.runAndWait()
            
            if not part_path.exists():
                raise RuntimeError("pyttsx3 non ha prodotto uno dei segmenti audio attesi.")
            part_files.append(part_path)
            percent = 14.0 + (index / total) * 76.0
            emit_progress(progress_callback, percent, f"✓ Segmento {index}/{total} pronto")

        engine.stop()
        emit_progress(progress_callback, 92.0, "Assemblaggio audio finale...")
        concatenate_wav_files(part_files, destination)
        emit_progress(progress_callback, 96.0, "Assemblaggio completato ✓")

    if not destination.exists():
        raise RuntimeError("pyttsx3 non ha prodotto il file audio atteso.")
    return "pyttsx3"


def synthesize_with_xtts(
    text: str,
    reference_wavs: list[Path],
    destination: Path,
    language: str = "it",
    progress_callback: ProgressCallback | None = None,
    mood: str = "neutro",
    speed_multiplier: float = 1.0,
) -> str:
    XTTS_MPL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MPLCONFIGDIR", str(XTTS_MPL_CACHE_DIR))

    # Evita FutureWarning di torch.load su stderr che in alcuni ambienti PowerShell
    # viene interpretato come errore nonostante la sintesi sia riuscita.
    warnings.filterwarnings(
        "ignore",
        message=r"You are using `torch.load` with `weights_only=False`.*",
        category=FutureWarning,
    )

    try:
        from TTS.api import TTS  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "Motore XTTS non disponibile. Installa una libreria compatibile TTS per usarlo."
        ) from exc

    emit_progress(progress_callback, 2.0, "Verifica campioni di riferimento...")
    ref_str = ", ".join(item.name for item in reference_wavs)
    emit_progress(progress_callback, 4.0, f"Campioni: {ref_str}")
    
    global _XTTS_MODEL_INSTANCE
    if _XTTS_MODEL_INSTANCE is None:
        with _XTTS_MODEL_LOCK:
            if _XTTS_MODEL_INSTANCE is None:
                emit_progress(progress_callback, 5.0, "Caricamento modello XTTS (prima volta, puo durare ~30-60s)...")
                progress_thread_stop = threading.Event()

                def update_during_load():
                    """Aggiorna il progresso ogni 3 secondi durante il caricamento iniziale."""
                    messages = [
                        "Scaricamento pesi del modello...",
                        "Inizializzazione GPU...",
                        "Compilazione acceleratori...",
                        "Verifica modello XTTS v2...",
                    ]
                    msg_idx = 0
                    while not progress_thread_stop.is_set() and progress_callback:
                        msg = messages[msg_idx % len(messages)]
                        emit_progress(progress_callback, min(10.5, 5.0 + msg_idx * 1.2), msg)
                        msg_idx += 1
                        progress_thread_stop.wait(3.0)

                updater = threading.Thread(target=update_during_load, daemon=True)
                updater.start()
                try:
                    _XTTS_MODEL_INSTANCE = TTS(XTTS_MODEL_NAME)
                finally:
                    progress_thread_stop.set()
                    updater.join(timeout=1)
                emit_progress(progress_callback, 11.0, "Modello XTTS caricato ✓")
            else:
                emit_progress(progress_callback, 11.0, "Modello XTTS gia in memoria ✓")
    else:
        emit_progress(progress_callback, 11.0, "Modello XTTS gia in memoria ✓")

    tts = _XTTS_MODEL_INSTANCE
    
    chunks = split_text_chunks(text)
    emit_progress(progress_callback, 12.0, f"Split completato: {len(chunks) or 1} segmenti da sintetizzare")

    with tempfile.TemporaryDirectory(prefix="clonavoce_xtts_") as tmp_dir:
        part_files: list[Path] = []
        total = max(len(chunks), 1)
        speaker_wavs = [str(item) for item in reference_wavs]
        for index, chunk in enumerate(chunks or [text], start=1):
            part_path = Path(tmp_dir) / f"chunk_{index:03d}.wav"
            
            # Anteprima del testo
            chunk_preview = chunk[:50] + ("..." if len(chunk) > 50 else "")
            emit_progress(progress_callback, 12.5 + (index - 1) / total * 2.0, f"Segmento {index}/{total}: '{chunk_preview}'")
            
            tts_kwargs = {
                "text": chunk,
                "file_path": str(part_path),
                "speaker_wav": speaker_wavs,
                "language": language,
            }
            speed = MOOD_SPEED.get(mood, 1.0) * speed_multiplier
            if abs(speed - 1.0) > 0.01:
                tts_kwargs["speed"] = speed

            emit_progress(progress_callback, 14.5 + (index - 1) / total * 2.0, f"Sintesi segmento {index}/{total} (speed: {speed:.2f}x)...")
            
            try:
                tts.tts_to_file(**tts_kwargs)
            except TypeError:
                # Some TTS backends ignore/deny speed; fallback without breaking synthesis.
                tts_kwargs.pop("speed", None)
                tts.tts_to_file(**tts_kwargs)
            if not part_path.exists():
                raise RuntimeError("XTTS non ha prodotto uno dei segmenti audio attesi.")
            part_files.append(part_path)
            percent = 17.0 + (index / total) * 75.0
            emit_progress(progress_callback, percent, f"✓ Segmento {index}/{total} pronto")

        emit_progress(progress_callback, 94.0, "Assemblaggio segmenti audio...")
        concatenate_wav_files(part_files, destination)
        emit_progress(progress_callback, 96.0, "Assemblaggio completato ✓")

    if not destination.exists():
        raise RuntimeError("XTTS non ha prodotto il file audio atteso.")
    return "xtts"


def xtts_available() -> bool:
    return importlib.util.find_spec("TTS.api") is not None


def pyttsx3_available() -> bool:
    try:
        import pyttsx3  # noqa: F401
        return True
    except ImportError:
        return False


def write_sidecar(output_wav: Path, metadata: dict) -> Path:
    sidecar = output_wav.with_suffix(output_wav.suffix + ".json")
    sidecar.write_text(json.dumps(metadata, indent=2, ensure_ascii=True), encoding="utf-8")
    return sidecar


def command_init_profile(args: argparse.Namespace) -> int:
    ensure_dirs()
    profile = slugify(args.profile)
    path = profile_file(profile)
    if path.exists():
        raise FileExistsError(f"Il profilo '{profile}' esiste gia.")

    confirmation_token = f"SELF-VOICE-{profile.upper()}"
    data = {
        "profile": profile,
        "display_name": args.display_name.strip(),
        "created_at": utc_now_iso(),
        "consent": {
            "speaker_confirmed": bool(args.i_am_the_speaker),
            "attested_at": utc_now_iso() if args.i_am_the_speaker else None,
            "confirmation_token": confirmation_token,
            "personal_use_only": True,
        },
        "samples": [],
        "safety": {
            "audio_watermark": True,
            "json_sidecar": True,
            "max_text_length": MAX_TEXT_LENGTH,
        },
    }
    save_path = save_profile(profile, data)
    (profile_dir(profile) / "samples").mkdir(parents=True, exist_ok=True)
    consent_note = profile_dir(profile) / "CONSENSO_USO_PERSONALE.txt"
    consent_note.write_text(
        textwrap.dedent(
            f"""
            Profilo: {profile}
            Intestatario: {args.display_name.strip()}
            Creato: {data['created_at']}

            Questo profilo e destinato solo alla mia voce e al mio uso personale.
            Token di conferma per la sintesi: {confirmation_token}
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    print(f"Profilo creato: {save_path}")
    print(f"Token conferma sintesi: {confirmation_token}")
    print("Aggiungi ora uno o piu campioni WAV con il comando add-sample.")
    return 0


def command_add_sample(args: argparse.Namespace) -> int:
    ensure_dirs()
    profile = slugify(args.profile)
    data = load_profile(profile)
    source = Path(args.wav).expanduser().resolve()
    info = validate_sample_input(source)
    source_sha256 = sha256_file(source)

    sample_dir = profile_dir(profile) / "samples"
    sample_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    created_entries: list[dict] = []

    with tempfile.TemporaryDirectory(prefix="clonavoce_sample_") as tmp_dir:
        imported_wav = Path(tmp_dir) / "imported.wav"
        import_sample_as_wav(source, imported_wav)
        imported_info = wav_info(imported_wav)
        segments = random_segment_plan(imported_info["duration_seconds"], max_seconds=MAX_SAMPLE_SECONDS)

        for index, (start_seconds, duration_seconds) in enumerate(segments, start=1):
            sample_name = f"sample_{timestamp}_{index:02d}.wav"
            destination = sample_dir / sample_name
            if len(segments) == 1 and start_seconds <= 0.0:
                shutil.copy2(imported_wav, destination)
            else:
                extract_wav_segment(imported_wav, destination, start_seconds, duration_seconds)

            stored_info = wav_info(destination)
            entry = {
                "filename": sample_name,
                "original_filename": source.name,
                "original_format": info.get("source_format", source.suffix.lower().lstrip(".")),
                "source_sha256": source_sha256,
                "sha256": sha256_file(destination),
                "added_at": utc_now_iso(),
                "segment": {
                    "start_seconds": round(start_seconds, 3),
                    "duration_seconds": round(stored_info["duration_seconds"], 3),
                },
                "info": stored_info,
            }
            data.setdefault("samples", []).append(entry)
            created_entries.append(entry)

    save_profile(profile, data)

    print(f"Campioni aggiunti: {len(created_entries)}")
    print(f"Origine: {source.name} ({info.get('source_format', source.suffix.lower().lstrip('.'))})")
    for item in created_entries:
        seg = item.get("segment", {})
        print(
            f"- {item['filename']} | start={seg.get('start_seconds', 0.0)}s | durata={item['info']['duration_seconds']}s"
        )
    return 0


def command_remove_sample(args: argparse.Namespace) -> int:
    ensure_dirs()
    profile = slugify(args.profile)
    data = load_profile(profile)
    samples = data.get("samples", [])
    if not samples:
        raise ValueError("Il profilo non contiene campioni da rimuovere.")

    remove_all = bool(getattr(args, "all", False))
    requested = [item.strip() for item in (getattr(args, "sample", []) or []) if item and item.strip()]
    if not remove_all and not requested:
        raise ValueError("Specifica --sample (anche piu volte) oppure --all.")

    selected_filenames: set[str] = set()
    if remove_all:
        selected_filenames = {entry.get("filename", "") for entry in samples}
    else:
        for token in requested:
            if token.isdigit():
                index = int(token)
                if 1 <= index <= len(samples):
                    selected_filenames.add(samples[index - 1].get("filename", ""))
                    continue
            token_low = token.lower()
            for entry in samples:
                filename = str(entry.get("filename", ""))
                original = str(entry.get("original_filename", ""))
                if token_low == filename.lower() or token_low == original.lower():
                    selected_filenames.add(filename)

    if not selected_filenames:
        raise ValueError("Nessun campione trovato con i criteri indicati.")

    kept: list[dict] = []
    removed: list[dict] = []
    for entry in samples:
        if entry.get("filename", "") in selected_filenames:
            removed.append(entry)
        else:
            kept.append(entry)

    data["samples"] = kept
    save_profile(profile, data)

    if not bool(getattr(args, "keep_files", False)):
        sample_dir = profile_dir(profile) / "samples"
        for entry in removed:
            filename = entry.get("filename")
            if not filename:
                continue
            path = sample_dir / filename
            if path.exists():
                path.unlink()

    print(f"Campioni rimossi: {len(removed)}")
    for entry in removed:
        print(f"- {entry.get('filename')} (origine: {entry.get('original_filename', '-')})")
    return 0


def command_status(args: argparse.Namespace) -> int:
    profile = slugify(args.profile)
    data = load_profile(profile)
    consent = data.get("consent", {})
    samples = data.get("samples", [])
    print(f"Profilo: {data.get('profile')}")
    print(f"Nome: {data.get('display_name')}")
    print(f"Consenso confermato: {consent.get('speaker_confirmed')}")
    print(f"Campioni WAV: {len(samples)}")
    if samples:
        latest = samples[-1]
        print(f"Ultimo campione: {latest['filename']} ({latest['info']['duration_seconds']} s)")
    print(f"pyttsx3 disponibile: {pyttsx3_available()}")
    print(f"xtts disponibile: {xtts_available()}")
    print(f"Token sintesi: {consent.get('confirmation_token')}")
    return 0


def command_list_voices(_: argparse.Namespace) -> int:
    if not pyttsx3_available():
        raise RuntimeError("pyttsx3 non installato. Nessuna voce locale interrogabile.")
    import pyttsx3  # type: ignore

    engine = pyttsx3.init()
    for voice in engine.getProperty("voices"):
        languages = getattr(voice, "languages", []) or []
        languages_text = ", ".join(
            item.decode(errors="ignore") if isinstance(item, bytes) else str(item)
            for item in languages
        )
        print(f"- {voice.id} | {voice.name} | {languages_text}")
    engine.stop()
    return 0


def command_synthesize(args: argparse.Namespace) -> int:
    ensure_dirs()
    profile = slugify(args.profile)
    data = load_profile(profile)
    consent = data.get("consent", {})
    if not consent.get("speaker_confirmed"):
        raise PermissionError("Il profilo non ha consenso confermato.")
    expected_token = consent.get("confirmation_token")
    if not args.confirmation_token or args.confirmation_token.strip() != expected_token:
        raise PermissionError("Token di conferma non valido per questo profilo.")

    text = read_text_arg(args.text, args.text_file)
    progress_callback = getattr(args, "progress_callback", None)
    mood = normalize_mood(getattr(args, "mood", "neutro"))
    preset = normalize_preset(getattr(args, "preset", "professionale"))
    preset_cfg = VOICE_PRESETS[preset]
    
    # Accent handling: load accent config
    accent = normalize_accent(getattr(args, "accent", "italiano_standard"))
    accent_cfg = ACCENT_PRESETS[accent]
    selected_language = normalize_language(getattr(args, "language", None) or "it")
    accent_language = normalize_language(accent_cfg.get("language", selected_language))
    language = selected_language if accent == "italiano_standard" else accent_language
    language_name = LANGUAGE_FULL_NAMES.get(language, language)
    
    # Combine preset + accent + user parameters for speed, pitch, volume
    user_speed = float(getattr(args, "speed", 1.0))
    user_pitch = int(getattr(args, "pitch", 0))
    user_volume = float(getattr(args, "volume", 0.0))
    
    effective_speed = max(0.5, min(2.0, preset_cfg["speed"] * accent_cfg["speed"] * user_speed))
    final_pitch = int(preset_cfg["pitch"] + accent_cfg["pitch"] + user_pitch)
    final_volume = float(preset_cfg["volume"] + accent_cfg["volume"] + user_volume)
    
    if args.out:
        output_path = Path(args.out).expanduser().resolve()
    else:
        default_fmt = str(getattr(args, "format", "mp3") or "mp3")
        output_path = choose_default_output(profile, fmt=default_fmt, language=language, accent=accent)

    output_fmt = output_path.suffix.lstrip(".").lower()
    if output_fmt not in {"wav", "mp3"}:
        raise ValueError(
            f"Estensione output non supportata: '.{output_fmt}'. Usa .wav o .mp3"
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    emit_progress(
        progress_callback,
        1.0,
        f"Preset: {preset} | Inflessione: {accent} | Lingua: {language_name} | Velocita: {effective_speed:.2f}x | Pitch: {final_pitch} | Volume: {final_volume:.1f}dB",
    )

    segments = parse_dialogue_segments(
        text=text,
        default_profile=profile,
        default_language=language,
        default_mood=mood,
    )

    emit_progress(progress_callback, 1.8, f"Parsing completato: {len(segments)} segmenti | Testo: {len(text)} caratteri")
    emit_progress(progress_callback, 2.5, f"Inizio sintesi vocale con {args.engine}...")

    with tempfile.TemporaryDirectory(prefix="clonavoce_") as tmp_dir:
        raw_output = Path(tmp_dir) / "raw.wav"
        engine = args.engine
        selected_engine = None
        reference_samples_used: dict[str, list[str]] = {}

        part_files: list[Path] = []
        for index, segment in enumerate(segments, start=1):
            seg_profile = slugify(segment["profile"])
            seg_data = data if seg_profile == profile else load_profile(seg_profile)
            seg_consent = seg_data.get("consent", {})
            if not seg_consent.get("speaker_confirmed"):
                raise PermissionError(f"Il profilo '{seg_profile}' non ha consenso confermato.")

            seg_text = str(segment["text"])
            seg_language = str(segment["language"])
            seg_mood = normalize_mood(str(segment["mood"]))
            part_path = Path(tmp_dir) / f"dialog_{index:03d}.wav"

            emit_progress(
                progress_callback,
                3.0 + (index - 1) / max(1, len(segments)) * 0.5,
                f"Segmento {index}/{len(segments)} | voce={seg_profile} | umore={seg_mood} | lingue={seg_language}",
            )

            if engine in {"auto", "xtts"} and xtts_available():
                seg_refs = find_reference_samples(seg_data, profile_dir(seg_profile))
                reference_samples_used[seg_profile] = [item.name for item in seg_refs]
                selected_engine = synthesize_with_xtts(
                    seg_text,
                    seg_refs,
                    part_path,
                    language=seg_language,
                    progress_callback=progress_callback,
                    mood=seg_mood,
                    speed_multiplier=effective_speed,
                )
            elif engine == "xtts":
                raise RuntimeError("Hai richiesto XTTS ma il motore non e disponibile.")
            else:
                selected_engine = synthesize_with_pyttsx3(
                    seg_text,
                    part_path,
                    progress_callback=progress_callback,
                    mood=seg_mood,
                    speed_multiplier=effective_speed,
                )

            part_files.append(part_path)

        emit_progress(progress_callback, 92.0, "Assemblaggio tutti i segmenti in un unico audio...")
        concatenate_wav_files(part_files, raw_output)
        emit_progress(progress_callback, 95.0, "Assemblaggio completato ✓")

        if output_fmt == "mp3":
            emit_progress(progress_callback, 96.0, "Conversione WAV → MP3 a 320kbps...")
            convert_wav_to_mp3(raw_output, output_path)
            emit_progress(progress_callback, 97.0, "Conversione MP3 completata ✓")
        else:
            import shutil as _shutil
            emit_progress(progress_callback, 96.0, "Salvataggio file WAV finale...")
            _shutil.copy2(raw_output, output_path)
            emit_progress(progress_callback, 97.0, "Salvataggio completato ✓")

        pitch_semitones = final_pitch
        volume_db = final_volume
        if pitch_semitones != 0 or abs(volume_db) > 0.01:
            emit_progress(
                progress_callback,
                98.0,
                f"Applicazione effetti audio (pitch: {pitch_semitones}, volume: {volume_db:.1f} dB)...",
            )
            effects_ext = output_path.suffix if output_path.suffix else ".wav"
            effects_output = Path(tmp_dir) / f"with_effects{effects_ext}"
            apply_audio_effects(
                output_path,
                effects_output,
                pitch_semitones=pitch_semitones,
                volume_db=volume_db,
            )
            import shutil as _shutil
            _shutil.move(str(effects_output), str(output_path))
            emit_progress(progress_callback, 99.0, "Effetti applicati ✓")

    metadata = {
        "generated_at": utc_now_iso(),
        "profile": data.get("profile"),
        "display_name": data.get("display_name"),
        "engine": selected_engine,
        "mood": mood,
        "language": language,
        "language_name": language_name,
        "preset": preset,
        "accent": accent,
        "speed_multiplier": effective_speed,
        "pitch_semitones": final_pitch,
        "volume_db": final_volume,
        "reference_samples": reference_samples_used,
        "dialogue_segments": [
            {
                "profile": item["profile"],
                "language": item["language"],
                "mood": item["mood"],
                "text_length": len(item["text"]),
            }
            for item in segments
        ],
        "synthetic_voice": True,
        "personal_use_only": True,
        "text_length": len(text),
        "audio_watermark": False,
        "output_file": str(output_path),
    }
    sidecar = write_sidecar(output_path, metadata)
    emit_progress(progress_callback, 100.0, "Audio pronto")

    print(f"Audio generato: {output_path}")
    print(f"Metadati: {sidecar}")
    print(f"Motore usato: {selected_engine}")
    return 0


def command_analyze_audio(args: argparse.Namespace) -> int:
    """Analizza un file audio per estrarre inflessioni prosodiche.
    
    Crea un ACCENT_PRESET personalizzato basato su pitch, speed, volume dell'audio.
    """
    try:
        from audio_inflection_analyzer import extract_and_save_preset
    except ImportError:
        raise ImportError(
            "Modulo audio_inflection_analyzer non trovato.\n"
            "Assicurati che audio_inflection_analyzer.py sia nella stessa cartella."
        )
    
    audio_path = Path(args.audio).expanduser().resolve()
    if not audio_path.exists():
        raise FileNotFoundError(f"File audio non trovato: {audio_path}")
    
    output_json = Path(args.output).expanduser().resolve() if args.output else audio_path.parent / f"{audio_path.stem}_preset.json"
    
    print(f"📊 Analisi audio in corso: {audio_path.name}")
    print("Estrazione F0, energia, timing...")
    
    language_code = normalize_language(args.language or "it")

    preset = extract_and_save_preset(
        audio_path,
        output_json,
        accent_name=args.name or audio_path.stem,
        language=language_code,
    )
    preset["language_name"] = LANGUAGE_FULL_NAMES.get(language_code, language_code)
    
    print(f"\n✓ Preset creato con sucesso!")
    print(f"  Pitch shift: {preset['pitch']:+d} semitoni")
    print(f"  Speed multiplier: {preset['speed']:.2f}x")
    print(f"  Volume: {preset['volume']:+.1f} dB")
    print(f"\nSalvato in: {output_json}")
    print(f"\nPer usare questo accento, aggiungi a ACCENT_PRESETS:")
    print(
        f'  "{preset.get("accent_name", args.name or audio_path.stem)}": '
        f'{json.dumps({"language": preset["language"], "language_name": preset["language_name"], "pitch": preset["pitch"], "speed": preset["speed"], "volume": preset["volume"]}, indent=4)}'
    )
    
    return 0


def command_check_audio(args: argparse.Namespace) -> int:
    """Controlla un audio prodotto e suggerisce correzioni del preset accento."""
    try:
        from audio_inflection_analyzer import suggest_preset_corrections
    except ImportError:
        raise ImportError(
            "Modulo audio_inflection_analyzer non trovato.\n"
            "Assicurati che audio_inflection_analyzer.py sia nella stessa cartella."
        )

    audio_path = Path(args.audio).expanduser().resolve()
    if not audio_path.exists():
        raise FileNotFoundError(f"File audio non trovato: {audio_path}")

    accent = normalize_accent(args.accent)
    current = ACCENT_PRESETS[accent]
    report = suggest_preset_corrections(audio_path, current)

    print(f"Controllo audio: {audio_path.name}")
    print(f"Preset corrente: {accent}")
    print(
        f"Corrente   -> pitch={report['current']['pitch']:+d}, speed={report['current']['speed']:.2f}, volume={report['current']['volume']:+.1f}"
    )
    print(
        f"Misurato   -> pitch={report['measured']['pitch']:+d}, speed={report['measured']['speed']:.2f}, volume={report['measured']['volume']:+.1f}"
    )
    print(
        f"Delta      -> pitch={report['delta']['pitch']:+d}, speed={report['delta']['speed']:+.2f}, volume={report['delta']['volume']:+.1f}"
    )
    print(
        f"Suggerito  -> pitch={report['suggested']['pitch']:+d}, speed={report['suggested']['speed']:.2f}, volume={report['suggested']['volume']:+.1f}"
    )

    suggested = {
        "language": current.get("language", "it"),
        "language_name": current.get("language_name", LANGUAGE_FULL_NAMES.get(current.get("language", "it"), "Italiano")),
        "pitch": report["suggested"]["pitch"],
        "speed": report["suggested"]["speed"],
        "volume": report["suggested"]["volume"],
    }

    print("\nAggiornamento consigliato in ACCENT_PRESETS:")
    print(f'"{accent}": {json.dumps(suggested, ensure_ascii=False)}')
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Tool locale per uso della propria voce con consenso esplicito e watermark.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init-profile", help="Crea un nuovo profilo personale.")
    init_parser.add_argument("--profile", required=True, help="Identificatore profilo.")
    init_parser.add_argument("--display-name", required=True, help="Nome leggibile del proprietario.")
    init_parser.add_argument(
        "--i-am-the-speaker",
        action="store_true",
        help="Attesta che il profilo appartiene alla tua voce.",
    )
    init_parser.set_defaults(func=command_init_profile)

    sample_parser = subparsers.add_parser("add-sample", help="Aggiunge un campione WAV o OGG al profilo.")
    sample_parser.add_argument("--profile", required=True)
    sample_parser.add_argument("--wav", required=True, help="Percorso del file WAV o OGG.")
    sample_parser.set_defaults(func=command_add_sample)

    remove_parser = subparsers.add_parser("remove-sample", help="Rimuove campioni dal profilo.")
    remove_parser.add_argument("--profile", required=True)
    remove_parser.add_argument(
        "--sample",
        action="append",
        help="Indice 1-based o filename/original_filename del campione da rimuovere. Ripetibile.",
    )
    remove_parser.add_argument("--all", action="store_true", help="Rimuove tutti i campioni del profilo.")
    remove_parser.add_argument(
        "--keep-files",
        action="store_true",
        help="Rimuove solo dal profilo JSON ma lascia i file audio sul disco.",
    )
    remove_parser.set_defaults(func=command_remove_sample)

    status_parser = subparsers.add_parser("status", help="Mostra lo stato del profilo.")
    status_parser.add_argument("--profile", required=True)
    status_parser.set_defaults(func=command_status)

    voices_parser = subparsers.add_parser("list-voices", help="Elenca le voci locali pyttsx3.")
    voices_parser.set_defaults(func=command_list_voices)

    synth_parser = subparsers.add_parser("synthesize", help="Genera un audio sintetico dal profilo.")
    synth_parser.add_argument("--profile", required=True)
    synth_parser.add_argument("--text", help="Testo da sintetizzare.")
    synth_parser.add_argument("--text-file", help="File UTF-8 contenente il testo.")
    synth_parser.add_argument(
        "--engine",
        choices=["auto", "pyttsx3", "xtts"],
        default="auto",
        help="Motore di sintesi da usare.",
    )
    synth_parser.add_argument("--out", help="Percorso di output (es. output.wav oppure output.mp3).")
    synth_parser.add_argument(
        "--language",
        default="it",
        help="Lingua di sintesi (accetta codice o nome esteso: Italiano, Inglese, Spagnolo, Francese, Tedesco, Portoghese, ecc.). Default: it",
    )
    synth_parser.add_argument(
        "--mood",
        default="neutro",
        choices=list(MOOD_SPEED.keys()),
        help="Umore della voce: neutro, felice, triste, arrabbiato, calmo, energico.",
    )
    synth_parser.add_argument(
        "--preset",
        default="professionale",
        choices=list(VOICE_PRESETS.keys()),
        help="Preset vocale: professionale, sussurrante, energico, arrogante, stanco.",
    )
    synth_parser.add_argument(
        "--speed",
        type=float,
        default=1.0,
        help="Moltiplicatore velocita sintesi (es. 0.8 = piu lento, 1.2 = piu veloce). Default: 1.0",
    )
    synth_parser.add_argument(
        "--pitch",
        type=int,
        default=0,
        help="Shift pitch in semitoni (-12 = piu grave, +12 = piu acuto). Default: 0",
    )
    synth_parser.add_argument(
        "--volume",
        type=float,
        default=0.0,
        help="Aggiustamento volume in dB (-12 = piu silenzioso, +12 = piu forte). Default: 0.0",
    )
    synth_parser.add_argument(
        "--accent",
        default="italiano_standard",
        choices=list(ACCENT_PRESETS.keys()),
        help="Inflessione dialettale o accento estero: italiano_standard, napoletano, siciliano, romana, toscana, lombarda, english_uk, english_us, english_irish, spanish_castellano, spanish_latin, french, german, portuguese_br.",
    )
    synth_parser.add_argument(
        "--confirmation-token",
        help="Token del profilo richiesto per autorizzare la sintesi.",
    )
    synth_parser.set_defaults(func=command_synthesize)

    # Subcommand: analyze-audio
    analyze_parser = subparsers.add_parser(
        "analyze-audio",
        help="Analizza file audio per estrarre inflessioni prosodiche (pitch, speed, volume) e creare custom accent preset.",
    )
    analyze_parser.add_argument(
        "audio",
        help="Percorso file audio (wav, mp3, ogg) da analizzare",
    )
    analyze_parser.add_argument(
        "--output",
        help="Percorso JSON di output per salvare preset (default: stessa cartella audio con .json)",
    )
    analyze_parser.add_argument(
        "--name",
        help="Nome per il nuovo accento (default: nome file audio)",
    )
    analyze_parser.add_argument(
        "--language",
        default="it",
        help="Lingua XTTS per il preset (codice o nome esteso). Default: it",
    )
    analyze_parser.set_defaults(func=command_analyze_audio)

    # Subcommand: check-audio
    check_parser = subparsers.add_parser(
        "check-audio",
        help="Controlla un audio prodotto e suggerisce correzioni per un preset accento esistente.",
    )
    check_parser.add_argument(
        "audio",
        help="Percorso file audio prodotto (wav/mp3) da analizzare",
    )
    check_parser.add_argument(
        "--accent",
        required=True,
        choices=list(ACCENT_PRESETS.keys()),
        help="Preset accento da verificare e correggere",
    )
    check_parser.set_defaults(func=command_check_audio)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.func(args)
    except Exception as exc:
        print(f"Errore: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
