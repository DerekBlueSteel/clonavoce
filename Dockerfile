FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV MPLCONFIGDIR=/app/ClonaVoce/.mplcache

WORKDIR /app/ClonaVoce

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        espeak-ng \
        libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-deploy.txt ./
RUN pip install --no-cache-dir -r requirements-deploy.txt

COPY BIN ./BIN
COPY AVVIA_CLONA_VOCE.bat ./AVVIA_CLONA_VOCE.bat
COPY AVVIA_CLONA_VOCE_GUI.bat ./AVVIA_CLONA_VOCE_GUI.bat

RUN mkdir -p output BIN/profiles .mplcache

EXPOSE 8000

CMD ["uvicorn", "clona_voce_service:app", "--app-dir", "BIN", "--host", "0.0.0.0", "--port", "8000"]
