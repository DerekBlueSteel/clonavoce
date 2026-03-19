"""Analizza file audio per estrarre inflessioni prosodiche (pitch, velocità, dinamica).

Usa crepe per estrazione F0, librosa per energia e timing.
Restituisce parametri che possono essere applicati come ACCENT_PRESET.
"""

from __future__ import annotations

import numpy as np
from pathlib import Path
import json


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


def analyze_audio_inflections(audio_path: str | Path) -> dict:
    """Analizza un file audio ed estrae inflessioni prosodiche.
    
    Args:
        audio_path: Percorso al file audio (wav, mp3, ogg)
    
    Returns:
        dict con chiavi:
        - pitch_shift_semitones: Traslazione pitch media (int, -12 a +12)
        - speed_multiplier: Moltiplicatore velocità (float, 0.5 a 2.0)
        - volume_db: Ajustamento volume (float, -12 a +12)
        - f0_mean: Media F0 (Hz)
        - f0_std: Deviazione standard F0
        - energy_mean: Media energia (dB)
        - analysis_details: dict con dati raw
    """
    try:
        import librosa
        import crepe
    except ImportError:
        raise ImportError(
            "Dipendenze mancanti. Installa con:\n"
            "pip install librosa crepe numpy"
        )
    
    audio_path = Path(audio_path)
    if not audio_path.exists():
        raise FileNotFoundError(f"File audio non trovato: {audio_path}")
    
    # Carica audio
    y, sr = librosa.load(str(audio_path), sr=None, mono=True)
    
    # === ESTRAZIONE F0 (PITCH) con CREPE ===
    # CREPE è più accurato di librosa.yin ma richiede GPU/CPU rispetto allo yin
    try:
        # crepe restituisce (times, frequencies, confidence, activation)
        _, frequency, confidence, _ = crepe.predict(y, sr, viterbi=True)
        
        # Filtra basandosi su confidence (>0.1 = valido)
        valid_freqs = frequency[confidence > 0.1]
        if len(valid_freqs) > 0:
            f0_mean = float(np.mean(valid_freqs))
            f0_std = float(np.std(valid_freqs))
        else:
            f0_mean = 0.0
            f0_std = 0.0
    except Exception as e:
        print(f"⚠ CREPE fallito ({e}), uso librosa.yin")
        f0 = librosa.yin(y, fmin=50, fmax=500, sr=sr)
        valid_f0 = f0[f0 > 0]
        f0_mean = float(np.mean(valid_f0)) if len(valid_f0) > 0 else 0.0
        f0_std = float(np.std(valid_f0)) if len(valid_f0) > 0 else 0.0
    
    # === ESTRAZIONE ENERGIA (VOLUME) ===
    # S = spettrogramma in scala logaritmica
    S = librosa.feature.melspectrogram(y=y, sr=sr, n_mels=128)
    S_db = librosa.power_to_db(S, ref=np.max)
    energy_mean = float(np.mean(S_db))
    energy_std = float(np.std(S_db))
    
    # === ESTRAZIONE TIMING (VELOCITÀ) ===
    # Stima durata e conta frame per derivare velocità
    duration = librosa.get_duration(y=y, sr=sr)
    
    # Rileva onset (attacchi consonantici) per misurare pauses
    onset_frames = librosa.onset.onset_detect(y=y, sr=sr)
    onset_times = librosa.frames_to_time(onset_frames, sr=sr)
    
    if len(onset_times) > 1:
        # Media tra gli onset (articolazione)
        inter_onset_intervals = np.diff(onset_times)
        mean_ioi = float(np.mean(inter_onset_intervals))
    else:
        mean_ioi = duration
    
    # === CONVERSIONE A PARAMETRI ACCENT ===
    # Assumiamo un reference pitch (italiano standard ~120Hz femminile)
    reference_f0 = 120.0
    
    # Pitch shift in semitoni
    if f0_mean > 0:
        pitch_shift_semitones = int(12 * np.log2(f0_mean / reference_f0))
        pitch_shift_semitones = max(-12, min(12, pitch_shift_semitones))
    else:
        pitch_shift_semitones = 0
    
    # Volume: converte energia media (-80 dB a 0 dB) a range -12 a +12 dB
    # Normaliza tra -12 e +12 basandosi su energy_mean (-30 dB = reference)
    reference_energy = -30.0
    volume_db = float((energy_mean - reference_energy) / 5.0)  # Scala empirica
    volume_db = max(-12.0, min(12.0, volume_db))
    
    # Speed multiplier basato su onset spacing
    # Audio lento = grandi intervalli tra onset
    # Audio veloce = piccoli intervalli
    reference_mean_ioi = 0.5  # 500ms reference
    if mean_ioi > 0:
        speed_multiplier = reference_mean_ioi / mean_ioi
        speed_multiplier = max(0.5, min(2.0, speed_multiplier))
    else:
        speed_multiplier = 1.0
    
    return {
        # Formato diretto compatibile con ACCENT_PRESETS
        "pitch": int(pitch_shift_semitones),
        "speed": float(speed_multiplier),
        "volume": float(volume_db),
        "language": "it",  # Default per analisi
        "language_name": "Italiano",
        # Alias legacy
        "pitch_shift_semitones": int(pitch_shift_semitones),
        "speed_multiplier": float(speed_multiplier),
        "volume_db": float(volume_db),
        # Dati grezzi per debugging/refining
        "analysis_details": {
            "f0_mean": f0_mean,
            "f0_std": f0_std,
            "energy_mean": energy_mean,
            "energy_std": energy_std,
            "mean_onset_interval": mean_ioi,
            "duration_seconds": duration,
            "onset_count": len(onset_times),
        },
    }


