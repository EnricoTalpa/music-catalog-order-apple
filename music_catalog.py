#!/usr/bin/env python3
"""
Music Catalog — Cataloga, analizza e corregge i metadati dei brani audio.

Comandi:
    scan      /cartella       Scansiona e cataloga tutti i brani nel database
    report                    Mostra i brani con tag mancanti o sospetti
    duplicati [--revisione]   Trova i doppioni nella libreria (anteprima di default)
    lookup    [--acoustid]    Cerca info corrette su MusicBrainz / AcoustID
    fix                       Mostra le correzioni proposte e le applica (con conferma)
    organizza /destinazione   Copia/sposta i brani nella struttura Apple Music
    reset                     Svuota il catalogo (i file non vengono toccati)
    export    [file.csv]      Esporta il catalogo in CSV

Requisiti:
    pip3 install mutagen musicbrainzngs pyacoustid
    brew install chromaprint   # (per AcoustID fingerprint)
"""

import os
import sys
import re
import time
import sqlite3
import argparse
import subprocess
import csv
import shutil
import unicodedata
from datetime import datetime
from pathlib import Path
from collections import defaultdict

# ---------------------------------------------------------------------------
# Import opzionali con messaggi chiari
# ---------------------------------------------------------------------------
try:
    import mutagen
    from mutagen import File as MutagenFile
    HAS_MUTAGEN = True
except ImportError:
    HAS_MUTAGEN = False

try:
    import musicbrainzngs
    musicbrainzngs.set_useragent("MusicCatalog", "2.0", "https://github.com/example")
    HAS_MUSICBRAINZ = True
except ImportError:
    HAS_MUSICBRAINZ = False

try:
    import acoustid
    HAS_ACOUSTID = True
except ImportError:
    HAS_ACOUSTID = False

# ---------------------------------------------------------------------------
# Configurazione
# ---------------------------------------------------------------------------
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "music_catalog.db")

ESTENSIONI_AUDIO = {
    ".mp3", ".m4a", ".aac", ".flac", ".wav",
    ".aiff", ".aif", ".ogg", ".wma", ".caf", ".opus",
}

# Campi tag che ci interessano (chiave mutagen EasyTag → nome italiano)
CAMPI_TAG = {
    "title": "titolo",
    "artist": "artista",
    "album": "album",
    "albumartist": "album_artista",
    "tracknumber": "traccia",
    "discnumber": "disco",
    "date": "anno",
    "genre": "genere",
}

# Campi obbligatori (se mancano → tag incompleti)
CAMPI_OBBLIGATORI = {"title", "artist", "album", "date", "genre"}

# Punteggio base per formato (più alto = meglio). Formati Apple hanno priorità.
PRIORITA_FORMATO = {
    ".aiff": 100, ".aif": 100,   # Apple AIFF (lossless)
    ".caf":   95,                 # Apple Core Audio Format
    ".m4a":   90,                 # Apple AAC / ALAC
    ".wav":   85,                 # PCM lossless
    ".flac":  80,                 # FLAC lossless
    ".opus":  60,
    ".ogg":   50,
    ".aac":   50,
    ".mp3":   40,
    ".wma":   30,
}


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
def init_db(db_path=DB_PATH):
    """Crea il database e la tabella se non esistono."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS brani (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            percorso TEXT UNIQUE,
            nome_file TEXT,
            dimensione INTEGER,
            -- Info tecniche
            durata REAL,
            bitrate INTEGER,
            sample_rate INTEGER,
            canali INTEGER,
            formato TEXT,
            -- Tag attuali
            titolo TEXT,
            artista TEXT,
            album TEXT,
            album_artista TEXT,
            traccia TEXT,
            disco TEXT,
            anno TEXT,
            genere TEXT,
            -- Suggerimenti MusicBrainz
            sug_titolo TEXT,
            sug_artista TEXT,
            sug_album TEXT,
            sug_album_artista TEXT,
            sug_traccia TEXT,
            sug_anno TEXT,
            sug_genere TEXT,
            musicbrainz_id TEXT,
            acoustid_fingerprint TEXT,
            -- Stato
            campi_mancanti INTEGER DEFAULT 0,
            stato TEXT DEFAULT 'da_analizzare',
            ultima_scansione TEXT
        )
    """)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Lettura metadati
# ---------------------------------------------------------------------------
def leggi_tag(filepath):
    """Legge i tag di un file audio usando mutagen. Restituisce un dict."""
    tag = {}
    try:
        audio = MutagenFile(filepath, easy=True)
        if audio is None:
            return tag
        for chiave_mutagen, chiave_db in CAMPI_TAG.items():
            valore = audio.get(chiave_mutagen)
            if valore:
                # mutagen restituisce liste
                tag[chiave_db] = valore[0] if isinstance(valore, list) else str(valore)
    except Exception:
        pass
    return tag


def leggi_info_tecniche(filepath):
    """Legge info tecniche (durata, bitrate, ecc.) usando mutagen."""
    info = {}
    try:
        audio = mutagen.File(filepath)
        if audio is not None and audio.info:
            info["durata"] = getattr(audio.info, "length", None)
            info["bitrate"] = getattr(audio.info, "bitrate", None)
            info["sample_rate"] = getattr(audio.info, "sample_rate", None)
            info["canali"] = getattr(audio.info, "channels", None)
    except Exception:
        pass
    # Fallback: afinfo su macOS
    if not info.get("bitrate"):
        try:
            r = subprocess.run(
                ["afinfo", filepath],
                capture_output=True, text=True, timeout=10,
            )
            for linea in r.stdout.splitlines():
                if "bit rate:" in linea.lower():
                    m = re.search(r"(\d+)", linea)
                    if m:
                        info["bitrate"] = int(m.group(1))
                if "estimated duration:" in linea.lower():
                    m = re.search(r"([\d.]+)", linea)
                    if m and not info.get("durata"):
                        info["durata"] = float(m.group(1))
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass
    return info


# ---------------------------------------------------------------------------
# Comando: scan
# ---------------------------------------------------------------------------
def cmd_scan(args, conn):
    """Scansiona una cartella e cataloga tutti i brani nel database."""
    if not HAS_MUTAGEN:
        print("❌ Serve 'mutagen'. Installa con: pip3 install mutagen")
        sys.exit(1)

    cartella = os.path.abspath(args.cartella)
    if not os.path.isdir(cartella):
        print(f"❌ '{cartella}' non è una cartella valida.")
        sys.exit(1)

    print(f"\n🔍 Scansione: {cartella}\n")

    # Raccogli file audio
    file_audio = []
    for root, _, files in os.walk(cartella):
        for nome in files:
            if os.path.splitext(nome)[1].lower() in ESTENSIONI_AUDIO:
                file_audio.append(os.path.join(root, nome))

    print(f"   Trovati {len(file_audio)} file audio\n")

    inseriti = aggiornati = errori = 0
    for i, filepath in enumerate(file_audio, 1):
        try:
            nome_file = os.path.basename(filepath)
            dimensione = os.path.getsize(filepath)
            formato = os.path.splitext(filepath)[1].lower()

            # Leggi tag e info tecniche
            tag = leggi_tag(filepath)
            info = leggi_info_tecniche(filepath)

            # Conta campi mancanti
            mancanti = sum(
                1 for campo in CAMPI_OBBLIGATORI
                if not tag.get(CAMPI_TAG.get(campo, campo))
            )

            # Upsert nel database
            conn.execute("""
                INSERT INTO brani (
                    percorso, nome_file, dimensione, formato,
                    durata, bitrate, sample_rate, canali,
                    titolo, artista, album, album_artista,
                    traccia, disco, anno, genere,
                    campi_mancanti, stato, ultima_scansione
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(percorso) DO UPDATE SET
                    nome_file=excluded.nome_file,
                    dimensione=excluded.dimensione,
                    formato=excluded.formato,
                    durata=excluded.durata,
                    bitrate=excluded.bitrate,
                    sample_rate=excluded.sample_rate,
                    canali=excluded.canali,
                    titolo=excluded.titolo,
                    artista=excluded.artista,
                    album=excluded.album,
                    album_artista=excluded.album_artista,
                    traccia=excluded.traccia,
                    disco=excluded.disco,
                    anno=excluded.anno,
                    genere=excluded.genere,
                    campi_mancanti=excluded.campi_mancanti,
                    ultima_scansione=excluded.ultima_scansione
            """, (
                filepath, nome_file, dimensione, formato,
                info.get("durata"), info.get("bitrate"),
                info.get("sample_rate"), info.get("canali"),
                tag.get("titolo"), tag.get("artista"),
                tag.get("album"), tag.get("album_artista"),
                tag.get("traccia"), tag.get("disco"),
                tag.get("anno"), tag.get("genere"),
                mancanti,
                "da_analizzare" if mancanti > 0 else "completo",
                datetime.now().isoformat(),
            ))

            if conn.total_changes > 0:
                aggiornati += 1
            inseriti += 1

        except Exception as e:
            print(f"  ⚠ Errore: {filepath}: {e}")
            errori += 1

        if i % 200 == 0:
            conn.commit()
            print(f"   ...{i}/{len(file_audio)} processati")

    conn.commit()

    # Riepilogo
    totale = conn.execute("SELECT COUNT(*) FROM brani").fetchone()[0]
    incompleti = conn.execute(
        "SELECT COUNT(*) FROM brani WHERE campi_mancanti > 0"
    ).fetchone()[0]

    print(f"\n✅ Scansione completata:")
    print(f"   {inseriti} brani processati, {errori} errori")
    print(f"   {totale} brani totali nel catalogo")
    print(f"   {incompleti} brani con tag incompleti\n")


