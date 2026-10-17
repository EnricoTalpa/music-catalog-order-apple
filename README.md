# Music Catalog — discoteca unica, ordinata, senza doppioni

Uno strumento per **catalogare, ripulire e unire** più librerie musicali in
**una sola cartella** pronta per l'app Musica di Apple (File → Importa),
**senza doppioni** — né nei file né nei metadati — e senza cancellare mai niente.

Nato da una casa reale: la musica era sparsa su un server di rete (condivisione
SMB), su due Mac diversi, e nel cloud di Apple Music — e ogni fonte aveva il
suo problema.

## Cosa risolve

| Problema trovato sul campo | Come lo risolve |
|---|---|
| La stessa canzone presente più volte ("01 Albachiara.mp3", "01 Albachiara 1.mp3", "01 Albachiara 2.mp3"...) | `duplicati`: trova i doppioni per **artista + titolo + album** e tiene solo la copia di qualità migliore |
| Live, studio e versioni in album diversi fusi per errore | la chiave di confronto include l'**album**: "Jumping Jack Flash" a Europa '73 e Atlantic City '89 restano **due brani distinti** |
| Due cartelle da unire in una | `organizza`: copia/sposta nella struttura Apple Music (`Artista/Album/01 Titolo.ext`, compilation e multi-disco gestiti), deduplicando in fase di copia |
| File "invisibili": nomi scritti in Unicode NFC vs NFD | `normalizza_nfc.py` + confronti portati tutti in NFC: i file smettono di sparire |
| L'app Musica che **riscarica dal cloud** i file che mancano, ricreando doppioni e cartelle "resuscitate" | la ricetta: cartella supporti sulla libreria unica, copia-file attiva (finisce sul server, non in locale), **Libreria sincronizzata disattivata** |
| Download cloud interrotti: file di 0-30 KB che "vincono" i confronti perché in formato m4a | `ripara_rotti.py`: trova i file sotto soglia, cerca una copia sana dello stesso brano e la sostituisce |
| Spostamenti via rete lentissimi (mv via SMB, un file alla volta) | `organizza --piano FILE` genera uno script shell **ripetibile** da eseguire dove i file sono locali; `lancia_copie.py` copia in parallelo (~8 MB/s invece di ~1 file/minuto) |

## Principi

- **Anteprima sempre**: ogni comando di default *mostra* e non tocca; serve
  `--esegui` per agire, con conferma
- **Mai cancellare**: tutto ciò che viene tolto va in una cartella di
  revisione (`--revisione DIR`) con un manifest CSV dei percorsi originali,
  ripristinabili a mano
- **Il catalogo è la memoria, non la musica**: un SQLite con i tag letti dai
  file; si ricostruisce con `reset` + `scan` quando serve

## Comandi

```
./run.sh scan /cartella                     # cataloga nel DB (accumula più cartelle)
./run.sh report                             # tag mancanti o sospetti
./run.sh duplicati                          # anteprima dei doppioni (senza toccare nulla)
./run.sh duplicati --revisione DIR --esegui # sposta i doppioni in DIR (manifest CSV)
./run.sh organizza /dest --esegui          # copia in struttura Apple Music, deduplicando
./run.sh organizza /dest --sposta --piano FILE --esegui  # genera script ripetibile
./run.sh reset --esegui                     # svuota il catalogo (i file non si toccano)
./run.sh export [file.csv]                  # esporta il catalogo
```

Strumenti ausiliari:

```
normalizza_nfc.py /cartella [--applica]     # rapporto/normalizzazione nomi NFC vs NFD
ripara_rotti.py /cartella [--esegui]       # sostituisce i file rotti con copie sane
lancia_copie.py piano.sh /prefisso          # copie parallele di un piano (reti lente)
fix_percorsi.py [--inverti]                 # traduce i percorsi del catalogo (server ↔ client)
```

## Installazione

```bash
python3 -m venv venv && venv/bin/pip install -r requirements.txt   # serve mutagen
```

Per i test: `ffmpeg` (genera i file audio di prova).
Python 3.10+ (sviluppato e usato su 3.12 e 3.14).

Test:

```bash
python3 test/test_duplicati.py   # comando duplicati + revisione + manifest
python3 test/test_merge.py       # unione di due cartelle senza doppioni
```

## Nota tecnica: chiave di confronto

Un brano è "lo stesso brano" se coincidono **artista, titolo e album**
(normalizzati: minuscolo, spazi ridotti, Unicode NFC; l'album mancante si
deduce dalla cartella). La scelta della copia da tenere usa un punteggio:
formato (AIFF/CAF/M4A > FLAC > MP3...) + bitrate dal catalogo, e a parità
vince il nome senza numero di traccia. Un file "vincitore" che poi si rivela
rotto (vedi `ripara_rotti.py`) è l'eccezione nota: il formato non dice se il
file è integro.

## Storia in breve

Il progetto è nato per unire la musica di un server di rete e di due Mac in
una discoteca unica. Lungo la strada ha dovuto imparare che: i nomi Unicode
possono nascondersi in due forme; il cloud di Apple Music combatte chi
riordina da sotto (riscarica ciò che sposti); i download interrotti creano
zombie di 20 KB; e le operazioni pesanti vanno fatte dove i file vivono
davvero (sul server), non attraverso due salti di rete.