def create_accent_preset_from_audio(
    audio_path: str | Path,
    accent_name: str,
    language: str = "it",
    language_name: str = "Italiano"
) -> dict:
    """Crea un preset accento da analisi audio.
    
    Args:
        audio_path: File audio di riferimento
        accent_name: Nome per il nuovo accento (es. "mio_accento")
        language: Codice lingua XTTS
        language_name: Nome lingua per esteso
    
    Returns:
        dict pronto per aggiunta a ACCENT_PRESETS
    """
    analysis = analyze_audio_inflections(audio_path)
    analysis["accent_name"] = accent_name
    analysis["language"] = language
    analysis["language_name"] = language_name
    
    return analysis


def suggest_preset_corrections(audio_path: str | Path, current_preset: dict, blend: float = 0.4) -> dict:
    """Confronta audio prodotto con preset corrente e suggerisce correzioni.
    
    Args:
        audio_path: File audio da analizzare.
        current_preset: Preset attuale da confrontare.
        blend: Frazione del delta da applicare (0.0 = nessuna modifica, 1.0 = valore misurato pieno).
                Default 0.4 per correzioni più graduali.
    """
    measured = analyze_audio_inflections(audio_path)
    current_pitch = int(current_preset.get("pitch", 0))
    current_speed = float(current_preset.get("speed", 1.0))
    current_volume = float(current_preset.get("volume", 0.0))

    target_pitch = int(measured["pitch"])
    target_speed = float(measured["speed"])
    target_volume = float(measured["volume"])

    pitch_delta = target_pitch - current_pitch
    speed_delta = target_speed - current_speed
    volume_delta = target_volume - current_volume

    # Soglie: ignore delta trascurabili
    _PITCH_THRESH = 1     # semitoni
    _SPEED_THRESH = 0.05  # fattore
    _VOL_THRESH = 1.0     # dB

    sugg_pitch = current_pitch + int(pitch_delta * blend) if abs(pitch_delta) >= _PITCH_THRESH else current_pitch
    sugg_speed = current_speed + speed_delta * blend if abs(speed_delta) >= _SPEED_THRESH else current_speed
    sugg_volume = current_volume + volume_delta * blend if abs(volume_delta) >= _VOL_THRESH else current_volume

    suggested = {
        "pitch": max(-12, min(12, sugg_pitch)),
        "speed": round(max(0.5, min(2.0, sugg_speed)), 2),
        "volume": round(max(-12.0, min(12.0, sugg_volume)), 1),
    }

    return {
        "measured": measured,
        "current": {
            "pitch": current_pitch,
            "speed": current_speed,
            "volume": current_volume,
        },
        "delta": {
            "pitch": pitch_delta,
            "speed": speed_delta,
            "volume": volume_delta,
        },
        "suggested": suggested,
    }


def extract_and_save_preset(
    audio_path: str | Path,
    output_json: str | Path,
    accent_name: str = "custom",
    language: str = "it"
) -> dict:
    """Analizza audio e salva preset come JSON.
    
    Args:
        audio_path: File audio
        output_json: Percorso salvataggio JSON
        accent_name: Nome accento
        language: Codice lingua
    
    Returns:
        dict del preset creato
    """
    language_name = LANGUAGE_FULL_NAMES.get(str(language).lower(), str(language).lower())
    preset = create_accent_preset_from_audio(
        audio_path, accent_name, language, language_name
    )
    
    output_json = Path(output_json)
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(preset, indent=2, ensure_ascii=False))
    
    print(f"✓ Preset salvato: {output_json}")
    print(f"  Pitch: {preset['pitch']:+d} semitoni")
    print(f"  Speed: {preset['speed']:.2f}x")
    print(f"  Volume: {preset['volume']:+.1f} dB")
    
    return preset


if __name__ == "__main__":
    # Esempio di utilizzo da linea di comando
    import sys
    
    if len(sys.argv) < 2:
        print("Uso: python audio_inflection_analyzer.py <audio_file> [output.json]")
        sys.exit(1)
    
    audio_file = sys.argv[1]
    output_file = sys.argv[2] if len(sys.argv) > 2 else "accent_preset.json"
    
    preset = extract_and_save_preset(audio_file, output_file)
