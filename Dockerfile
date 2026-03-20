FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV MPLCONFIGDIR=/app/ClonaVoce/.mplcache

WORKDIR /app/ClonaVoce

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        espeak-ng \
        libespeak-ng-dev \
        libsndfile1 \
    && ln -sf /usr/lib/aarch64-linux-gnu/libespeak-ng.so.1 /usr/lib/libespeak.so.1 || \
       ln -sf $(find /usr/lib -name 'libespeak-ng.so.1' | head -1) /usr/lib/libespeak.so.1 || true \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-deploy.txt ./
RUN pip install --no-cache-dir -r requirements-deploy.txt

COPY BIN ./BIN
COPY AVVIA_CLONA_VOCE.bat ./AVVIA_CLONA_VOCE.bat
COPY AVVIA_CLONA_VOCE_GUI.bat ./AVVIA_CLONA_VOCE_GUI.bat

RUN mkdir -p output BIN/profiles .mplcache

# Pre-scaricare il modello whisper tiny durante il build per evitare download a runtime.
# Usa CLONAVOCE_TRANSCRIBE_MODEL=base per qualita superiore (piu lento).
ENV CLONAVOCE_TRANSCRIBE_MODEL=tiny
RUN python -c "import faster_whisper; faster_whisper.WhisperModel('tiny', compute_type='int8'); print('Modello whisper tiny precaricato.')"

EXPOSE 8000

CMD ["uvicorn", "clona_voce_service:app", "--app-dir", "BIN", "--host", "0.0.0.0", "--port", "8000"]