# ---------------------------------------------------------------------------
# Comando: report
# ---------------------------------------------------------------------------
def cmd_report(args, conn):
    """Mostra un report dei brani con tag mancanti o sospetti."""
    print("\n📊 Report catalogo musicale\n")

    totale = conn.execute("SELECT COUNT(*) FROM brani").fetchone()[0]
    if totale == 0:
        print("   Database vuoto. Esegui prima: python3 music_catalog.py scan /cartella")
        return

    # Statistiche generali
    print(f"   Brani totali: {totale}")

    for campo_db in ["titolo", "artista", "album", "anno", "genere"]:
        mancanti = conn.execute(
            f"SELECT COUNT(*) FROM brani WHERE {campo_db} IS NULL OR {campo_db} = ''"
        ).fetchone()[0]
        if mancanti > 0:
            print(f"   ⚠ {campo_db:15s} mancante in {mancanti:>5} brani ({mancanti*100//totale}%)")

    # Formati
    print("\n   Formati:")
    for row in conn.execute(
        "SELECT formato, COUNT(*) as n FROM brani GROUP BY formato ORDER BY n DESC"
    ):
        print(f"     {row['formato']:8s} {row['n']:>5} brani")

    # Brani più problematici
    print("\n--- Brani con più campi mancanti (top 30) ---\n")
    rows = conn.execute("""
        SELECT percorso, nome_file, titolo, artista, album, anno, genere, campi_mancanti
        FROM brani
        WHERE campi_mancanti > 0
        ORDER BY campi_mancanti DESC, nome_file
        LIMIT 30
    """).fetchall()

    if not rows:
        print("   ✅ Tutti i brani hanno i tag completi!")
        return

    for i, r in enumerate(rows, 1):
        manca = []
        if not r["titolo"]:   manca.append("titolo")
        if not r["artista"]:  manca.append("artista")
        if not r["album"]:    manca.append("album")
        if not r["anno"]:     manca.append("anno")
        if not r["genere"]:   manca.append("genere")
        print(f"  {i:>3}. {r['nome_file']}")
        print(f"       Mancano: {', '.join(manca)}")
        if r["titolo"] or r["artista"]:
            print(f"       Attuale: {r['artista'] or '?'} — {r['titolo'] or '?'}")
        print()

    # Conteggio per stato
    print("--- Stato brani ---")
    for row in conn.execute(
        "SELECT stato, COUNT(*) as n FROM brani GROUP BY stato ORDER BY n DESC"
    ):
        print(f"   {row['stato']:20s} {row['n']:>5} brani")
    print()


# ---------------------------------------------------------------------------
# Comando: duplicati (nella libreria, sul catalogo)
# ---------------------------------------------------------------------------
def formatta_dim(nbytes):
    for u in ("B", "KB", "MB", "GB", "TB"):
        if not nbytes or nbytes < 1024:
            return f"{(nbytes or 0):.1f} {u}"
        nbytes /= 1024
    return f"{nbytes:.1f} PB"


def normalizza_testo(testo):
    """Normalizza un tag per il confronto: minuscolo, senza spazi extra,
    Unicode in forma NFC (così 'à' composto e decomposto coincidono)."""
    if not testo:
        return ""
    testo = unicodedata.normalize("NFC", str(testo))
    return re.sub(r"\s+", " ", testo).strip().lower()


def titolo_da_nome(nome_file):
    """Estrae il titolo dal nome file, senza numero traccia.
    '03 Song.mp3' → 'song', 'Artist - Album 01 Song.mp3' → 'song'
    """
    name = unicodedata.normalize("NFC", os.path.splitext(nome_file)[0])
    m = re.search(r"(?:^|[\s-])\d{1,3}[\s._-]+([^/]+)$", name)
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip().lower()
    pulito = re.sub(r"^\d{1,3}[\s._-]+", "", name).strip()
    return re.sub(r"\s+", " ", pulito).lower()


def ha_prefisso_numerico(nome_file):
    """True se il nome file contiene un numero traccia (inizio o nel mezzo)."""
    name = os.path.splitext(nome_file)[0]
    if re.match(r"^\d{1,3}[\s._-]+", name):
        return True
    return bool(re.search(r"(?:^|[\s-])\d{1,3}[\s._-]+[A-Za-z]", name))


def punteggio_brano(r):
    """Qualità stimata del brano: formato + bitrate (dal DB, niente afinfo).
    Più alto = meglio. A parità vince il nome senza numero traccia."""
    base = PRIORITA_FORMATO.get((r["formato"] or "").lower(), 20)
    bitrate = r["bitrate"] or 0
    punteggio = base + (min(bitrate / 28000, 50) if bitrate > 0 else 0)
    if not ha_prefisso_numerico(r["nome_file"]):
        punteggio += 0.1
    return punteggio


def _raggruppa_per_brano(brani):
    """Raggruppa le righe del DB per brano (chiave condivisa da 'duplicati'
    e 'organizza'). Chiave: (artista, titolo, album) dai tag — stesso brano
    nello stesso album = stessa registrazione; live/studio e versioni in
    album diversi NON vengono fusi. Se manca il tag album, fallback sul nome
    della cartella (struttura Apple Music); se manca il tag titolo, fallback
    sul titolo estratto dal nome file (confronto meno affidabile).
    Restituisce (gruppi, per_chiave_da_tags).
    """
    def album_di(r):
        album = normalizza_testo(r["album"])
        if album:
            return album
        # senza tag album: usa il nome della cartella che contiene il file
        return os.path.basename(os.path.dirname(r["percorso"])).lower()

    gruppi = defaultdict(list)
    per_chiave_da_tags = {}
    for r in brani:
        titolo_tag = normalizza_testo(r["titolo"])
        if titolo_tag:
            chiave = (normalizza_testo(r["artista"]), titolo_tag, album_di(r))
            per_chiave_da_tags[chiave] = True
        else:
            chiave = ("", titolo_da_nome(r["nome_file"]), album_di(r))
            per_chiave_da_tags[chiave] = False
        gruppi[chiave].append(r)
    return gruppi, per_chiave_da_tags


