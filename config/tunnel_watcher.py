"""
tunnel_watcher.py  –  ClonaVoce PC-side watchdog
==================================================
Gira in background sul PC e mantiene allineato Render con il tunnel cloudflared.

Ciclo ogni CHECK_INTERVAL secondi:
  1. Legge l'URL corrente del tunnel dal log di cloudflared
  2. Legge la posizione in cui Render punta (GET /internal/tunnel-refresh-needed
     + remote_xtts_url_preview da /health/private)
  3. Se l'URL locale ≠ quello su Render → chiama render_sync.py → deploy
  4. Se Render ha segnalato "refresh richiesto" dall'app → forza sync anche se
     gli URL sembrano uguali (il tunnel potrebbe rispondere male a quella URL)
  5. Verifica che il tunnel risponda; se no → avvisa e aspetta nuovo URL

Struttura prevista nella cartella config/:
  xtts_local.env          ← config con credenziali e URL
  cloudflared_tunnel.log  ← scritto da cloudflared in esecuzione
  render_sync.py          ← script che aggiorna env vars su Render + avvia deploy
  tunnel_watcher.log      ← log del watcher (ruota dopo 500 KB)

Lanciare con:
  python config/tunnel_watcher.py          (dalla root del progetto ClonaVoce)
oppure tramite AVVIA_TUNNEL_WATCHER.bat
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

# ── Config ────────────────────────────────────────────────────────────────────
BASE_DIR       = pathlib.Path(__file__).parent
ENV_FILE       = BASE_DIR / "xtts_local.env"
TUNNEL_LOG     = BASE_DIR / "cloudflared_tunnel.log"
RENDER_SYNC    = BASE_DIR / "render_sync.py"
WATCHER_LOG    = BASE_DIR / "tunnel_watcher.log"
RENDER_BASE    = "https://clonavoce.onrender.com"

CHECK_INTERVAL           = 30      # secondi tra un ciclo e l'altro
LOG_MAX_BYTES            = 500_000 # ruota watcher.log dopo ~500 KB
DEAD_TUNNEL_RESTART_SECS = 300     # 5 min: se tunnel morto da questo tempo → restart
TUNNEL_URL_WAIT_SECS     = 90      # secondi max per attendere nuovo URL dopo restart
LOCAL_PORT               = 8010    # porta locale server XTTS

_URL_PATTERN = re.compile(r'https://[\w-]+\.trycloudflare\.com')


# ── Helpers ───────────────────────────────────────────────────────────────────

def _log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        if WATCHER_LOG.exists() and WATCHER_LOG.stat().st_size > LOG_MAX_BYTES:
            bak = WATCHER_LOG.with_suffix(".log.bak")
            WATCHER_LOG.rename(bak)
        with WATCHER_LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    if not ENV_FILE.exists():
        return env
    for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip()
    return env


def _normalize(url: str) -> str:
    u = str(url or "").strip()
    while u.endswith("/"):
        u = u[:-1]
    return u


def _current_tunnel_url() -> str:
    """Estrae l'URL trycloudflare.com più recente dal log di cloudflared."""
    if not TUNNEL_LOG.exists():
        return ""
    try:
        text = TUNNEL_LOG.read_text(encoding="utf-8", errors="replace")
        matches = _URL_PATTERN.findall(text)
        return matches[-1] if matches else ""
    except Exception:
        return ""


