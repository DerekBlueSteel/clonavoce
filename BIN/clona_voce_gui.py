from __future__ import annotations

import argparse
import shutil
import threading
import traceback
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, simpledialog, ttk

try:
    import winsound
except Exception:  # pragma: no cover - fallback non Windows
    winsound = None

import clona_voce_personale as core


class ClonaVoceApp:
    _SAMPLE_MIN_SECONDS = 3.0
    _SAMPLE_MIN_RATE = 16000
    _SAMPLE_MAX_CHANNELS = 2
    _VOICE_PREVIEW_TEXT = "Ciao, questa e un'anteprima della voce selezionata."

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("ClonaVoce Personale")
        self.root.geometry("980x760")
        self.root.minsize(900, 680)

        core.ensure_dirs()

        self.profile_var = tk.StringVar()
        self.display_name_var = tk.StringVar()
        self.consent_var = tk.BooleanVar(value=True)
        self.engine_var = tk.StringVar(value="auto")
        self.token_var = tk.StringVar()
        self.sample_path_var = tk.StringVar()
        self.output_path_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Nessun profilo selezionato")
        self.quick_text_var = tk.StringVar()
        self.language_var = tk.StringVar(value="Italiano")
        self.mood_var = tk.StringVar(value="neutro")
        self.format_var = tk.StringVar(value="mp3")
        
        self.progress_var = tk.DoubleVar(value=0.0)
        self.progress_status_var = tk.StringVar(value="")
        self.show_synth_controls_var = tk.BooleanVar(value=True)
        self.show_synth_batch_var = tk.BooleanVar(value=True)
        self.show_synth_progress_var = tk.BooleanVar(value=True)
        self.show_synth_notes_var = tk.BooleanVar(value=True)
        self.synth_start_time = None
        self.is_synthesizing = False

        self.preset_var = tk.StringVar(value="professionale")
        self.speed_var = tk.DoubleVar(value=1.0)
        self.pitch_var = tk.DoubleVar(value=0.0)
        self.volume_var = tk.DoubleVar(value=0.0)
        self.accent_var = tk.StringVar(value="italiano_standard")
        self.batch_workers_var = tk.IntVar(value=2)
        self._batch_jobs: list[dict] = []
        self._batch_next_id = 1
        self._batch_active_count = 0
        self._xtts_like_active_count = 0
        self._quick_bar_visible = True
        self._synth_compact_mode = False
        self._last_quick_output: Path | None = None
        self._preview_generation_running = False
        
        self._wrap_labels: list[ttk.Label] = []
        self._build_layout()
        self.refresh_profiles()
        self.format_var.trace_add("write", self._on_format_changed)
        self.preset_var.trace_add("write", self._on_preset_changed)
        self.accent_var.trace_add("write", self._on_accent_changed)
        self._on_preset_changed()
        self.root.bind("<Configure>", self._on_window_resize, add="+")

    def _build_layout(self) -> None:
        outer = ttk.Frame(self.root, padding=14)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(3, weight=1)

        header = ttk.Frame(outer)
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)

        ttk.Label(header, text="ClonaVoce Personale", font=("Segoe UI", 18, "bold")).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(
            header,
            text="Uso locale della propria voce con token di conferma, watermark audio e metadati.",
        ).grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Label(header, textvariable=self.status_var, foreground="#0b5f2a").grid(
            row=2, column=0, sticky="w", pady=(6, 0)
        )

        profile_bar = ttk.LabelFrame(outer, text="Profilo attivo", padding=12)
        profile_bar.grid(row=1, column=0, sticky="ew", pady=(14, 12))
        for column in range(10):
            profile_bar.columnconfigure(column, weight=1 if column == 1 else 0)

        ttk.Label(profile_bar, text="Profilo").grid(row=0, column=0, sticky="w")
        self.profile_combo = ttk.Combobox(profile_bar, textvariable=self.profile_var, state="readonly")
        self.profile_combo.grid(row=0, column=1, sticky="ew", padx=(8, 12))
        self.profile_combo.bind("<<ComboboxSelected>>", lambda _: self.on_profile_selected())

        ttk.Button(profile_bar, text="Aggiorna", command=self.refresh_profiles).grid(row=0, column=2, padx=(0, 8))
        ttk.Button(profile_bar, text="Stato", command=self.show_status).grid(row=0, column=3, sticky="w", padx=(0, 8))
        ttk.Button(profile_bar, text="Aggiungi campione WAV/OGG", command=self.add_sample).grid(row=0, column=4, sticky="w")
        ttk.Button(profile_bar, text="Rimuovi campione", command=self.remove_sample).grid(row=0, column=5, sticky="w", padx=(8, 0))
        ttk.Button(profile_bar, text="Anteprima voce", command=self.play_selected_voice_preview).grid(row=0, column=6, sticky="w", padx=(8, 0))
        ttk.Button(profile_bar, text="Crea sample voci", command=self.build_all_voice_previews).grid(row=0, column=7, sticky="w", padx=(8, 0))
        ttk.Button(profile_bar, text="Modifica nome", command=self.edit_profile_name).grid(row=0, column=8, sticky="w", padx=(8, 0))
        ttk.Button(profile_bar, text="Elimina profilo", command=self.delete_profile).grid(row=0, column=9, sticky="w", padx=(8, 0))

        self._build_quick_synth_bar(outer)

        notebook = ttk.Notebook(outer)
        notebook.grid(row=3, column=0, sticky="nsew")

        profile_tab = ttk.Frame(notebook, padding=14)
        synth_tab = ttk.Frame(notebook, padding=14)
        log_tab = ttk.Frame(notebook, padding=14)
        notebook.add(profile_tab, text="Crea Profilo")
        notebook.add(synth_tab, text="Sintesi")
        notebook.add(log_tab, text="Log")

        self._build_profile_tab(profile_tab)
        self._build_synth_tab(synth_tab)
        self._build_log_tab(log_tab)

    def _on_window_resize(self, event: tk.Event) -> None:
        if event.widget is not self.root:
            return
        wrap = max(300, event.width - 80)
        for lbl in self._wrap_labels:
            lbl.configure(wraplength=wrap)
        self._set_quick_bar_visible(event.height >= 760)
        self._set_synth_compact_mode(event.height < 860)

    def _set_quick_bar_visible(self, visible: bool) -> None:
        if not hasattr(self, "quick_synth_bar"):
            return
        if visible == self._quick_bar_visible:
            return
        if visible:
            self.quick_synth_bar.grid()
        else:
            self.quick_synth_bar.grid_remove()
        self._quick_bar_visible = visible

    def _set_synth_compact_mode(self, compact: bool) -> None:
        required = (
            hasattr(self, "_synth_actions"),
            hasattr(self, "_synth_progress"),
            hasattr(self, "_synth_notes_label"),
            hasattr(self, "_batch_frame"),
        )
        if not all(required):
            return
        if compact == self._synth_compact_mode:
            return

        self._synth_compact_mode = compact
        # In finestra ridotta nasconde automaticamente sezioni secondarie
        # per mantenere sempre visibili testo e pulsanti di generazione.
        if compact:
            self.show_synth_batch_var.set(False)
            self.show_synth_notes_var.set(False)

        if compact:
            self._synth_actions.grid(row=3, column=0, sticky="ew", pady=(0, 6), padx=(0, 4))
            self._synth_progress.grid(row=3, column=1, sticky="ew", pady=(0, 6), padx=(4, 0))
            self._batch_frame.grid(row=2, column=1, sticky="nsew", pady=(0, 10), padx=(6, 0))
            self._synth_notes_label.grid_remove()
        else:
            self._synth_actions.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(0, 8), padx=(0, 0))
            self._synth_progress.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(0, 8), padx=(0, 0))
            self._batch_frame.grid(row=2, column=1, sticky="nsew", pady=(0, 10), padx=(6, 0))
            self._synth_notes_label.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(4, 0))

        self._refresh_synth_section_visibility()

    def _refresh_synth_section_visibility(self) -> None:
        if hasattr(self, "_controls_frame"):
            if self.show_synth_controls_var.get():
                self._controls_frame.grid()
            else:
                self._controls_frame.grid_remove()
        if hasattr(self, "_batch_frame"):
            if self.show_synth_batch_var.get():
                self._batch_frame.grid()
            else:
                self._batch_frame.grid_remove()
        if hasattr(self, "_synth_progress"):
            if self.show_synth_progress_var.get():
                self._synth_progress.grid()
            else:
                self._synth_progress.grid_remove()
        if hasattr(self, "_synth_notes_label"):
            if self.show_synth_notes_var.get() and not self._synth_compact_mode:
                self._synth_notes_label.grid()
            else:
                self._synth_notes_label.grid_remove()

    def _on_format_changed(self, *_) -> None:
        current = self.output_path_var.get().strip()
        if not current:
            return
        from pathlib import Path as _P
        new_ext = "." + (self.format_var.get() or "wav")
        self.output_path_var.set(str(_P(current).with_suffix(new_ext)))

    def _build_default_output_path(self, profile: str, fmt: str) -> str:
        language = core.normalize_language(self.language_var.get().strip() or "Italiano")
        accent = self.accent_var.get().strip() or "italiano_standard"
        return str(core.choose_default_output(profile, fmt=fmt, language=language, accent=accent))

    def _on_preset_changed(self, *_) -> None:
        preset = self.preset_var.get().strip() or "professionale"
        cfg = core.VOICE_PRESETS.get(preset)
        if not cfg:
            return
        # I cursori rappresentano SOLO regolazioni manuali dell'utente.
        # Il preset viene gia applicato nel core durante la sintesi.

    def _on_accent_changed(self, *_) -> None:
        accent = self.accent_var.get().strip() or "italiano_standard"
        cfg = core.ACCENT_PRESETS.get(accent)
        if not cfg:
            return
        if accent == "italiano_standard":
            return
        new_language = core.LANGUAGE_FULL_NAMES.get(cfg.get("language", "it"), "Italiano")
        if new_language != self.language_var.get():
            self.language_var.set(new_language)
            self.log(f"Lingua automaticamente cambiata a '{new_language}' per accento '{accent}'")

    _XTTS_LANGUAGES = [
        "Italiano", "Inglese", "Spagnolo", "Francese", "Tedesco", "Portoghese", "Polacco",
        "Turco", "Russo", "Olandese", "Ceco", "Arabo", "Cinese (Semplificato)",
        "Giapponese", "Coreano", "Ungherese", "Tailandese",
    ]
    _MOODS = ["neutro", "felice", "triste", "arrabbiato", "calmo", "energico"]

    def _build_quick_synth_bar(self, outer: ttk.Frame) -> None:
        bar = ttk.LabelFrame(outer, text="Sintesi rapida", padding=10)
        bar.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        self.quick_synth_bar = bar
        # col 1 (text entry) stretches most; col 6 (button) gets a bit of extra space too
        bar.columnconfigure(1, weight=3)
        bar.columnconfigure(6, weight=1)

        ttk.Label(bar, text="Testo:").grid(row=0, column=0, sticky="w")
        ttk.Entry(bar, textvariable=self.quick_text_var).grid(
            row=0, column=1, sticky="ew", padx=(8, 8)
        )
        ttk.Label(bar, text="Lingua:").grid(row=0, column=2, sticky="w", padx=(0, 4))
        lang_combo = ttk.Combobox(
            bar, textvariable=self.language_var,
            values=self._XTTS_LANGUAGES, width=7, state="readonly"
        )
        lang_combo.grid(row=0, column=3, padx=(0, 8))
        ttk.Label(bar, text="Umore:").grid(row=0, column=4, sticky="w", padx=(0, 4))
        ttk.Combobox(
            bar, textvariable=self.mood_var,
            values=self._MOODS, width=10, state="readonly",
        ).grid(row=0, column=5, padx=(0, 8))
        ttk.Button(bar, text="Genera audio", command=self.quick_synthesize).grid(
            row=0, column=6, sticky="ew"
        )

        player_row = ttk.Frame(bar)
        player_row.grid(row=1, column=0, columnspan=7, sticky="ew", pady=(8, 0))
        player_row.columnconfigure(3, weight=1)
        ttk.Button(player_row, text="Play ultimo", command=self.play_last_generated_audio).grid(row=0, column=0, sticky="w")
        ttk.Button(player_row, text="Stop", command=self.stop_audio_playback).grid(row=0, column=1, sticky="w", padx=(6, 0))
        ttk.Button(player_row, text="Play anteprima voce", command=self.play_selected_voice_preview).grid(row=0, column=2, sticky="w", padx=(6, 0))
        self.quick_player_status = tk.StringVar(value="Nessun audio rapido generato")
        ttk.Label(player_row, textvariable=self.quick_player_status, foreground="#0b5f2a").grid(
            row=0, column=3, sticky="w", padx=(10, 0)
        )

    def quick_synthesize(self) -> None:
        try:
            profile = self.selected_profile()
            text = self.quick_text_var.get().strip()
            if not text:
                raise ValueError("Inserisci un testo da sintetizzare.")
            fmt = "wav"
            output_path = self._build_default_output_path(profile, fmt)
            self.output_path_var.set(output_path)
            
            import time
            self.progress_var.set(0.0)
            self.progress_status_var.set("Avvio sintesi rapida...")
            self.synth_start_time = time.time()
            self.root.update_idletasks()
            
            args = argparse.Namespace(
                profile=profile,
                text=text,
                text_file=None,
                engine=self.engine_var.get().strip() or "auto",
                language=core.normalize_language(self.language_var.get().strip() or "Italiano"),
                mood=self.mood_var.get().strip() or "neutro",
                preset=self.preset_var.get().strip() or "professionale",
                speed=self.speed_var.get(),
                pitch=int(self.pitch_var.get()),
                volume=self.volume_var.get(),
                accent=self.accent_var.get().strip() or "italiano_standard",
                out=output_path,
                confirmation_token=self.token_var.get().strip(),
                progress_callback=self._on_synthesis_progress,
            )
            core.command_synthesize(args)
            self._last_quick_output = Path(output_path)
            self.quick_player_status.set(f"Pronto: {self._last_quick_output.name}")
            self.play_audio_file(self._last_quick_output)
            messagebox.showinfo("ClonaVoce", f"Audio generato in:\n{output_path}")
            self.log(f"Audio generato per {profile}: {output_path}")
            self.output_path_var.set(self._build_default_output_path(profile, fmt))
            self.progress_var.set(0.0)
            self.progress_status_var.set("")
        except Exception as exc:
            messagebox.showerror("ClonaVoce", str(exc))
            self.log(f"Errore sintesi rapida: {exc}")
            self.progress_var.set(0.0)
            self.progress_status_var.set("")

    def stop_audio_playback(self) -> None:
        if winsound is None:
            return
        try:
            winsound.PlaySound(None, winsound.SND_PURGE)
        except Exception:
            pass

    def play_audio_file(self, audio_path: Path) -> None:
        if winsound is None:
            self.quick_player_status.set("Player non disponibile su questo sistema")
            return
        if not audio_path.exists():
            self.quick_player_status.set("Audio non trovato")
            return
        try:
            winsound.PlaySound(str(audio_path), winsound.SND_FILENAME | winsound.SND_ASYNC)
            self.quick_player_status.set(f"In riproduzione: {audio_path.name}")
        except Exception as exc:
            self.quick_player_status.set("Errore player")
            self.log(f"Errore riproduzione audio: {exc}")

    def play_last_generated_audio(self) -> None:
        if not self._last_quick_output:
            messagebox.showwarning("ClonaVoce", "Nessun audio rapido disponibile.")
            return
        self.play_audio_file(self._last_quick_output)

    def _voice_preview_path(self, profile: str) -> Path:
        preview_dir = core.OUTPUT_DIR / "voice_previews"
        preview_dir.mkdir(parents=True, exist_ok=True)
        return preview_dir / f"{core.slugify(profile)}_preview.wav"

    def _ensure_voice_preview(self, profile: str, force_regen: bool = False) -> Path:
        preview_path = self._voice_preview_path(profile)
        if preview_path.exists() and not force_regen:
            return preview_path

        data = core.load_profile(profile)
        token = str(data.get("consent", {}).get("confirmation_token", "")).strip()
        if not token:
            raise ValueError(f"Token mancante per il profilo '{profile}'.")

        args = argparse.Namespace(
            profile=profile,
            text=self._VOICE_PREVIEW_TEXT,
            text_file=None,
            engine="auto",
            language="it",
            mood="neutro",
            preset="professionale",
            speed=1.0,
            pitch=0,
            volume=0.0,
            accent="italiano_standard",
            out=str(preview_path),
            confirmation_token=token,
            progress_callback=None,
        )
        core.command_synthesize(args)
        return preview_path

    def play_selected_voice_preview(self, force_regen: bool = False) -> None:
        if self._preview_generation_running:
            messagebox.showinfo("ClonaVoce", "Generazione anteprima gia in corso.")
            return
        profile = self.profile_var.get().strip()
        if not profile:
            messagebox.showwarning("ClonaVoce", "Seleziona prima un profilo.")
            return

        self._preview_generation_running = True
        self.quick_player_status.set(f"Preparazione anteprima: {profile}...")

        def worker() -> None:
            try:
                preview_path = self._ensure_voice_preview(profile, force_regen=force_regen)
            except Exception as exc:
                self.root.after(0, lambda e=str(exc): self._on_voice_preview_ready(None, e))
                return
            self.root.after(0, lambda p=preview_path: self._on_voice_preview_ready(p, None))

        threading.Thread(target=worker, daemon=True).start()

    def _on_voice_preview_ready(self, preview_path: Path | None, error: str | None) -> None:
        self._preview_generation_running = False
        if error is not None:
            messagebox.showerror("ClonaVoce", error)
            self.quick_player_status.set("Anteprima non disponibile")
            self.log(f"Errore anteprima voce: {error}")
            return
        if preview_path is None:
            return
        self.play_audio_file(preview_path)
        self.log(f"Anteprima voce pronta: {preview_path}")

    def build_all_voice_previews(self) -> None:
        if self._preview_generation_running:
            messagebox.showinfo("ClonaVoce", "Generazione anteprime gia in corso.")
            return
        profiles = core.list_profiles()
        if not profiles:
            messagebox.showwarning("ClonaVoce", "Nessun profilo disponibile.")
            return

        self._preview_generation_running = True
        self.quick_player_status.set("Generazione sample voci in corso...")

        def worker() -> None:
            created = 0
            skipped = 0
            failed = 0
            last_error = ""
            for profile in profiles:
                try:
                    path = self._voice_preview_path(profile)
                    existed = path.exists()
                    self._ensure_voice_preview(profile, force_regen=False)
                    if existed:
                        skipped += 1
                    else:
                        created += 1
                except Exception as exc:
                    failed += 1
                    last_error = str(exc)
                    self.log(f"Errore sample voce '{profile}': {exc}")
            self.root.after(0, lambda: self._on_all_voice_previews_ready(created, skipped, failed, last_error))

        threading.Thread(target=worker, daemon=True).start()

    def _on_all_voice_previews_ready(self, created: int, skipped: int, failed: int, last_error: str) -> None:
        self._preview_generation_running = False
        self.quick_player_status.set("Sample voci aggiornati")
        if failed == 0:
            messagebox.showinfo(
                "ClonaVoce",
                f"Sample pronti. Creati: {created}, gia presenti: {skipped}.",
            )
        else:
            messagebox.showwarning(
                "ClonaVoce",
                f"Completato con errori. Creati: {created}, gia presenti: {skipped}, errori: {failed}. Ultimo errore: {last_error}",
            )

    def _build_profile_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(1, weight=1)
        parent.rowconfigure(5, weight=1)

        ttk.Label(parent, text="ID profilo").grid(row=0, column=0, sticky="w")
        ttk.Entry(parent, textvariable=self.profile_var).grid(row=0, column=1, sticky="ew", padx=(12, 0))

        ttk.Label(parent, text="Nome visualizzato").grid(row=1, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(parent, textvariable=self.display_name_var).grid(
            row=1, column=1, sticky="ew", padx=(12, 0), pady=(10, 0)
        )

        ttk.Checkbutton(
            parent,
            text="Confermo che il profilo corrisponde alla mia voce",
            variable=self.consent_var,
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(12, 0))

        ttk.Button(parent, text="Crea profilo", command=self.create_profile).grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(14, 0)
        )

        info = (
            "Dopo la creazione, aggiungi almeno un campione WAV o OGG se vuoi usare un motore vocale basato su campione. "
            "Il fallback pyttsx3 resta disponibile anche senza campioni."
        )
        _info_lbl = ttk.Label(parent, text=info, wraplength=840, justify="left")
        _info_lbl.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(14, 0))
        self._wrap_labels.append(_info_lbl)

        self.profile_details = scrolledtext.ScrolledText(parent, height=16, wrap="word")
        self.profile_details.grid(row=5, column=0, columnspan=2, sticky="nsew", pady=(14, 0))
        self.profile_details.configure(state="disabled")

    def _build_synth_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=3)
        parent.columnconfigure(1, weight=2)
        parent.rowconfigure(2, weight=1)

        sections_bar = ttk.LabelFrame(parent, text="Sezioni visibili", padding=8)
        sections_bar.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        ttk.Checkbutton(
            sections_bar,
            text="Impostazioni",
            variable=self.show_synth_controls_var,
            command=self._refresh_synth_section_visibility,
        ).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(
            sections_bar,
            text="Coda",
            variable=self.show_synth_batch_var,
            command=self._refresh_synth_section_visibility,
        ).grid(row=0, column=1, sticky="w", padx=(12, 0))
        ttk.Checkbutton(
            sections_bar,
            text="Progresso",
            variable=self.show_synth_progress_var,
            command=self._refresh_synth_section_visibility,
        ).grid(row=0, column=2, sticky="w", padx=(12, 0))
        ttk.Checkbutton(
            sections_bar,
            text="Note",
            variable=self.show_synth_notes_var,
            command=self._refresh_synth_section_visibility,
        ).grid(row=0, column=3, sticky="w", padx=(12, 0))

        controls = ttk.Frame(parent)
        controls.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        self._controls_frame = controls
        controls.columnconfigure(0, weight=1)
        controls.columnconfigure(1, weight=1)

        settings_frame = ttk.LabelFrame(controls, text="Impostazioni", padding=10)
        settings_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 6), pady=(0, 8))
        settings_frame.columnconfigure(1, weight=1)
        settings_frame.columnconfigure(3, weight=1)

        ttk.Label(settings_frame, text="Motore").grid(row=0, column=0, sticky="w")
        ttk.Combobox(
            settings_frame,
            textvariable=self.engine_var,
            state="readonly",
            values=["auto", "pyttsx3", "xtts"],
        ).grid(row=0, column=1, sticky="w", padx=(12, 0))

        ttk.Label(settings_frame, text="Lingua").grid(row=0, column=2, sticky="w", padx=(24, 0))
        ttk.Combobox(
            settings_frame,
            textvariable=self.language_var,
            values=self._XTTS_LANGUAGES,
            width=9,
            state="readonly",
        ).grid(row=0, column=3, sticky="w", padx=(8, 0))

        ttk.Label(settings_frame, text="Umore").grid(row=1, column=0, sticky="w", pady=(10, 0))
        ttk.Combobox(
            settings_frame,
            textvariable=self.mood_var,
            values=self._MOODS,
            width=11,
            state="readonly",
        ).grid(row=1, column=1, sticky="w", padx=(12, 0), pady=(10, 0))

        ttk.Label(settings_frame, text="Formato").grid(row=1, column=2, sticky="w", padx=(24, 0), pady=(10, 0))
        ttk.Combobox(
            settings_frame,
            textvariable=self.format_var,
            values=["wav", "mp3"],
            width=6,
            state="readonly",
        ).grid(row=1, column=3, sticky="w", padx=(8, 0), pady=(10, 0))

        preset_frame = ttk.LabelFrame(controls, text="Preset Vocale e Effetti", padding=10)
        preset_frame.grid(row=1, column=0, sticky="nsew", padx=(0, 6))
        preset_frame.columnconfigure(1, weight=1)

        ttk.Label(preset_frame, text="Preset").grid(row=0, column=0, sticky="w")
        ttk.Combobox(
            preset_frame,
            textvariable=self.preset_var,
            values=list(core.VOICE_PRESETS.keys()),
            state="readonly",
            width=14,
        ).grid(row=0, column=1, sticky="w", padx=(12, 0))

        ttk.Label(preset_frame, text="Velocita").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Scale(preset_frame, from_=0.5, to=2.0, variable=self.speed_var, orient="horizontal").grid(
            row=1, column=1, sticky="ew", padx=(12, 0), pady=(8, 0)
        )
        self.speed_label = ttk.Label(preset_frame, text="1.00x")
        self.speed_label.grid(row=1, column=2, padx=(8, 0), pady=(8, 0))
        self.speed_var.trace_add("write", lambda *_: self.speed_label.config(text=f"{self.speed_var.get():.2f}x"))

        ttk.Label(preset_frame, text="Pitch (semitoni)").grid(row=2, column=0, sticky="w", pady=(8, 0))
        ttk.Scale(preset_frame, from_=-12, to=12, variable=self.pitch_var, orient="horizontal").grid(
            row=2, column=1, sticky="ew", padx=(12, 0), pady=(8, 0)
        )
        self.pitch_label = ttk.Label(preset_frame, text="0")
        self.pitch_label.grid(row=2, column=2, padx=(8, 0), pady=(8, 0))
        self.pitch_var.trace_add("write", lambda *_: self.pitch_label.config(text=f"{int(self.pitch_var.get())}"))

        ttk.Label(preset_frame, text="Volume (dB)").grid(row=3, column=0, sticky="w", pady=(8, 0))
        ttk.Scale(preset_frame, from_=-12, to=12, variable=self.volume_var, orient="horizontal").grid(
            row=3, column=1, sticky="ew", padx=(12, 0), pady=(8, 0)
        )
        self.volume_label = ttk.Label(preset_frame, text="0.0 dB")
        self.volume_label.grid(row=3, column=2, padx=(8, 0), pady=(8, 0))
        self.volume_var.trace_add("write", lambda *_: self.volume_label.config(text=f"{self.volume_var.get():.1f} dB"))

        accent_frame = ttk.LabelFrame(controls, text="Accento Dialettale", padding=10)
        accent_frame.grid(row=0, column=1, sticky="nsew", padx=(6, 0), pady=(0, 8))
        accent_frame.columnconfigure(1, weight=1)

        ttk.Label(accent_frame, text="Accento").grid(row=0, column=0, sticky="w")
        ttk.Combobox(
            accent_frame,
            textvariable=self.accent_var,
            values=list(core.ACCENT_PRESETS.keys()),
            state="readonly",
            width=18,
        ).grid(row=0, column=1, sticky="w", padx=(12, 0))
        ttk.Label(accent_frame, text="(Cambia lingua e modula il tono automaticamente)", foreground="#666").grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(6, 0)
        )
        
        token_output_frame = ttk.LabelFrame(controls, text="Token e Output", padding=10)
        token_output_frame.grid(row=1, column=1, sticky="nsew", padx=(6, 0))
        token_output_frame.columnconfigure(1, weight=1)

        ttk.Label(token_output_frame, text="Token conferma").grid(row=0, column=0, sticky="w")
        ttk.Entry(token_output_frame, textvariable=self.token_var).grid(row=0, column=1, sticky="ew", padx=(12, 0))
        ttk.Button(token_output_frame, text="Carica dal profilo", command=self.load_token_from_profile).grid(
            row=0, column=2, padx=(8, 0)
        )

        ttk.Label(token_output_frame, text="File di output").grid(row=1, column=0, sticky="w", pady=(10, 0))
        output_row = ttk.Frame(token_output_frame)
        output_row.grid(row=1, column=1, sticky="ew", padx=(12, 0), pady=(10, 0))
        output_row.columnconfigure(0, weight=1)
        ttk.Entry(output_row, textvariable=self.output_path_var).grid(row=0, column=0, sticky="ew")
        ttk.Button(output_row, text="Sfoglia", command=self.choose_output_path).grid(row=0, column=1, padx=(8, 0))
        ttk.Button(token_output_frame, text="Auto", command=self.set_default_output_path).grid(
            row=1, column=2, padx=(8, 0), pady=(10, 0)
        )

        text_frame = ttk.LabelFrame(parent, text="Testo da sintetizzare", padding=10, borderwidth=2)
        text_frame.grid(row=2, column=0, sticky="nsew", pady=(0, 10), padx=(0, 6))
        text_frame.columnconfigure(0, weight=1)
        text_frame.rowconfigure(0, weight=1)
        self.text_box = scrolledtext.ScrolledText(text_frame, height=16, wrap="word", font=("Consolas", 10))
        self.text_box.grid(row=0, column=0, sticky="nsew")

        actions = ttk.Frame(parent)
        actions.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        self._synth_actions = actions
        for _col in range(6):
            actions.columnconfigure(_col, weight=1)
        ttk.Button(actions, text="Genera audio", command=self.synthesize_audio).grid(
            row=0, column=0, sticky="ew", padx=(0, 3)
        )
        ttk.Button(actions, text="Aggiungi in coda", command=self.add_current_text_to_batch).grid(
            row=0, column=1, sticky="ew", padx=(3, 3)
        )
        ttk.Button(actions, text="Avvia coda", command=self.start_batch_synthesis).grid(
            row=0, column=2, sticky="ew", padx=(3, 3)
        )
        ttk.Button(actions, text="Svuota coda", command=self.clear_batch_queue).grid(
            row=0, column=3, sticky="ew", padx=(3, 3)
        )
        ttk.Label(actions, text="Paralleli:").grid(row=0, column=4, sticky="e", padx=(3, 3))
        ttk.Spinbox(actions, from_=1, to=6, width=4, textvariable=self.batch_workers_var).grid(
            row=0, column=5, sticky="w", padx=(0, 0)
        )
        ttk.Label(
            actions,
            text=(
                "Aggiungi in coda: prepara un job senza avvio. "
                "Avvia coda: parte la lavorazione dei job in coda. "
                "Svuota coda: rimuove solo i job in attesa."
            ),
            foreground="#666",
        ).grid(row=1, column=0, columnspan=6, sticky="w", pady=(6, 0))

        progress_frame = ttk.LabelFrame(parent, text="Progresso", padding=10)
        progress_frame.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        self._synth_progress = progress_frame
        progress_frame.columnconfigure(0, weight=1)
        ttk.Progressbar(progress_frame, variable=self.progress_var, maximum=100.0, mode="determinate").grid(
            row=0, column=0, sticky="ew"
        )
        ttk.Label(progress_frame, textvariable=self.progress_status_var, foreground="#0066cc").grid(
            row=1, column=0, sticky="w", pady=(6, 0)
        )

        batch_frame = ttk.LabelFrame(parent, text="Coda generazione multipla", padding=10)
        batch_frame.grid(row=2, column=1, sticky="nsew", pady=(0, 10), padx=(6, 0))
        self._batch_frame = batch_frame
        batch_frame.columnconfigure(0, weight=1)
        batch_frame.rowconfigure(1, weight=1)

        ttk.Label(
            batch_frame,
            text="Ogni job usa tutto il contenuto del campo testo. Premi 'Genera audio' per avviare subito anche in parallelo.",
        ).grid(row=0, column=0, sticky="w", pady=(0, 6))

        cols = ("id", "status", "progress", "output", "text")
        self.batch_tree = ttk.Treeview(batch_frame, columns=cols, show="headings", height=8)
        self.batch_tree.grid(row=1, column=0, sticky="nsew")
        self.batch_tree.heading("id", text="#")
        self.batch_tree.heading("status", text="Stato")
        self.batch_tree.heading("progress", text="Progresso")
        self.batch_tree.heading("output", text="Output")
        self.batch_tree.heading("text", text="Testo")
        self.batch_tree.column("id", width=45, stretch=False, anchor="center")
        self.batch_tree.column("status", width=95, stretch=False, anchor="center")
        self.batch_tree.column("progress", width=95, stretch=False, anchor="center")
        self.batch_tree.column("output", width=260, stretch=True)
        self.batch_tree.column("text", width=320, stretch=True)

        notes = (
            "Dialoghi multi-voce: {{voice=silvio;mood=felice;language=it}} Ciao! "
            "{{voice=luca;mood=triste}} Non oggi. {{default}} ritorna al default."
        )
        _notes_lbl = ttk.Label(parent, text=notes, wraplength=840, justify="left")
        _notes_lbl.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        self._synth_notes_label = _notes_lbl
        self._wrap_labels.append(_notes_lbl)
        self._refresh_synth_section_visibility()

    def _build_log_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)
        self.log_box = scrolledtext.ScrolledText(parent, wrap="word")
        self.log_box.grid(row=0, column=0, sticky="nsew")
        self.log_box.configure(state="disabled")

    def log(self, message: str) -> None:
        self.log_box.configure(state="normal")
        self.log_box.insert("end", message.rstrip() + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")
        # Salva anche su file per debug
        try:
            log_file = core.OUTPUT_DIR.parent / "gui_errors.log"
            with open(log_file, "a", encoding="utf-8") as f:
                from datetime import datetime
                timestamp = datetime.now().isoformat()
                f.write(f"[{timestamp}] {message}\n")
        except Exception:
            pass

    def set_profile_details(self, text: str) -> None:
        self.profile_details.configure(state="normal")
        self.profile_details.delete("1.0", "end")
        self.profile_details.insert("1.0", text)
        self.profile_details.configure(state="disabled")

    def selected_profile(self) -> str:
        profile = self.profile_var.get().strip()
        if not profile:
            raise ValueError("Seleziona o inserisci un profilo.")
        return profile

    def refresh_profiles(self) -> None:
        profiles = core.list_profiles()
        self.profile_combo["values"] = profiles
        if self.profile_var.get() not in profiles:
            if profiles:
                self.profile_var.set(profiles[0])
            else:
                self.profile_var.set("")
        self.on_profile_selected(refresh_only=True)
        self.log(f"Profili disponibili: {', '.join(profiles) if profiles else 'nessuno'}")

    def on_profile_selected(self, refresh_only: bool = False) -> None:
        profile = self.profile_var.get().strip()
        if not profile:
            self.status_var.set("Nessun profilo selezionato")
            self.set_profile_details("Nessun profilo disponibile.")
            self.token_var.set("")
            return

        try:
            data = core.load_profile(profile)
        except Exception as exc:
            self.status_var.set(str(exc))
            self.set_profile_details(str(exc))
            return

        self.display_name_var.set(data.get("display_name", ""))
        token = data.get("consent", {}).get("confirmation_token", "")
        self.token_var.set(token)
        summary = self.format_profile_details(data)
        self.set_profile_details(summary)
        sample_count = len(data.get("samples", []))
        self.status_var.set(f"Profilo {data.get('profile')} pronto, campioni audio: {sample_count}")
        if not refresh_only:
            fmt = self.format_var.get() or "wav"
            self.output_path_var.set(self._build_default_output_path(profile, fmt))
            self.log(f"Profilo selezionato: {profile}")

    def format_profile_details(self, data: dict) -> str:
        samples = data.get("samples", [])
        lines = [
            f"Profilo: {data.get('profile')}",
            f"Nome: {data.get('display_name')}",
            f"Consenso confermato: {data.get('consent', {}).get('speaker_confirmed')}",
            f"Token: {data.get('consent', {}).get('confirmation_token')}",
            f"Campioni audio: {len(samples)}",
            f"pyttsx3 disponibile: {core.pyttsx3_available()}",
            f"xtts disponibile: {core.xtts_available()}",
            f"soundfile disponibile: {core.soundfile_available()}",
            "",
        ]
        for index, sample in enumerate(samples, start=1):
            info = sample.get("info", {})
            lines.append(
                f"{index}. {sample.get('filename')} | origine: {sample.get('original_filename', '-')} ({sample.get('original_format', '-')}) | {info.get('duration_seconds')} s | {info.get('framerate')} Hz"
            )
        if not samples:
            lines.append("Nessun campione audio associato al profilo.")
        return "\n".join(lines)

    def create_profile(self) -> None:
        try:
            args = argparse.Namespace(
                profile=self.selected_profile(),
                display_name=self.display_name_var.get().strip(),
                i_am_the_speaker=self.consent_var.get(),
            )
            if not args.display_name:
                raise ValueError("Inserisci un nome visualizzato.")
            core.command_init_profile(args)
            self.refresh_profiles()
            self.on_profile_selected()
            messagebox.showinfo("ClonaVoce", "Profilo creato correttamente.")
            self.log(f"Creato profilo {args.profile}")
            if messagebox.askyesno(
                "ClonaVoce",
                "Vuoi creare subito il sample ascoltabile di questa voce?",
            ):
                self.play_selected_voice_preview(force_regen=True)
        except Exception as exc:
            messagebox.showerror("ClonaVoce", str(exc))
            self.log(f"Errore creazione profilo: {exc}")

    def edit_profile_name(self) -> None:
        try:
            profile = self.selected_profile()
            data = core.load_profile(profile)
            current_name = str(data.get("display_name", "")).strip()
            new_name = simpledialog.askstring(
                "Modifica nome profilo",
                "Inserisci il nuovo nome visualizzato:",
                initialvalue=current_name,
            )
            if new_name is None:
                return
            new_name = new_name.strip()
            if not new_name:
                raise ValueError("Il nome visualizzato non puo essere vuoto.")

            data["display_name"] = new_name
            core.save_profile(profile, data)
            self.display_name_var.set(new_name)
            self.show_status()
            self.log(f"Nome profilo aggiornato: {profile} -> {new_name}")
            messagebox.showinfo("ClonaVoce", "Nome profilo aggiornato correttamente.")
        except Exception as exc:
            messagebox.showerror("ClonaVoce", str(exc))
            self.log(f"Errore modifica nome profilo: {exc}")

    def delete_profile(self) -> None:
        try:
            profile = self.selected_profile()
            data = core.load_profile(profile)
            sample_count = len(data.get("samples", []))

            if not messagebox.askyesno(
                "Elimina profilo",
                (
                    f"Stai per eliminare il profilo '{profile}'.\n"
                    f"Campioni associati: {sample_count}.\n\n"
                    "Verranno rimossi anche output e preview cache del profilo.\n"
                    "Continuare?"
                ),
            ):
                return

            typed = simpledialog.askstring(
                "Conferma eliminazione",
                f"Per confermare, digita esattamente: {profile}",
            )
            if typed is None:
                return
            if typed.strip() != profile:
                raise ValueError("Conferma non valida: nome profilo non corrispondente.")

            profile_path = core.profile_dir(profile)
            output_profile_dir = core.OUTPUT_DIR / profile
            preview_file = core.OUTPUT_DIR / "voice_previews" / f"{profile}_preview.wav"

            if profile_path.exists():
                shutil.rmtree(profile_path, ignore_errors=False)
            if output_profile_dir.exists():
                shutil.rmtree(output_profile_dir, ignore_errors=False)
            if preview_file.exists():
                preview_file.unlink()

            self.refresh_profiles()
            messagebox.showinfo("ClonaVoce", f"Profilo '{profile}' eliminato correttamente.")
            self.log(f"Profilo eliminato: {profile}")
        except Exception as exc:
            messagebox.showerror("ClonaVoce", str(exc))
            self.log(f"Errore eliminazione profilo: {exc}")

    def show_status(self) -> None:
        try:
            profile = self.selected_profile()
            data = core.load_profile(profile)
            self.set_profile_details(self.format_profile_details(data))
            self.status_var.set(f"Profilo {profile} aggiornato")
            self.log(f"Stato aggiornato per {profile}")
        except Exception as exc:
            messagebox.showerror("ClonaVoce", str(exc))
            self.log(f"Errore stato profilo: {exc}")

    def choose_output_path(self) -> None:
        profile = self.profile_var.get().strip() or "profilo"
        fmt = self.format_var.get() or "wav"
        suggested = Path(self._build_default_output_path(profile, fmt))
        if fmt == "mp3":
            ext, ftypes = ".mp3", [("File MP3", "*.mp3")]
        else:
            ext, ftypes = ".wav", [("File WAV", "*.wav")]
        chosen = filedialog.asksaveasfilename(
            title="Salva audio sintetico",
            defaultextension=ext,
            initialdir=str(suggested.parent),
            initialfile=suggested.name,
            filetypes=ftypes,
        )
        if chosen:
            self.output_path_var.set(chosen)

    def set_default_output_path(self) -> None:
        try:
            profile = self.selected_profile()
            fmt = self.format_var.get() or "wav"
            self.output_path_var.set(self._build_default_output_path(profile, fmt))
        except Exception as exc:
            messagebox.showerror("ClonaVoce", str(exc))

    def choose_sample_paths(self) -> list[Path]:
        chosen = filedialog.askopenfilenames(
            title="Seleziona campione audio",
            filetypes=[("File audio supportati", "*.wav *.ogg"), ("File WAV", "*.wav"), ("File OGG", "*.ogg")],
        )
        if not chosen:
            return []
        self.sample_path_var.set("; ".join(chosen))
        return [Path(item) for item in chosen]

    def add_sample(self) -> None:
        try:
            profile = self.selected_profile()
            samples = self.choose_sample_paths()
            if not samples:
                return

            profile_data = core.load_profile(profile)
            existing_originals = {
                str(item.get("original_filename", "")).strip().lower()
                for item in profile_data.get("samples", [])
                if str(item.get("original_filename", "")).strip()
            }
            existing_source_hashes = {
                str(item.get("source_sha256", "")).strip().lower()
                for item in profile_data.get("samples", [])
                if str(item.get("source_sha256", "")).strip()
            }

            added = 0
            failed = 0
            skipped = 0
            selected_hashes: set[str] = set()
            for sample in samples:
                try:
                    source_hash = core.sha256_file(sample).lower()
                    sample_name_key = sample.name.strip().lower()

                    # Skip se già caricato o duplicato nella selezione corrente.
                    if source_hash in existing_source_hashes or source_hash in selected_hashes:
                        skipped += 1
                        self.log(f"Campione saltato (gia caricato): {sample}")
                        continue
                    if sample_name_key in existing_originals:
                        skipped += 1
                        self.log(f"Campione saltato (nome gia presente): {sample}")
                        continue

                    info = core.validate_sample_input(sample)
                    if float(info.get("duration_seconds", 0.0)) < self._SAMPLE_MIN_SECONDS:
                        skipped += 1
                        self.log(
                            f"Campione non idoneo (troppo corto < {self._SAMPLE_MIN_SECONDS:.1f}s): {sample}"
                        )
                        continue
                    if int(info.get("framerate", 0)) < self._SAMPLE_MIN_RATE:
                        skipped += 1
                        self.log(
                            f"Campione non idoneo (sample rate < {self._SAMPLE_MIN_RATE}Hz): {sample}"
                        )
                        continue
                    if int(info.get("channels", 1)) > self._SAMPLE_MAX_CHANNELS:
                        skipped += 1
                        self.log(
                            f"Campione non idoneo (canali > {self._SAMPLE_MAX_CHANNELS}): {sample}"
                        )
                        continue

                    args = argparse.Namespace(profile=profile, wav=str(sample))
                    core.command_add_sample(args)
                    added += 1
                    selected_hashes.add(source_hash)
                    existing_source_hashes.add(source_hash)
                    existing_originals.add(sample_name_key)
                    self.log(f"Campione aggiunto a {profile}: {sample}")
                except Exception as exc:
                    failed += 1
                    self.log(f"Errore aggiunta campione {sample}: {exc}")

            self.show_status()
            if failed == 0:
                messagebox.showinfo(
                    "ClonaVoce",
                    f"Import completato. Aggiunti: {added}, scartati: {skipped}.",
                )
            else:
                messagebox.showwarning(
                    "ClonaVoce",
                    f"Operazione completata con errori. Aggiunti: {added}, scartati: {skipped}, falliti: {failed}. Vedi log.",
                )

            if added > 0 and messagebox.askyesno(
                "ClonaVoce",
                "Campioni caricati. Vuoi creare/aggiornare ora il sample ascoltabile di questa voce?",
            ):
                self.play_selected_voice_preview(force_regen=True)
        except Exception as exc:
            messagebox.showerror("ClonaVoce", str(exc))
            self.log(f"Errore aggiunta campione: {exc}")

    def remove_sample(self) -> None:
        try:
            profile = self.selected_profile()
            data = core.load_profile(profile)
            samples = data.get("samples", [])
            if not samples:
                raise ValueError("Nessun campione disponibile da rimuovere.")

            preview = []
            for index, sample in enumerate(samples[-15:], start=max(1, len(samples) - 14)):
                preview.append(f"{index}) {sample.get('filename')} <- {sample.get('original_filename', '-')}")
            prompt = (
                "Inserisci indici o filename separati da virgola (es: 1,3 oppure sample_...wav).\n"
                "Scrivi 'all' per rimuovere tutti i campioni.\n\n"
                "Campioni recenti:\n" + "\n".join(preview)
            )
            default_value = str(len(samples))
            value = simpledialog.askstring("Rimuovi campione", prompt, initialvalue=default_value)
            if value is None:
                return
            raw = value.strip()
            if not raw:
                return

            if raw.lower() == "all":
                args = argparse.Namespace(profile=profile, sample=[], all=True, keep_files=False)
            else:
                tokens = [item.strip() for item in raw.split(",") if item.strip()]
                args = argparse.Namespace(profile=profile, sample=tokens, all=False, keep_files=False)

            core.command_remove_sample(args)
            self.show_status()
            messagebox.showinfo("ClonaVoce", "Campioni rimossi correttamente.")
            self.log(f"Campioni rimossi da {profile}: {raw}")
        except Exception as exc:
            messagebox.showerror("ClonaVoce", str(exc))
            self.log(f"Errore rimozione campioni: {exc}")

    def load_token_from_profile(self) -> None:
        try:
            profile = self.selected_profile()
            data = core.load_profile(profile)
            token = data.get("consent", {}).get("confirmation_token", "")
            self.token_var.set(token)
            self.log(f"Token caricato per {profile}")
        except Exception as exc:
            messagebox.showerror("ClonaVoce", str(exc))

    def _on_synthesis_progress(self, percent: float, message: str) -> None:
        """Callback per aggiornare la barra di progresso e il tempo restante."""
        import time
        
        self.progress_var.set(percent)
        
        if percent == 1.0 and self.synth_start_time:
            elapsed = time.time() - self.synth_start_time
            self.progress_status_var.set(f"100% | {message}")
        elif percent > 1.0 and self.synth_start_time:
            elapsed = time.time() - self.synth_start_time
            rate = percent / elapsed if elapsed > 0 else 1.0
            remaining_percent = 100.0 - percent
            eta_seconds = remaining_percent / rate if rate > 0 else 0
            
            if eta_seconds > 60:
                eta_str = f"{int(eta_seconds // 60)}m {int(eta_seconds % 60)}s"
            else:
                eta_str = f"{int(eta_seconds)}s"
            
            self.progress_status_var.set(f"{percent:.0f}% | {message} | ETA {eta_str}")
        else:
            self.progress_status_var.set(f"{percent:.0f}% | {message}")
        
        self.root.update_idletasks()

    def synthesize_audio(self) -> None:
        try:
            job = self._create_job_from_current_text()
            self._append_job_to_queue(job)
            self._dispatch_queued_jobs()
            self.log(f"Job #{job['id']} accodato e avviato: voce={job['profile']}")
        except Exception as exc:
            messagebox.showerror("ClonaVoce", str(exc))
            self.log(f"Errore sintesi: {exc}")
            self.progress_var.set(0.0)
            self.progress_status_var.set("")
            self.is_synthesizing = False

    def _create_job_from_current_text(self) -> dict:
        profile = self.selected_profile()
        text = self.text_box.get("1.0", "end").strip()
        if not text:
            raise ValueError("Inserisci un testo da sintetizzare.")

        fmt = (self.format_var.get() or "wav").lower()
        token = self.token_var.get().strip()
        engine = self.engine_var.get().strip() or "auto"
        language = core.normalize_language(self.language_var.get().strip() or "Italiano")
        mood = self.mood_var.get().strip() or "neutro"
        preset = self.preset_var.get().strip() or "professionale"
        accent = self.accent_var.get().strip() or "italiano_standard"
        speed = self.speed_var.get()
        pitch = int(self.pitch_var.get())
        volume = self.volume_var.get()

        # Blocco duplicati solo se coincidenti su voce + testo + parametri.
        text_key = " ".join(text.split())
        params_key = (
            engine,
            language,
            mood,
            preset,
            accent,
            round(float(speed), 4),
            int(pitch),
            round(float(volume), 4),
        )
        for job in self._batch_jobs:
            if (
                job.get("profile") == profile
                and job.get("text_key") == text_key
                and job.get("params_key") == params_key
                and job.get("status") in {"queued", "running", "done"}
            ):
                raise ValueError(
                    "Esiste gia una generazione identica (stessa voce, testo e parametri)."
                )

        job_id = self._batch_next_id
        self._batch_next_id += 1
        base = Path(self._build_default_output_path(profile, fmt))
        output_path = str(base.with_name(f"{base.stem}_job{job_id:03d}{base.suffix}"))

        return {
            "id": job_id,
            "profile": profile,
            "text": text,
            "text_key": text_key,
            "params_key": params_key,
            "output": output_path,
            "status": "queued",
            "progress": 0.0,
            "engine": engine,
            "language": language,
            "mood": mood,
            "preset": preset,
            "accent": accent,
            "speed": speed,
            "pitch": pitch,
            "volume": volume,
            "token": token,
            # XTTS (e auto che tipicamente seleziona XTTS) non e thread-safe in parallelo.
            "requires_exclusive_tts": engine in {"xtts", "auto"},
        }

    def _append_job_to_queue(self, job: dict) -> None:
        self._batch_jobs.append(job)
        preview = job["text"][:70] + ("..." if len(job["text"]) > 70 else "")
        self.batch_tree.insert(
            "",
            "end",
            iid=f"job_{job['id']}",
            values=(job["id"], "In coda", "0%", job["output"], preview),
        )

    def add_current_text_to_batch(self) -> None:
        try:
            job = self._create_job_from_current_text()
            self._append_job_to_queue(job)
            self.log(f"Coda: aggiunto job #{job['id']} per profilo '{job['profile']}'.")
        except Exception as exc:
            messagebox.showerror("ClonaVoce", str(exc))

    def clear_batch_queue(self) -> None:
        if self._batch_active_count > 0:
            messagebox.showwarning("ClonaVoce", "Ci sono job in esecuzione: posso svuotare solo quelli in coda.")
        queued_ids = {job["id"] for job in self._batch_jobs if job.get("status") == "queued"}
        self._batch_jobs = [job for job in self._batch_jobs if job.get("status") != "queued"]
        for job_id in queued_ids:
            iid = f"job_{job_id}"
            if iid in self.batch_tree.get_children():
                self.batch_tree.delete(iid)
        self.log("Coda multipla: rimossi i job non ancora avviati.")

    def _update_batch_job_row(self, job: dict, status: str, percent: float | None = None) -> None:
        job["status"] = status
        if percent is not None:
            job["progress"] = float(percent)
        iid = f"job_{job['id']}"
        if iid not in self.batch_tree.get_children():
            return
        status_map = {
            "queued": "In coda",
            "running": "In corso",
            "done": "Completato",
            "error": "Errore",
        }
        label = status_map.get(status, status)
        preview = job["text"][:70] + ("..." if len(job["text"]) > 70 else "")
        self.batch_tree.item(
            iid,
            values=(job["id"], label, f"{job['progress']:.0f}%", job["output"], preview),
        )

    def _run_single_batch_job(self, job: dict) -> None:
        def on_progress(percent: float, _message: str) -> None:
            clamped = max(0.0, min(100.0, float(percent)))
            self.root.after(0, lambda: self._update_batch_job_row(job, "running", clamped))

        args = argparse.Namespace(
            profile=job["profile"],
            text=job["text"],
            text_file=None,
            engine=job["engine"],
            language=job["language"],
            mood=job["mood"],
            preset=job["preset"],
            speed=job["speed"],
            pitch=job["pitch"],
            volume=job["volume"],
            accent=job["accent"],
            out=job["output"],
            confirmation_token=job["token"],
            progress_callback=on_progress,
        )
        core.command_synthesize(args)

    def _on_batch_job_finished(self, job: dict, error: str | None = None) -> None:
        if error is None:
            self._update_batch_job_row(job, "done", 100.0)
            self.log(f"Job #{job['id']} completato: {job['output']}")
        else:
            self._update_batch_job_row(job, "error", job.get("progress", 0.0))
            self.log(f"Job #{job['id']} errore: {error}")

        self._batch_active_count = max(0, self._batch_active_count - 1)
        if job.get("requires_exclusive_tts"):
            self._xtts_like_active_count = max(0, self._xtts_like_active_count - 1)
        self.progress_status_var.set(f"Attivi: {self._batch_active_count} | In coda: {sum(1 for j in self._batch_jobs if j['status'] == 'queued')}")
        self._dispatch_queued_jobs()

    def _start_job_thread(self, job: dict) -> None:
        self._update_batch_job_row(job, "running", 0.0)
        self._batch_active_count += 1
        if job.get("requires_exclusive_tts"):
            self._xtts_like_active_count += 1

        def worker() -> None:
            try:
                self._run_single_batch_job(job)
            except Exception as exc:
                err_msg = f"Job #{job['id']} errore thread:\n{traceback.format_exc()}"
                self.root.after(0, lambda msg=err_msg: self.log(msg))
                self.root.after(0, lambda e=str(exc), j=job: self._on_batch_job_finished(j, e))
                return
            self.root.after(0, lambda j=job: self._on_batch_job_finished(j, None))

        threading.Thread(target=worker, daemon=True).start()

    def _dispatch_queued_jobs(self) -> None:
        workers = max(1, min(6, int(self.batch_workers_var.get() or 1)))
        while self._batch_active_count < workers:
            next_job = next(
                (
                    job
                    for job in self._batch_jobs
                    if job.get("status") == "queued"
                    and not (job.get("requires_exclusive_tts") and self._xtts_like_active_count > 0)
                ),
                None,
            )
            if next_job is None:
                break
            self._start_job_thread(next_job)

        self.progress_status_var.set(f"Attivi: {self._batch_active_count} | In coda: {sum(1 for j in self._batch_jobs if j['status'] == 'queued')}")

    def start_batch_synthesis(self) -> None:
        pending = [job for job in self._batch_jobs if job["status"] == "queued"]
        if not pending:
            messagebox.showwarning("ClonaVoce", "Nessun job in coda da avviare.")
            return
        self.log(f"Avvio coda multipla: {len(pending)} job in attesa.")
        self._dispatch_queued_jobs()


def main() -> int:
    root = tk.Tk()
    style = ttk.Style(root)
    if "vista" in style.theme_names():
        style.theme_use("vista")
    app = ClonaVoceApp(root)
    app.set_default_output_path()
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())