def _trova_duplicati(conn):
    """Cerca i duplicati nel catalogo. Restituisce una lista di dizionari:
    {'chiave': (artista, titolo), 'da_tags': bool, 'tenuto': riga, 'dup': riga, 'motivo': str}
    """
    rows = conn.execute("""
        SELECT id, percorso, nome_file, formato, dimensione, bitrate,
               titolo, artista, album, traccia, stato
        FROM brani
        WHERE stato IS NULL OR stato != 'in_revisione'
        ORDER BY artista, album, titolo, nome_file
    """).fetchall()

    # Salta i file che non esistono più su disco (catalogo non aggiornato)
    brani = []
    mancanti = 0
    for r in rows:
        if os.path.exists(r["percorso"]):
            brani.append(r)
        else:
            mancanti += 1
    if mancanti:
        print(f"\n   ⚠ {mancanti} brani nel DB non esistono più su disco (riesegui 'scan').")

    # Raggruppa per brano (chiave condivisa con 'organizza')
    gruppi, per_chiave_da_tags = _raggruppa_per_brano(brani)

    duplicati = []
    for chiave, lista in gruppi.items():
        if len(lista) < 2:
            continue
        lista_ord = sorted(lista, key=punteggio_brano, reverse=True)
        tenuto = lista_ord[0]
        for dup in lista_ord[1:]:
            if ha_prefisso_numerico(dup["nome_file"]) and not ha_prefisso_numerico(tenuto["nome_file"]):
                motivo = "duplicato con numero traccia nel nome"
            elif (dup["formato"] or "") != (tenuto["formato"] or ""):
                motivo = f"qualità inferiore ({dup['formato'] or '?'} vs {tenuto['formato'] or '?'})"
            else:
                motivo = "stesso brano, copia in più"
            duplicati.append({
                "chiave": chiave,
                "da_tags": per_chiave_da_tags[chiave],
                "tenuto": tenuto,
                "dup": dup,
                "motivo": motivo,
            })
    return duplicati


def cmd_duplicati(args, conn):
    """Trova i doppioni nella libreria (stesso artista + titolo + album) e li mette da parte.

    Di default mostra solo un'anteprima, senza toccare nulla.
    Con --revisione DIR --esegui sposta i duplicati in una cartella di
    revisione (NON li cancella) e scrive un CSV con i percorsi originali.
    Con --esegui da solo li elimina definitivamente, dopo conferma.
    """
    esegui = getattr(args, "esegui", False)
    revisione = getattr(args, "revisione", None)

    duplicati = _trova_duplicati(conn)

    if not duplicati:
        print("\n✅ Nessun duplicato trovato nel catalogo.")
        return

    spazio = sum((d["dup"]["dimensione"] or 0) for d in duplicati)
    n_gruppi = len({d["chiave"] for d in duplicati})
    print()
    print("=" * 60)
    print(f"🔁 {len(duplicati)} duplicati in {n_gruppi} gruppi "
          f"({formatta_dim(spazio)} recuperabili)")
    print("=" * 60 + "\n")

    # Raggruppa per chiave per la visualizzazione
    per_gruppo = defaultdict(list)
    for d in duplicati:
        per_gruppo[d["chiave"]].append(d)

    def descr(r):
        ext = (r["formato"] or "?").ljust(5)
        br = r["bitrate"]
        br_s = f"{br // 1000}kbps" if br else "?"
        dim = formatta_dim(r["dimensione"] or 0).rjust(10)
        return f"[{ext} {br_s:>7s} {dim}] {os.path.basename(r['percorso'])}"

    gruppi_mostrati = 0
    for chiave in sorted(per_gruppo):
        gruppo = per_gruppo[chiave]
        tenuto = gruppo[0]["tenuto"]
        artista, titolo, album = chiave
        etichetta = f"{artista} — {titolo}  [{album}]" if artista else f"{titolo}  [{album}]"
        avviso = "" if gruppo[0]["da_tags"] else "   ⚠ senza tag: confronto sul nome file"
        print(f"  🎵 {etichetta}{avviso}")
        print(f"     ✅ TENUTO   {descr(tenuto)}")
        for d in gruppo:
            print(f"     ❌ {d['motivo']}")
            print(f"        {descr(d['dup'])}")
            print(f"        → {d['dup']['percorso']}")
        print()
        gruppi_mostrati += 1
        if gruppi_mostrati >= 20 and not esegui and len(per_gruppo) > 20:
            print(f"  ... e altri {len(per_gruppo) - 20} gruppi\n")
            break

    if not esegui:
        print("⚠️  Modalità ANTEPRIMA — nessun file toccato.")
        print(f"   Per mettere i duplicati da parte (consigliato):")
        print(f"   music_catalog.py duplicati --revisione /cartella/revisione --esegui")
        print(f"   Per eliminarli definitivamente:")
        print(f"   music_catalog.py duplicati --esegui\n")
        return

    # --- Modalità revisione: sposta, non cancellare ---
    if revisione:
        revisione = os.path.abspath(revisione)
        os.makedirs(revisione, exist_ok=True)
        print(f"\n📁 Sposto i duplicati in: {revisione}")
        spostati = errori = 0
        manifest = []
        for d in duplicati:
            dup = d["dup"]
            # Struttura: revisione / <cartella padre> / <nome file>
            padre = os.path.basename(os.path.dirname(dup["percorso"])) or "radice"
            dest_dir = os.path.join(revisione, padre)
            os.makedirs(dest_dir, exist_ok=True)
            dest = os.path.join(dest_dir, os.path.basename(dup["percorso"]))
            n = 2
            while os.path.exists(dest):
                base, ext = os.path.splitext(dest)
                dest = f"{base} ({n}){ext}"
                n += 1
            try:
                shutil.move(dup["percorso"], dest)
                spostati += 1
                manifest.append((dup["percorso"], dest, d["motivo"], d["tenuto"]["percorso"]))
                conn.execute(
                    "UPDATE brani SET percorso = ?, stato = 'in_revisione' WHERE id = ?",
                    (dest, dup["id"]),
                )
            except (OSError, shutil.Error) as e:
                print(f"  ⚠ Errore spostando {dup['percorso']}: {e}")
                errori += 1
        conn.commit()

        # Manifest CSV: percorsi originali per poter ripristinare
        manifest_path = os.path.join(revisione, "da_rivedere.csv")
        try:
            with open(manifest_path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["origine", "destinazione", "motivo", "brano_tenuto"])
                for m in manifest:
                    w.writerow(m)
        except OSError as e:
            print(f"  ⚠ Errore scrivendo il manifest: {e}")

        print(f"\n✅ {spostati} file spostati in revisione, {errori} errori.")
        print(f"   Elenco con percorsi originali: {manifest_path}")
        print("   Controlla la cartella: quando sei sicuro, svuotala tu.")
        return

    # --- Modalità elimina ---
    print(f"\n⚠️  Stai per ELIMINARE definitivamente {len(duplicati)} file.")
    risposta = input("   Confermi? (sì/no): ").strip().lower()
    if risposta not in ("sì", "si", "s", "y", "yes"):
        print("   Operazione annullata.")
        return
    eliminati = errori = 0
    for d in duplicati:
        dup = d["dup"]
        try:
            os.remove(dup["percorso"])
            eliminati += 1
            conn.execute("DELETE FROM brani WHERE id = ?", (dup["id"],))
        except OSError as e:
            print(f"  ⚠ Errore eliminando {dup['percorso']}: {e}")
            errori += 1
    conn.commit()
    print(f"\n✅ {eliminati} file eliminati, {errori} errori. Spazio: {formatta_dim(spazio)}")