def _tunnel_alive(tunnel_url: str) -> bool:
    if not tunnel_url:
        return False
    health = tunnel_url.rstrip("/") + "/health"
    try:
        req = urllib.request.Request(health, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status == 200
    except Exception:
        return False


def _render_get(path: str, api_key: str) -> dict:
    headers = {"Accept": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    try:
        req = urllib.request.Request(f"{RENDER_BASE}{path}", headers=headers)
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode("utf-8", "replace")) or {}
    except urllib.error.HTTPError as exc:
        return {"_http_error": exc.code}
    except Exception as exc:
        return {"_network_error": str(exc)}


def _render_post(path: str, api_key: str, body: dict) -> dict:
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    try:
        data = json.dumps(body).encode()
        req = urllib.request.Request(f"{RENDER_BASE}{path}", data=data,
                                     headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode("utf-8", "replace")) or {}
    except Exception as exc:
        return {"_error": str(exc)}


def _render_current_url(health_private: dict) -> str:
    """Estrae l'URL RemoteXTTS che Render sta usando dal payload health/private."""
    preview = str(health_private.get("pc_health_url_preview") or "").strip()
    # pc_health_url_preview punta a .../health; converti in .../synthesize
    if preview.endswith("/health"):
        return preview[: -len("/health")] + "/synthesize"
    # Altrimenti usa remote_xtts_url_preview se presente
    alt = str(health_private.get("remote_xtts_url_preview") or "").strip()
    return alt or ""


def _kill_cloudflared() -> bool:
    """Termina tutti i processi cloudflared in esecuzione (Windows taskkill)."""
    try:
        result = subprocess.run(
            ["taskkill", "/F", "/IM", "cloudflared.exe", "/T"],
            capture_output=True, timeout=15,
        )
        ok = result.returncode == 0
        _log(f"[RESTART] taskkill cloudflared: {'OK' if ok else 'nessun processo trovato'}")
        return ok
    except Exception as exc:
        _log(f"[RESTART] taskkill eccezione: {exc}")
        return False


def _find_cloudflared_cmd(env: dict) -> str:
    """Trova il binario cloudflared: via env oppure nel PATH."""
    fixed = env.get("CLOUDFLARED_CMD", "").strip()
    if fixed and pathlib.Path(fixed).exists():
        return fixed
    # Cerca nella cartella tools/ vicino al progetto (padre di BASE_DIR)
    for candidate in [
        BASE_DIR.parent / "tools" / "cloudflared.exe",
        BASE_DIR.parent / "tools" / "cloudflared",
        pathlib.Path("cloudflared"),
    ]:
        try:
            path = shutil.which(str(candidate)) or (str(candidate) if pathlib.Path(str(candidate)).exists() else None)
            if path:
                return path
        except Exception:
            pass
    return "cloudflared"  # fallback: deve essere nel PATH


def _restart_cloudflared(env: dict) -> bool:
    """
    Uccide cloudflared, ruota il log e avvia una nuova istanza quick tunnel.
    Attende fino a TUNNEL_URL_WAIT_SECS che nel log appaia un nuovo URL.
    Ritorna True se un nuovo URL è stato trovato.
    """
    port = env.get("CLONAVOCE_REMOTE_PORT", str(LOCAL_PORT)).strip() or str(LOCAL_PORT)
    cf_cmd = _find_cloudflared_cmd(env)
    _log(f"[RESTART] Avvio restart cloudflared (porta {port}, cmd: {cf_cmd})")

    # 1. Termina il vecchio processo
    _kill_cloudflared()
    time.sleep(2)

    # 2. Ruota il vecchio log
    if TUNNEL_LOG.exists():
        bak = TUNNEL_LOG.with_suffix(".log.bak")
        try:
            TUNNEL_LOG.rename(bak)
            _log(f"[RESTART] Vecchio log spostato in {bak.name}")
        except Exception as exc:
            _log(f"[RESTART] Impossibile ruotare log: {exc}")
            try:
                TUNNEL_LOG.unlink()
            except Exception:
                pass

    # 3. Avvia nuovo processo cloudflared in background
    _log(f"[RESTART] Avvio: {cf_cmd} tunnel --url http://127.0.0.1:{port}")
    try:
        log_handle = TUNNEL_LOG.open("w", encoding="utf-8")
        subprocess.Popen(
            [cf_cmd, "tunnel", "--url", f"http://127.0.0.1:{port}"],
            stdout=log_handle,
            stderr=log_handle,
            close_fds=True,
        )
        _log("[RESTART] Processo cloudflared avviato.")
    except Exception as exc:
        _log(f"[RESTART] Impossibile avviare cloudflared: {exc}")
        return False

    # 4. Attendi nuovo URL nel log
    _log(f"[RESTART] Attendo nuovo URL (max {TUNNEL_URL_WAIT_SECS}s)…")
    deadline = time.time() + TUNNEL_URL_WAIT_SECS
    while time.time() < deadline:
        time.sleep(3)
        url = _current_tunnel_url()
        if url:
            _log(f"[RESTART] Nuovo URL rilevato: {url}")
            # Aggiorna xtts_local.env con il nuovo url
            _update_env_url(url)
            return True

    _log("[RESTART] Timeout: nessun URL nel log dopo il restart.")
    return False


def _update_env_url(tunnel_url: str) -> None:
    """Aggiorna CLOUDFLARED_PUBLIC_URL e CLONAVOCE_REMOTE_XTTS_URL in xtts_local.env."""
    synth_url = tunnel_url.rstrip("/") + "/synthesize"
    if not ENV_FILE.exists():
        return
    try:
        lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
        new_lines = []
        found_pub = found_synth = False
        for ln in lines:
            k = ln.split("=", 1)[0].strip()
            if k == "CLOUDFLARED_PUBLIC_URL":
                new_lines.append(f"CLOUDFLARED_PUBLIC_URL={tunnel_url}")
                found_pub = True
            elif k == "CLONAVOCE_REMOTE_XTTS_URL":
                new_lines.append(f"CLONAVOCE_REMOTE_XTTS_URL={synth_url}")
                found_synth = True
            else:
                new_lines.append(ln)
        if not found_pub:
            new_lines.append(f"CLOUDFLARED_PUBLIC_URL={tunnel_url}")
        if not found_synth:
            new_lines.append(f"CLONAVOCE_REMOTE_XTTS_URL={synth_url}")
        ENV_FILE.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
        _log(f"[ENV] xtts_local.env aggiornato: URL={synth_url}")
    except Exception as exc:
        _log(f"[ENV] Impossibile aggiornare xtts_local.env: {exc}")



    """Lancia render_sync.py con il nuovo URL e aspetta la fine."""
    if not RENDER_SYNC.exists():
        _log(f"[ERRORE] render_sync.py non trovato: {RENDER_SYNC}")
        return False
    python = sys.executable
    _log(f"[SYNC] Eseguo render_sync.py con URL={new_synth_url} ...")
    try:
        result = subprocess.run(
            [python, str(RENDER_SYNC), new_synth_url],
            capture_output=False,
            timeout=700,  # Deploy Render può richiedere ~10 minuti
        )
        ok = result.returncode == 0
        _log(f"[SYNC] render_sync.py terminato: returncode={result.returncode} ({'OK' if ok else 'WARN'})")
        return ok
    except subprocess.TimeoutExpired:
        _log("[SYNC] render_sync.py timeout (>700s) – Render deploy troppo lento?")
        return False
    except Exception as exc:
        _log(f"[SYNC] render_sync.py eccezione: {exc}")
        return False


# ── Main loop ─────────────────────────────────────────────────────────────────

def main() -> None:
    _log("=" * 60)
    _log("tunnel_watcher avviato")
    _log(f"  ENV_FILE    : {ENV_FILE}")
    _log(f"  TUNNEL_LOG  : {TUNNEL_LOG}")
    _log(f"  CHECK       : ogni {CHECK_INTERVAL}s")
    _log(f"  DEAD→RESTART: dopo {DEAD_TUNNEL_RESTART_SECS}s")
    _log("=" * 60)

    last_synced_url:   str   = ""
    last_check_ts:     float = 0.0
    tunnel_dead_since: float = 0.0   # timestamp da quando il tunnel è morto

    while True:
        now = time.time()
        if (now - last_check_ts) < CHECK_INTERVAL:
            time.sleep(1)
            continue
        last_check_ts = now

        env = _load_env()
        app_api_key = env.get("CLONAVOCE_REMOTE_XTTS_KEY", "").strip()

        # 1. URL tunnel locale attuale
        tunnel_url = _current_tunnel_url()
        synth_url  = (tunnel_url.rstrip("/") + "/synthesize") if tunnel_url else ""

        if not tunnel_url:
            _log("[WARN] Nessun URL cloudflared nel log — tunnel non ancora avviato?")
            continue

        # 2. Chiedi a Render lo stato del collegamento + flag refresh
        health = _render_get("/health/private", app_api_key)
        if "_network_error" in health or "_http_error" in health:
            _log(f"[WARN] Render /health/private non raggiungibile: {health}")
            continue

        render_url      = _normalize(_render_current_url(health))
        local_synth_url = _normalize(synth_url)
        pc_status       = str(health.get("pc_link_status") or "").lower()

        # 3. Controlla il flag "refresh richiesto" dall'app
        refresh_info = _render_get("/internal/tunnel-refresh-needed", app_api_key)
        app_requested_refresh = bool(refresh_info.get("pending"))
        if app_requested_refresh:
            _log("[SIGNAL] App ha segnalato PC offline su Render → forzato controllo tunnel")

        # 4. Verifica salute tunnel
        tunnel_alive = _tunnel_alive(tunnel_url)
        if tunnel_alive:
            tunnel_dead_since = 0.0  # reset: tunnel vivo
        else:
            if tunnel_dead_since == 0.0:
                tunnel_dead_since = now
                _log(f"[DEAD] Tunnel non risponde. Inizio conteggio ({DEAD_TUNNEL_RESTART_SECS}s prima del restart).")
            dead_secs = now - tunnel_dead_since
            _log(f"[DEAD] Tunnel morto da {dead_secs:.0f}s / {DEAD_TUNNEL_RESTART_SECS}s")
            if dead_secs >= DEAD_TUNNEL_RESTART_SECS or app_requested_refresh:
                _log("[RESTART] Soglia raggiunta — riavvio cloudflared...")
                got_new = _restart_cloudflared(env)
                tunnel_dead_since = 0.0
                if got_new:
                    # Rileggi il nuovo URL e ricomincia il ciclo
                    new_url = _current_tunnel_url()
                    if new_url:
                        new_synth = new_url.rstrip("/") + "/synthesize"
                        _log(f"[RESTART] Eseguo sync Render con nuovo URL: {new_synth}")
                        ok = _run_render_sync(new_synth)
                        if ok:
                            last_synced_url = _normalize(new_synth)
                            _log(f"[OK] Render aggiornato a: {new_synth}")
                else:
                    _log("[RESTART] Nuovo URL non trovato dopo restart. Riprovo al prossimo ciclo.")
            continue  # Non sincronizziamo con URL morto

        # 5. Decide se serve un sync con URL vivo
        url_mismatch   = render_url and local_synth_url and (render_url != local_synth_url)
        already_synced = (local_synth_url == last_synced_url)

        need_sync = (
            url_mismatch
            or app_requested_refresh
            or (pc_status in {"offline", "not_configured"} and not already_synced)
        )

        _log(
            f"[CHECK] tunnel={tunnel_url} alive={tunnel_alive} "
            f"pc_status={pc_status or '?'} "
            f"render_url={render_url or '?'} "
            f"mismatch={url_mismatch} app_signal={app_requested_refresh} "
            f"need_sync={need_sync}"
        )

        if not need_sync:
            continue

        # 6. Esegui il sync
        _log(f"[SYNC] Avvio sync: {render_url or '(nessuno)'} → {local_synth_url}")
        ok = _run_render_sync(local_synth_url)
        if ok:
            last_synced_url = local_synth_url
            _log(f"[OK] Sync completato. Render aggiornato a: {local_synth_url}")
        else:
            _log("[WARN] Sync non completato. Riproverò al prossimo ciclo.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[tunnel_watcher] Interrotto dall'utente.")
