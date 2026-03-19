# ClonaVoce Personale

Tool locale per usare solo la propria voce con consenso esplicito, profilo personale e watermark audio/metadata.

## Cosa fa

- crea un profilo vocale personale con attestazione esplicita
- raccoglie campioni WAV o OGG locali da associare al profilo
- genera audio sintetico con controllo di consenso
- aggiunge un piccolo watermark sonoro e un file JSON sidecar con i metadati

## Limiti di sicurezza

- il tool e pensato solo per la propria voce
- senza profilo con consenso confermato la sintesi viene bloccata
- ogni output genera anche un file `.json` con `synthetic_voice: true`
- ogni output WAV viene marcato con un breve tono all'inizio e alla fine

## Motori supportati

- `pyttsx3`: fallback leggero, non clona la voce ma permette di verificare il flusso end-to-end
- `xtts`: opzionale, usa un campione della propria voce se nel sistema e disponibile una libreria compatibile `TTS`

## Avvio rapido

Installazione dipendenza minima per il fallback locale:

```bat
c:\Users\Luke\Desktop\script\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Se hai installato XTTS in `ClonaVoce\\.venv_xtts`, i launcher `.bat` useranno automaticamente quell'ambiente.

Da prompt nella cartella `ClonaVoce`:

```bat
AVVIA_CLONA_VOCE.bat --help
```

Avvio interfaccia grafica:

```bat
AVVIA_CLONA_VOCE_GUI.bat
```

Creazione profilo:

```bat
AVVIA_CLONA_VOCE.bat init-profile --profile luke --display-name "Luke" --i-am-the-speaker
```

Aggiunta campioni WAV o OGG:

```bat
AVVIA_CLONA_VOCE.bat add-sample --profile luke --wav "C:\audio\mio_campione.wav"
```

```bat
AVVIA_CLONA_VOCE.bat add-sample --profile luke --wav "C:\audio\mio_campione.ogg"
```

Stato profilo:

```bat
AVVIA_CLONA_VOCE.bat status --profile luke
```

Sintesi con conferma esplicita:

```bat
AVVIA_CLONA_VOCE.bat synthesize --profile luke --text "Questo e un test" --confirmation-token SELF-VOICE-LUKE
```

Tentativo con motore voce personale opzionale:

```bat
AVVIA_CLONA_VOCE.bat synthesize --profile luke --engine xtts --text-file testo.txt --confirmation-token SELF-VOICE-LUKE
```

## Struttura dati

I dati vengono creati in `ClonaVoce/BIN`:

- `profiles/<profilo>/profile.json`
- `profiles/<profilo>/samples/*.wav`
- `output/*.wav`
- `output/*.json`

## GUI

L'interfaccia grafica permette di:

- creare e selezionare profili
- ordinare i profili per nome in modo stabile (ignorando maiuscole/accenti)
- vedere token e stato del profilo
- modificare il nome visualizzato del profilo (`display_name`) con il pulsante `Modifica nome`
- eliminare un profilo con conferma doppia (`Elimina profilo`) e pulizia di output/preview collegati
- aggiungere campioni WAV o OGG con finestra di selezione file
- importare campioni `.ogg`, convertiti automaticamente in `.wav` dentro il profilo
- creare automaticamente anteprime voce cache (`output/voice_previews`)
- generare audio sintetico scegliendo motore, testo e output
- usare la coda multipla con pulsanti dedicati:
	- `Aggiungi in coda`: prepara un job senza avvio immediato
	- `Avvia coda`: avvia l'elaborazione dei job in coda
	- `Svuota coda`: rimuove solo i job ancora in attesa
- mostrare/nascondere sezioni della tab `Sintesi` (Impostazioni/Coda/Progresso/Note) per mantenere visibili testo e pulsanti principali anche con finestra ridotta
- leggere gli eventi recenti nel pannello log

## Note pratiche

- i campioni possono essere WAV oppure OGG; gli OGG vengono convertiti in WAV nel profilo
- `xtts` non viene installato automaticamente e puo richiedere dipendenze pesanti
- quando presente, `ClonaVoce\\.venv_xtts` viene preferito automaticamente dai launcher CLI/GUI
- se `xtts` non e disponibile, il tool ripiega su `pyttsx3` quando possibile