# ---------------------------------------------------------------------------
# Comando: lookup
# ---------------------------------------------------------------------------
def _cerca_musicbrainz(titolo, artista):
    """Cerca un brano su MusicBrainz per titolo e artista."""
    try:
        query = ""
        if titolo:
            query += f'recording:"{titolo}"'
        if artista:
            query += f' AND artist:"{artista}"' if query else f'artist:"{artista}"'
        if not query:
            return None

        result = musicbrainzngs.search_recordings(query=query, limit=3)
        recordings = result.get("recording-list", [])
        if not recordings:
            return None

        # Prendi il risultato con score più alto
        best = recordings[0]
        score = int(best.get("ext:score", 0))
        if score < 60:
            return None

        info = {
            "sug_titolo": best.get("title"),
            "musicbrainz_id": best.get("id"),
        }

        # Artista
        artists = best.get("artist-credit", [])
        if artists:
            info["sug_artista"] = artists[0].get("artist", {}).get("name")

        # Album (primo release)
        releases = best.get("release-list", [])
        if releases:
            rel = releases[0]
            info["sug_album"] = rel.get("title")
            info["sug_anno"] = rel.get("date", "")[:4] if rel.get("date") else None

            # Album artist
            artist_credit = rel.get("artist-credit", [])
            if artist_credit:
                info["sug_album_artista"] = artist_credit[0].get("artist", {}).get("name")

            # Numero traccia
            medium_list = rel.get("medium-list", [])
            if medium_list:
                tracks = medium_list[0].get("track-list", [])
                if tracks:
                    info["sug_traccia"] = tracks[0].get("number")

        # Genere (tag MusicBrainz)
        tags = best.get("tag-list", [])
        if tags:
            # Prendi il tag con count più alto
            tags_sorted = sorted(tags, key=lambda t: int(t.get("count", 0)), reverse=True)
            info["sug_genere"] = tags_sorted[0].get("name", "").title()

        return info

    except Exception:
        return None


def _cerca_acoustid(filepath, api_key):
    """Cerca un brano tramite fingerprint audio su AcoustID."""
    try:
        results = acoustid.match(api_key, filepath)
        for score, recording_id, title, artist in results:
            if score < 0.5:
                continue
            info = {
                "sug_titolo": title,
                "sug_artista": artist,
                "musicbrainz_id": recording_id,
            }
            # Cerca dettagli aggiuntivi su MusicBrainz
            if HAS_MUSICBRAINZ and recording_id:
                try:
                    rec = musicbrainzngs.get_recording_by_id(
                        recording_id, includes=["releases", "artist-credits", "tags"]
                    )
                    recording = rec.get("recording", {})

                    releases = recording.get("release-list", [])
                    if releases:
                        info["sug_album"] = releases[0].get("title")
                        date = releases[0].get("date", "")
                        if date:
                            info["sug_anno"] = date[:4]

                    tags = recording.get("tag-list", [])
                    if tags:
                        tags_sorted = sorted(
                            tags, key=lambda t: int(t.get("count", 0)), reverse=True
                        )
                        info["sug_genere"] = tags_sorted[0].get("name", "").title()

                    time.sleep(1)  # Rate limit MusicBrainz
                except Exception:
                    pass
            return info
        return None
    except Exception:
        return None


def cmd_lookup(args, conn):
    """Cerca info corrette su MusicBrainz e/o AcoustID."""
    usa_acoustid = getattr(args, "acoustid", False)
    api_key = getattr(args, "api_key", None)

    if not HAS_MUSICBRAINZ:
        print("❌ Serve 'musicbrainzngs'. Installa con: pip3 install musicbrainzngs")
        sys.exit(1)

    if usa_acoustid:
        if not HAS_ACOUSTID:
            print("❌ Serve 'pyacoustid'. Installa con: pip3 install pyacoustid")
            print("   Serve anche chromaprint: brew install chromaprint")
            sys.exit(1)
        if not api_key:
            print("❌ Per AcoustID serve una API key gratuita.")
            print("   Registrati su https://acoustid.org/new-application")
            print("   Poi usa: --api-key TUA_CHIAVE")
            sys.exit(1)

    # Seleziona brani da cercare
    rows = conn.execute("""
        SELECT id, percorso, nome_file, titolo, artista, album, anno, genere,
               campi_mancanti, stato
        FROM brani
        WHERE stato IN ('da_analizzare', 'completo')
          AND (campi_mancanti > 0 OR ? = 1)
        ORDER BY campi_mancanti DESC
    """, (1 if getattr(args, "tutti", False) else 0,)).fetchall()

    if not rows:
        print("\n✅ Nessun brano da analizzare.")
        return

    print(f"\n🔎 Lookup su MusicBrainz per {len(rows)} brani...\n")

    trovati = 0
    for i, r in enumerate(rows, 1):
        nome = r["nome_file"]
        titolo = r["titolo"]
        artista = r["artista"]

        # Se non ha tag, prova a estrarre dal nome file
        if not titolo and not artista:
            # "Artista - Titolo.mp3" pattern
            name_no_ext = os.path.splitext(nome)[0]
            # Rimuovi prefisso numerico
            name_clean = re.sub(r"^\d{1,3}[\s._-]+", "", name_no_ext)
            if " - " in name_clean:
                parts = name_clean.split(" - ", 1)
                artista = parts[0].strip()
                titolo = parts[1].strip()
            else:
                titolo = name_clean.strip()

        info = None

        # Strategia 1: MusicBrainz per tag
        if titolo or artista:
            info = _cerca_musicbrainz(titolo, artista)
            time.sleep(1.1)  # Rate limit: max 1 req/sec

        # Strategia 2: AcoustID (fingerprint audio)
        if not info and usa_acoustid and api_key:
            print(f"   🎵 Fingerprint: {nome}...")
            info = _cerca_acoustid(r["percorso"], api_key)

        if info:
            # Salva suggerimenti nel database
            updates = []
            params = []
            for chiave, valore in info.items():
                if valore and (chiave.startswith("sug_") or chiave == "musicbrainz_id"):
                    updates.append(f"{chiave} = ?")
                    params.append(valore)

            if updates:
                updates.append("stato = ?")
                params.append("suggerito")
                params.append(r["id"])
                conn.execute(
                    f"UPDATE brani SET {', '.join(updates)} WHERE id = ?",
                    params,
                )
                trovati += 1
                print(f"  {i:>4}. ✓ {nome}")
                print(f"        → {info.get('sug_artista', '?')} — "
                      f"{info.get('sug_titolo', '?')} "
                      f"({info.get('sug_album', '?')}, {info.get('sug_anno', '?')})")
        else:
            print(f"  {i:>4}. ✗ {nome} — nessun risultato")

        if i % 50 == 0:
            conn.commit()
            print(f"\n   ...{i}/{len(rows)} processati, {trovati} trovati\n")

    conn.commit()
    print(f"\n✅ Lookup completato: {trovati}/{len(rows)} brani con suggerimenti.\n")
    print("   Esegui 'python3 music_catalog.py fix' per applicare le correzioni.\n")


# ---------------------------------------------------------------------------
# Comando: fix
# ---------------------------------------------------------------------------
def cmd_fix(args, conn):
    """Mostra le correzioni proposte e le applica ai file."""
    if not HAS_MUTAGEN:
        print("❌ Serve 'mutagen'. Installa con: pip3 install mutagen")
        sys.exit(1)

    rows = conn.execute("""
        SELECT id, percorso, nome_file,
               titolo, artista, album, album_artista, traccia, anno, genere,
               sug_titolo, sug_artista, sug_album, sug_album_artista,
               sug_traccia, sug_anno, sug_genere
        FROM brani
        WHERE stato = 'suggerito'
        ORDER BY nome_file
    """).fetchall()

    if not rows:
        print("\n✅ Nessuna correzione da applicare.")
        print("   Esegui prima 'python3 music_catalog.py lookup' per cercare le info.\n")
        return

    print(f"\n📝 {len(rows)} brani con correzioni proposte\n")

    # Mappa campo suggerito → campo tag mutagen
    CAMPO_DB_A_MUTAGEN = {
        "titolo": "title",
        "artista": "artist",
        "album": "album",
        "album_artista": "albumartist",
        "traccia": "tracknumber",
        "anno": "date",
        "genere": "genre",
    }

    # Mostra anteprima
    correzioni = []  # lista di (row, {campo: nuovo_valore})
    for r in rows:
        diff = {}
        for campo_db, campo_mutagen in CAMPO_DB_A_MUTAGEN.items():
            attuale = r[campo_db] or ""
            suggerito = r[f"sug_{campo_db}"] or ""
            # Suggerisci solo se: il campo è vuoto, o il suggerito è diverso e non vuoto
            if suggerito and (not attuale or attuale.lower() != suggerito.lower()):
                diff[campo_db] = {
                    "mutagen": campo_mutagen,
                    "attuale": attuale,
                    "nuovo": suggerito,
                }

        if diff:
            correzioni.append((r, diff))

    if not correzioni:
        print("   Nessuna modifica necessaria (tag già allineati ai suggerimenti).")
        return

    # Mostra
    for i, (r, diff) in enumerate(correzioni, 1):
        print(f"  {i:>3}. {r['nome_file']}")
        for campo, info in diff.items():
            att = info["attuale"] or "(vuoto)"
            nuovo = info["nuovo"]
            print(f"       {campo:15s}  {att}  →  {nuovo}")
        print()
        if i >= 30 and not getattr(args, "esegui", False):
            restanti = len(correzioni) - 30
            if restanti > 0:
                print(f"       ... e altri {restanti} brani\n")
            break

    if not getattr(args, "esegui", False):
        print(f"⚠️  Modalità ANTEPRIMA — nessun file modificato.")
        print(f"   Per applicare le correzioni:")
        print(f"   python3 music_catalog.py fix --esegui\n")
        return

    # Conferma
    print(f"⚠️  Stai per modificare i tag di {len(correzioni)} file.")
    risposta = input("   Confermi? (sì/no): ").strip().lower()
    if risposta not in ("sì", "si", "s", "y", "yes"):
        print("   Operazione annullata.")
        return

    # Applica
    corretti = errori_fix = 0
    for r, diff in correzioni:
        filepath = r["percorso"]
        try:
            audio = MutagenFile(filepath, easy=True)
            if audio is None:
                print(f"  ⚠ Impossibile aprire: {filepath}")
                errori_fix += 1
                continue

            for campo, info in diff.items():
                audio[info["mutagen"]] = info["nuovo"]

            audio.save()
            corretti += 1

            # Aggiorna DB
            update_fields = []
            update_params = []
            for campo, info in diff.items():
                update_fields.append(f"{campo} = ?")
                update_params.append(info["nuovo"])
            update_fields.append("stato = ?")
            update_params.append("corretto")

            # Ricalcola campi mancanti
            tag_aggiornati = {c: diff[c]["nuovo"] if c in diff else r[c] for c in CAMPO_DB_A_MUTAGEN}
            nuovi_mancanti = sum(
                1 for c in CAMPI_OBBLIGATORI
                if not tag_aggiornati.get(CAMPI_TAG.get(c, c))
            )
            update_fields.append("campi_mancanti = ?")
            update_params.append(nuovi_mancanti)
            update_params.append(r["id"])

            conn.execute(
                f"UPDATE brani SET {', '.join(update_fields)} WHERE id = ?",
                update_params,
            )

        except Exception as e:
            print(f"  ⚠ Errore su {filepath}: {e}")
            errori_fix += 1

    conn.commit()
    print(f"\n✅ Completato: {corretti} brani corretti, {errori_fix} errori.\n")


# ---------------------------------------------------------------------------
# Comando: organizza (struttura Apple Music)
# ---------------------------------------------------------------------------
def pulisci_nome(nome, max_len=80):
    """Rimuove caratteri non validi per nomi di file/cartelle su macOS."""
    # Rimuovi caratteri proibiti
    pulito = re.sub(r'[/:"*?<>|\\]', '', nome)
    # Rimuovi punti iniziali/finali e spazi
    pulito = pulito.strip('. ')
    # Tronca se troppo lungo
    if len(pulito) > max_len:
        pulito = pulito[:max_len].rstrip('. ')
    return pulito or "Sconosciuto"


def formatta_traccia(traccia, disco=None, multi_disco=False):
    """Formatta il numero traccia: '01', '1-01' per multi-disco."""
    if not traccia:
        return ""
    # Gestisci formati tipo "3/12"
    num = re.split(r'[/\\]', str(traccia))[0].strip()
    try:
        num = int(num)
    except ValueError:
        return ""
    if multi_disco and disco:
        try:
            d = int(re.split(r'[/\\]', str(disco))[0].strip())
            return f"{d}-{num:02d}"
        except ValueError:
            pass
    return f"{num:02d}"


def _piano_organizzazione(conn, destinazione):
    """Calcola il piano di organizzazione in struttura Apple Music.

    Restituisce (operazioni, spazio_totale, conteggi):
      - operazioni: lista di (origine, destinazione, motivo_skip o None)
      - spazio_totale: byte dei file da copiare/spostare
      - conteggi: dict con totale, in_revisione, scomparsi, doppi_esclusi
    """
    rows = conn.execute("""
        SELECT id, percorso, nome_file, formato,
               titolo, artista, album, album_artista,
               traccia, disco, anno, genere,
               dimensione, bitrate, stato
        FROM brani
        ORDER BY artista, album, traccia, titolo
    """).fetchall()

    conteggi = {"totale": len(rows), "in_revisione": 0, "scomparsi": 0, "doppi_esclusi": 0}
    operazioni = []  # lista di (origine, destinazione, motivo_skip)
    spazio_totale = 0

    if not rows:
        return operazioni, spazio_totale, conteggi

    # Salta i doppioni messi da parte da 'duplicati' e i file scomparsi
    presenti = []
    for r in rows:
        if (r["stato"] or "") == "in_revisione":
            conteggi["in_revisione"] += 1
        elif not os.path.exists(r["percorso"]):
            conteggi["scomparsi"] += 1
        else:
            presenti.append(r)

    # Deduplica: stesso brano (artista + titolo + album) → tiene la copia migliore
    gruppi, _ = _raggruppa_per_brano(presenti)
    scelti = []
    for lista in gruppi.values():
        scelti.append(sorted(lista, key=punteggio_brano, reverse=True)[0])
    conteggi["doppi_esclusi"] = len(presenti) - len(scelti)
    rows = scelti

    def album_di(r):
        """Album dai tag; se manca, nome della cartella di origine."""
        return (
            r["album"]
            or os.path.basename(os.path.dirname(r["percorso"]))
            or "Sconosciuto"
        )

    # Determina quali album sono compilation (>1 artista diverso)
    album_artisti = {}  # album → set(artisti)
    for r in rows:
        album = album_di(r)
        artista = r["artista"] or "Sconosciuto"
        album_artisti.setdefault(album, set()).add(artista.lower())

    compilation_albums = {
        album for album, artisti in album_artisti.items()
        if len(artisti) > 1
    }

    # Determina album multi-disco
    album_dischi = {}  # album → set(disco)
    for r in rows:
        album = album_di(r)
        disco = r["disco"]
        if disco:
            try:
                d = int(re.split(r'[/\\]', str(disco))[0].strip())
                album_dischi.setdefault(album, set()).add(d)
            except ValueError:
                pass

    multi_disco_albums = {
        album for album, dischi in album_dischi.items()
        if len(dischi) > 1
    }

    # Calcola percorsi di destinazione
    for r in rows:
        origine = r["percorso"]
        if not os.path.exists(origine):
            continue

        # Determina artista cartella
        album = pulisci_nome(album_di(r))
        artista = r["artista"] or "Sconosciuto"
        album_artista = r["album_artista"] or artista
        titolo = r["titolo"] or os.path.splitext(r["nome_file"])[0]
        ext = r["formato"] or os.path.splitext(r["nome_file"])[1]

        # Compilation: usa "Compilation" come cartella artista
        is_compilation = album in compilation_albums
        if is_compilation:
            cartella_artista = pulisci_nome("Compilation")
        else:
            cartella_artista = pulisci_nome(album_artista)

        cartella_album = pulisci_nome(album)

        # Anno nell'album (opzionale, come fa Apple Music)
        anno = r["anno"]
        # Non aggiungere anno al nome cartella per rimanere fedeli ad Apple Music

        # Nome file: "01 Titolo.ext"
        is_multi_disco = album in multi_disco_albums
        num_traccia = formatta_traccia(r["traccia"], r["disco"], is_multi_disco)
        titolo_pulito = pulisci_nome(titolo)

        if num_traccia:
            nome_dest = f"{num_traccia} {titolo_pulito}{ext}"
        else:
            nome_dest = f"{titolo_pulito}{ext}"

        # Percorso completo
        percorso_dest = os.path.join(
            destinazione, cartella_artista, cartella_album, nome_dest
        )

        # Controlla se esiste già
        skip = None
        if os.path.exists(percorso_dest):
            # Se stesso file (stessa dimensione), salta
            if os.path.getsize(percorso_dest) == (r["dimensione"] or 0):
                skip = "già presente"
            else:
                # Aggiungi suffisso per evitare sovrascrittura
                base, e = os.path.splitext(percorso_dest)
                percorso_dest = f"{base} (2){e}"

        operazioni.append((origine, percorso_dest, skip))
        if not skip:
            spazio_totale += r["dimensione"] or 0

    return operazioni, spazio_totale, conteggi


def _sh_quote(percorso):
    """Quota un percorso per uno script shell (gestisce gli apostrofi)."""
    return "'" + percorso.replace("'", "'\\''") + "'"


def _scrivi_piano(file_piano, operazioni, sposta):
    """Scrive uno script shell con le operazioni di copia/spostamento.

    Ogni riga è indipendente e salta i file già spostati: lo script si può
    rilanciare più volte senza fare danni (riprendibile).
    """
    verbo = "mv" if sposta else "cp"
    n = 0
    with open(file_piano, "w", encoding="utf-8") as f:
        f.write("#!/bin/bash\n")
        f.write("# Piano generato da music_catalog.py organizza --piano\n")
        f.write("# Rilancabile: le righe saltano i file già spostati/copiati\n\n")
        for origine, dest, skip in operazioni:
            if skip:
                continue
            f.write(f"mkdir -p {_sh_quote(os.path.dirname(dest))}\n")
            f.write(
                f"[ -f {_sh_quote(origine)} ] && {verbo} -n "
                f"{_sh_quote(origine)} {_sh_quote(dest)}\n"
            )
            n += 1
    os.chmod(file_piano, 0o755)
    print(f"\n📝 Piano scritto in: {file_piano} ({n} operazioni)")
    print(f"   Eseguilo dove i file sono su disco locale:  bash {file_piano}")


def cmd_organizza(args, conn):
    """Copia o sposta i brani nella struttura Apple Music.

    Struttura:
        Destinazione/
          Artista/
            Album/
              01 Titolo.ext
              02 Titolo.ext
          Compilation/
            Album/
              1-01 Titolo.ext   (multi-disco)

    Deduplica in fase di copia: per ogni brano (artista + titolo + album)
    tiene solo la copia di qualità migliore; salta i doppioni messi in
    revisione da 'duplicati' e i file non più presenti su disco.

    Con --piano FILE invece di eseguire scrive uno script shell con le
    operazioni (ripetibile: ogni riga salta i file già spostati), da
    eseguire dove i file sono su disco locale.
    """
    destinazione = os.path.abspath(args.destinazione)
    sposta = getattr(args, "sposta", False)
    esegui = getattr(args, "esegui", False)
    piano = getattr(args, "piano", None)
    azione = "Spostamento" if sposta else "Copia"

    operazioni, spazio_totale, conteggi = _piano_organizzazione(conn, destinazione)

    if not conteggi["totale"]:
        print("\n   Database vuoto. Esegui prima: python3 music_catalog.py scan /cartella")
        return

    print(f"\n📁 {azione} in struttura Apple Music")
    print(f"   Destinazione: {destinazione}")
    print(f"   Brani nel catalogo: {conteggi['totale']}")
    if conteggi["in_revisione"]:
        print(f"   ⏭  {conteggi['in_revisione']} brani saltati (doppioni in revisione da 'duplicati')")
    if conteggi["scomparsi"]:
        print(f"   ⏭  {conteggi['scomparsi']} brani saltati (file non più su disco)")
    if conteggi["doppi_esclusi"]:
        print(f"   ⏭  {conteggi['doppi_esclusi']} doppioni esclusi (per ogni brano tiene il migliore)")
    print()

    # Riepilogo
    da_copiare = [(o, d) for o, d, s in operazioni if s is None]
    saltati = [(o, d, s) for o, d, s in operazioni if s is not None]

    print(f"   📋 {len(da_copiare)} brani da {'spostare' if sposta else 'copiare'}")
    print(f"   ⏭  {len(saltati)} brani saltati (già presenti)")

    # Formatta spazio
    sp = spazio_totale
    for u in ("B", "KB", "MB", "GB", "TB"):
        if sp < 1024:
            print(f"   💾 Spazio necessario: {sp:.1f} {u}\n")
            break
        sp /= 1024

    # Mostra anteprima struttura
    print("--- Struttura destinazione (anteprima) ---\n")
    cartelle_viste = set()
    mostrati = 0
    for origine, dest, skip in operazioni:
        if skip:
            continue
        rel = os.path.relpath(dest, destinazione)
        parti = rel.split(os.sep)

        # Mostra gerarchia cartelle
        cartella = os.path.join(*parti[:-1]) if len(parti) > 1 else ""
        if cartella and cartella not in cartelle_viste:
            cartelle_viste.add(cartella)
            artista_dir = parti[0] if len(parti) > 0 else ""
            album_dir = parti[1] if len(parti) > 1 else ""
            print(f"  📂 {artista_dir}/{album_dir}/")

        nome_f = parti[-1]
        print(f"     {nome_f}")

        mostrati += 1
        if mostrati >= 50 and not esegui:
            restanti = len(da_copiare) - 50
            if restanti > 0:
                print(f"\n     ... e altri {restanti} brani")
            break

    # Modalità piano: scrivi lo script shell invece di eseguire
    if piano:
        _scrivi_piano(piano, operazioni, sposta)
        return

    if not esegui:
        print(f"\n⚠️  Modalità ANTEPRIMA — nessun file {'spostato' if sposta else 'copiato'}.")
        flag_extra = " --sposta" if sposta else ""
        print(f"   Per eseguire:")
        print(f'   python3 music_catalog.py organizza "{destinazione}"{flag_extra} --esegui\n')
        return

    # Conferma
    print(f"\n⚠️  Stai per {'spostare' if sposta else 'copiare'} {len(da_copiare)} file.")
    risposta = input("   Confermi? (sì/no): ").strip().lower()
    if risposta not in ("sì", "si", "s", "y", "yes"):
        print("   Operazione annullata.")
        return

    # Esecuzione
    completati = errori_org = 0
    for origine, dest, skip in operazioni:
        if skip:
            continue
        try:
            # Crea directory se non esiste
            os.makedirs(os.path.dirname(dest), exist_ok=True)

            if sposta:
                shutil.move(origine, dest)
            else:
                shutil.copy2(origine, dest)  # copy2 preserva i metadati

            completati += 1
            if completati % 200 == 0:
                print(f"   ...{completati}/{len(da_copiare)} completati")

        except (OSError, shutil.Error) as e:
            print(f"  ⚠ Errore: {e}")
            errori_org += 1

    print(f"\n✅ Completato: {completati} file {'spostati' if sposta else 'copiati'}, "
          f"{errori_org} errori.")
    print(f"   Destinazione: {destinazione}\n")


# ---------------------------------------------------------------------------
# Comando: reset
# ---------------------------------------------------------------------------
def cmd_reset(args, conn):
    """Svuota il catalogo. I file audio NON vengono toccati."""
    esegui = getattr(args, "esegui", False)
    totale = conn.execute("SELECT COUNT(*) FROM brani").fetchone()[0]

    print(f"\n📀 Catalogo attuale: {totale} brani")
    for r in conn.execute(
        "SELECT stato, COUNT(*) as n FROM brani GROUP BY stato ORDER BY n DESC"
    ):
        print(f"   {(r['stato'] or '-'):20s} {r['n']:>6}")

    if totale == 0:
        print("   Catalogo già vuoto.")
        return

    if not esegui:
        print("\n⚠️  ANTEPRIMA — il catalogo NON è stato toccato.")
        print("   Per svuotarlo (i file audio restano al loro posto):")
        print("   music_catalog.py reset --esegui\n")
        return

    risposta = input(f"   Svuoto il catalogo ({totale} brani)? (sì/no): ").strip().lower()
    if risposta not in ("sì", "si", "s", "y", "yes"):
        print("   Operazione annullata.")
        return

    conn.execute("DELETE FROM brani")
    conn.commit()
    conn.execute("VACUUM")
    print("✅ Catalogo svuotato. Ora riesegui 'scan' sulle cartelle da catalogare.")


# ---------------------------------------------------------------------------
# Comando: export
# ---------------------------------------------------------------------------
def cmd_export(args, conn):
    """Esporta il catalogo in formato CSV."""
    output = getattr(args, "output", None) or os.path.expanduser("~/music_catalog.csv")

    rows = conn.execute("""
        SELECT percorso, nome_file, formato, durata, bitrate,
               titolo, artista, album, album_artista, traccia, anno, genere,
               sug_titolo, sug_artista, sug_album, sug_anno, sug_genere,
               campi_mancanti, stato, musicbrainz_id
        FROM brani
        ORDER BY artista, album, traccia, titolo
    """).fetchall()

    if not rows:
        print("   Database vuoto.")
        return

    colonne = rows[0].keys()
    with open(output, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(colonne)
        for r in rows:
            writer.writerow([r[c] for c in colonne])

    print(f"\n✅ Esportati {len(rows)} brani in: {output}\n")


# ---------------------------------------------------------------------------
# Menu interattivo
# ---------------------------------------------------------------------------
def chiedi(prompt, default=""):
    """Chiede input all'utente con un valore di default."""
    if default:
        risposta = input(f"   {prompt} [{default}]: ").strip()
        return risposta if risposta else default
    return input(f"   {prompt}: ").strip()


def chiedi_si_no(prompt, default_si=True):
    """Chiede sì/no all'utente."""
    hint = "S/n" if default_si else "s/N"
    risposta = input(f"   {prompt} ({hint}): ").strip().lower()
    if not risposta:
        return default_si
    return risposta in ("sì", "si", "s", "y", "yes")


def mostra_stato_db(conn):
    """Mostra un riepilogo rapido del database."""
    totale = conn.execute("SELECT COUNT(*) FROM brani").fetchone()[0]
    if totale == 0:
        print("   📀 Database: vuoto")
        return totale
    incompleti = conn.execute(
        "SELECT COUNT(*) FROM brani WHERE campi_mancanti > 0"
    ).fetchone()[0]
    suggeriti = conn.execute(
        "SELECT COUNT(*) FROM brani WHERE stato = 'suggerito'"
    ).fetchone()[0]
    corretti = conn.execute(
        "SELECT COUNT(*) FROM brani WHERE stato = 'corretto'"
    ).fetchone()[0]
    print(f"   📀 Database: {totale} brani "
          f"({incompleti} incompleti, {suggeriti} con suggerimenti, {corretti} corretti)")
    return totale


def menu_interattivo():
    """Menu principale interattivo."""
    print("\n" + "=" * 50)
    print("  🎵  MUSIC CATALOG  —  Gestione libreria audio")
    print("=" * 50)

    # Verifica dipendenze
    if not HAS_MUTAGEN:
        print("\n  ⚠ Libreria 'mutagen' non trovata.")
        print("    Installa con: pip3 install -r ~/requirements_music.txt")
        print("    Poi riavvia lo script.\n")
        sys.exit(1)

    conn = init_db()

    try:
        while True:
            print("\n" + "-" * 50)
            totale_db = mostra_stato_db(conn)
            print("-" * 50)
            print("\n  Cosa vuoi fare?\n")
            print("  A  Analizza e migliora tag")
            print("     Scansiona i brani, cerca info mancanti su MusicBrainz,")
            print("     correggi i tag e organizza in struttura Apple Music")
            print()
            print("  B  Trova i duplicati nella libreria")
            print("     Cerca nel catalogo i brani doppioni (stesso artista+titolo+album):")
            print("     tiene il migliore e mette gli altri in una cartella di")
            print("     revisione, senza cancellare")
            print()
            print("  C  Esporta catalogo in CSV")
            print()
            print("  D  Tutto insieme (A + B + C)")
            print("     Pulisci duplicati, migliora tag, organizza e esporta")
            print()
            print("  Q  Esci")
            print()

            scelta = input("  Scelta (A/B/C/D/Q): ").strip().upper()

            if scelta == "Q":
                print("\n  Ciao! \U0001f44b\n")
                break

            elif scelta == "A":
                _menu_analizza(conn)

            elif scelta == "B":
                _menu_duplicati(conn)

            elif scelta == "C":
                output = chiedi("File CSV di output", os.path.expanduser("~/music_catalog.csv"))
                args = argparse.Namespace(output=output)
                cmd_export(args, conn)

            elif scelta == "D":
                _menu_tutto(conn)

            else:
                print("  \u274c Scelta non valida.")

    finally:
        conn.close()


def _menu_analizza(conn):
    """Sotto-menu: analisi e miglioramento tag."""
    print("\n" + "=" * 50)
    print("  🔍  ANALIZZA E MIGLIORA TAG")
    print("=" * 50)

    # Passo 1: Scansione
    print("\n  PASSO 1 — Scansione cartella\n")
    cartella = chiedi("Cartella da scansionare", os.path.expanduser("~/Music"))
    if not os.path.isdir(cartella):
        print(f"  ❌ '{cartella}' non esiste.")
        return

    args = argparse.Namespace(cartella=cartella)
    cmd_scan(args, conn)

    # Passo 2: Report
    print("\n  PASSO 2 — Report tag\n")
    args = argparse.Namespace()
    cmd_report(args, conn)

    # Passo 3: Lookup MusicBrainz
    incompleti = conn.execute(
        "SELECT COUNT(*) FROM brani WHERE campi_mancanti > 0"
    ).fetchone()[0]

    if incompleti == 0:
        print("  ✅ Tutti i brani hanno tag completi!")
    else:
        print(f"\n  PASSO 3 — Cercare info su MusicBrainz per {incompleti} brani incompleti?\n")
        if chiedi_si_no("Vuoi cercare le info mancanti online?"):
            if not HAS_MUSICBRAINZ:
                print("  ⚠ Serve 'musicbrainzngs': pip3 install musicbrainzngs")
            else:
                usa_acoustid = False
                api_key = None
                print()
                if HAS_ACOUSTID:
                    if chiedi_si_no("Vuoi usare anche AcoustID (fingerprint audio)?", default_si=False):
                        api_key = chiedi("API key AcoustID (da https://acoustid.org/new-application)")
                        if api_key:
                            usa_acoustid = True

                tutti = chiedi_si_no("Cercare anche per brani con tag già completi?", default_si=False)

                args = argparse.Namespace(
                    acoustid=usa_acoustid, api_key=api_key, tutti=tutti
                )
                cmd_lookup(args, conn)

                # Passo 4: Applica correzioni
                suggeriti = conn.execute(
                    "SELECT COUNT(*) FROM brani WHERE stato = 'suggerito'"
                ).fetchone()[0]

                if suggeriti > 0:
                    print(f"\n  PASSO 4 — {suggeriti} brani con correzioni proposte\n")
                    # Anteprima
                    args = argparse.Namespace(esegui=False)
                    cmd_fix(args, conn)

                    if chiedi_si_no("Vuoi applicare le correzioni ai file?"):
                        args = argparse.Namespace(esegui=True)
                        cmd_fix(args, conn)

    # Passo 5: Organizza in struttura Apple Music
    print(f"\n  PASSO 5 — Organizzare i brani in struttura Apple Music?\n")
    if chiedi_si_no("Vuoi copiare i brani in una nuova cartella organizzata?"):
        destinazione = chiedi("Cartella di destinazione", os.path.expanduser("~/Music_Organizzata"))
        print()
        sposta = chiedi_si_no("Spostare i file (sì) o copiarli (no)?", default_si=False)

        # Anteprima
        args = argparse.Namespace(destinazione=destinazione, sposta=sposta, esegui=False)
        cmd_organizza(args, conn)

        if chiedi_si_no("Procedere con l'operazione?"):
            args = argparse.Namespace(destinazione=destinazione, sposta=sposta, esegui=True)
            cmd_organizza(args, conn)

    print("\n  ✅ Analisi completata!\n")


def _menu_tutto(conn):
    """Esegue tutto in sequenza: duplicati → analisi tag → organizza → export."""
    print("\n" + "=" * 50)
    print("  \U0001f680  PROCEDURA COMPLETA")
    print("=" * 50)

    print("\n  Questa procedura esegue tutto in ordine:")
    print("    1. Pulizia duplicati (cos\u00ec lavoriamo su meno file)")
    print("    2. Scansione e analisi tag")
    print("    3. Ricerca info mancanti su MusicBrainz")
    print("    4. Correzione tag")
    print("    5. Organizzazione in struttura Apple Music")
    print("    6. Esportazione catalogo in CSV\n")

    if not chiedi_si_no("Vuoi procedere?"):
        return

    # --- Fase 1: Duplicati (prima, per ridurre i file da analizzare) ---
    print("\n" + "=" * 50)
    print("  FASE 1/6 \u2014 Pulizia duplicati")
    print("=" * 50)
    print("\n  Conviene eliminare prima i duplicati, cos\u00ec le fasi")
    print("  successive lavorano su meno file.\n")

    if chiedi_si_no("Vuoi cercare i duplicati nel catalogo?", default_si=True):
        _menu_duplicati(conn)
        print("\n  ✅ Duplicati gestiti. Ora analizziamo i brani rimasti.\n")

    # --- Fase 2-5: Analisi tag + organizzazione ---
    print("\n" + "=" * 50)
    print("  FASE 2-5 \u2014 Analisi, correzione tag e organizzazione")
    print("=" * 50)
    _menu_analizza(conn)

    # --- Fase 6: Export ---
    print("\n" + "=" * 50)
    print("  FASE 6/6 \u2014 Esportazione")
    print("=" * 50)
    print()
    if chiedi_si_no("Vuoi esportare il catalogo in CSV?"):
        output = chiedi("File CSV di output", os.path.expanduser("~/music_catalog.csv"))
        args = argparse.Namespace(output=output)
        cmd_export(args, conn)

    print("\n" + "=" * 50)
    print("  \u2705  PROCEDURA COMPLETA TERMINATA!")
    print("=" * 50 + "\n")


def _menu_duplicati(conn):
    """Sotto-menu: duplicati nella libreria, sul catalogo."""
    print("\n" + "=" * 50)
    print("  🔍  TROVA I DUPLICATI NELLA LIBRERIA")
    print("=" * 50)

    print("\n  Cerca nel catalogo i brani doppioni (stesso artista + titolo +")
    print("  album; live e versioni in album diversi non vengono fusi).")
    print("  Il brano di qualità migliore viene tenuto, gli altri vengono")
    print("  messi da parte, senza cancellare.\n")

    if conn.execute("SELECT COUNT(*) FROM brani").fetchone()[0] == 0:
        print("  ❌ Database vuoto. Esegui prima una scansione ('scan /cartella').")
        return

    # Anteprima
    args = argparse.Namespace(esegui=False, revisione=None)
    cmd_duplicati(args, conn)

    print("  Cosa vuoi fare?")
    print("  1  Spostare i duplicati in una cartella di revisione (consigliato)")
    print("  2  Eliminarli definitivamente")
    print("  3  Nulla, esci")
    scelta = input("  Scelta (1/2/3): ").strip()
    if scelta not in ("1", "2"):
        return

    if scelta == "1":
        revisione = chiedi("Cartella di revisione", os.path.expanduser("~/Musica_da_rivedere"))
        args = argparse.Namespace(esegui=True, revisione=revisione)
    else:
        args = argparse.Namespace(esegui=True, revisione=None)
    cmd_duplicati(args, conn)


# ---------------------------------------------------------------------------
# Main — supporta sia menu interattivo che comandi CLI
# ---------------------------------------------------------------------------
def main():
    # Se lanciato senza argomenti → menu interattivo
    if len(sys.argv) == 1:
        menu_interattivo()
        return

    # Altrimenti → modalità CLI classica
    parser = argparse.ArgumentParser(
        description="Music Catalog — Cataloga, analizza e corregge metadati audio.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Lancia senza argomenti per il menu interattivo.

Comandi CLI:
  scan      /cartella       Scansiona e cataloga
  report                    Report tag mancanti
  duplicati [--revisione DIR] [--esegui]  Trova i doppioni nella libreria
  lookup    [--acoustid]    Cerca su MusicBrainz/AcoustID
  fix       [--esegui]      Applica correzioni ai tag
  organizza /dest [--esegui] Copia in struttura Apple Music
  export    [file.csv]      Esporta in CSV""",
    )
    sub = parser.add_subparsers(dest="comando", help="Comando da eseguire")

    # scan
    p_scan = sub.add_parser("scan", help="Scansiona e cataloga una cartella")
    p_scan.add_argument("cartella", help="Cartella da scansionare")

    # report
    sub.add_parser("report", help="Report dei tag mancanti/sospetti")

    # duplicati
    p_dup = sub.add_parser(
        "duplicati", help="Trova i doppioni nella libreria (anteprima di default)"
    )
    p_dup.add_argument(
        "--revisione", dest="revisione", default=None,
        help="Cartella dove spostare i duplicati (invece di eliminarli)",
    )
    p_dup.add_argument("--esegui", action="store_true")

    # reset
    p_reset = sub.add_parser("reset", help="Svuota il catalogo (i file non vengono toccati)")
    p_reset.add_argument("--esegui", action="store_true")

    # lookup
    p_lookup = sub.add_parser("lookup", help="Cerca info su MusicBrainz/AcoustID")
    p_lookup.add_argument("--acoustid", action="store_true")
    p_lookup.add_argument("--api-key", dest="api_key")
    p_lookup.add_argument("--tutti", action="store_true")

    # fix
    p_fix = sub.add_parser("fix", help="Applica le correzioni proposte")
    p_fix.add_argument("--esegui", action="store_true")

    # organizza
    p_org = sub.add_parser("organizza", help="Struttura Apple Music")
    p_org.add_argument("destinazione")
    p_org.add_argument("--sposta", action="store_true")
    p_org.add_argument("--esegui", action="store_true")
    p_org.add_argument(
        "--piano", dest="piano", default=None,
        help="Invece di eseguire, scrive uno script shell con le operazioni",
    )

    # export
    p_export = sub.add_parser("export", help="Esporta catalogo in CSV")
    p_export.add_argument("output", nargs="?", default=None)

    args = parser.parse_args()

    if not args.comando:
        parser.print_help()
        sys.exit(0)

    if not HAS_MUTAGEN and args.comando in ("scan", "fix"):
        print("❌ Installa con: pip3 install -r ~/requirements_music.txt")
        sys.exit(1)

    conn = init_db()
    try:
        cmds = {
            "scan": cmd_scan, "report": cmd_report, "duplicati": cmd_duplicati,
            "lookup": cmd_lookup, "fix": cmd_fix, "organizza": cmd_organizza,
            "export": cmd_export, "reset": cmd_reset,
        }
        cmds[args.comando](args, conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
