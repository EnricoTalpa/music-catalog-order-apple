#!/usr/bin/env python3
"""
Duplicate Cleaner v2 - Confronta due cartelle e pulisce la cartella "leggera".

Filtri applicati (in ordine):
  1. Percorso identico  → stesso percorso relativo nelle due cartelle
  2. Nome con prefisso numerico → "03 Song.mp3" vs "Song.mp3" → elimina quello numerato
  3. Qualità audio → stesso brano, formati diversi → tiene Apple / bitrate più alto
  4. Hash esatto → file identici byte per byte

Uso:
    python3 duplicate_cleaner.py /cartella_master /cartella_leggera
    python3 duplicate_cleaner.py /cartella_master /cartella_leggera --esegui
"""

import os
import sys
import re
import hashlib
import argparse
import subprocess
from collections import defaultdict

# ---------------------------------------------------------------------------
# Configurazione audio
# ---------------------------------------------------------------------------
ESTENSIONI_AUDIO = {
    ".mp3", ".m4a", ".aac", ".flac", ".wav",
    ".aiff", ".aif", ".ogg", ".wma", ".caf", ".opus",
}

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
# Utilità
# ---------------------------------------------------------------------------
def formatta_dim(nbytes):
    for u in ("B", "KB", "MB", "GB", "TB"):
        if nbytes < 1024:
            return f"{nbytes:.1f} {u}"
        nbytes /= 1024
    return f"{nbytes:.1f} PB"


def è_audio(percorso):
    return os.path.splitext(percorso)[1].lower() in ESTENSIONI_AUDIO


def estrai_titolo(filename):
    """Estrae il titolo pulito da vari formati di nome file.

    Gestisce:
      '03 Song.mp3'              → 'song'
      '03-Song.mp3'              → 'song'
      'Artist - Album 01 Song.mp3' → 'song'
      'Artist - 01 Song.mp3'     → 'song'
      'A - A 01 Baciami.aac'     → 'baciami'
      'A - A 0 Baciami.mp3'      → 'baciami'
      'Song.mp3'                 → 'song'
    """
    name = os.path.splitext(filename)[0]

    # Pattern 1: "Artista - ... N Titolo" (numero traccia nel mezzo/fine)
    # Cerca l'ultimo numero di 1-3 cifre seguito da spazio/separatore e testo
    m = re.search(r'(?:^|[\s-])\d{1,3}[\s._-]+([^/]+)$', name)
    if m:
        return m.group(1).strip().lower()

    # Pattern 2: Prefisso numerico classico "03 Song"
    pulito = re.sub(r"^\d{1,3}[\s._-]+", "", name).strip()
    if pulito:
        return pulito.lower()

    return name.strip().lower()


def nome_base(filename):
    """Rimuove prefissi numerici di traccia (1-3 cifre + separatore).
    '03 Song.mp3' → 'song.mp3'
    '03-Song.mp3' → 'song.mp3'
    '12. Song.mp3' → 'song.mp3'
    'Song.mp3'     → 'song.mp3'
    """
    name, ext = os.path.splitext(filename)
    pulito = re.sub(r"^\d{1,3}[\s._-]+", "", name).strip()
    if not pulito:
        return filename.lower()
    return (pulito + ext).lower()


def nome_base_senza_ext(filename):
    """Estrae il titolo puro per confrontare brani tra formati diversi.
    Usa estrai_titolo per gestire anche nomi tipo 'A - A 01 Baciami.aac'.
    """
    return estrai_titolo(filename)


def ha_prefisso_numerico(filename):
    """Restituisce True se il nome ha un numero di traccia (inizio o nel mezzo)."""
    name = os.path.splitext(filename)[0]
    # Prefisso classico: "03 Song"
    if re.match(r"^\d{1,3}[\s._-]+", name):
        return True
    # Numero nel mezzo: "Artist - Album 01 Song" o "A - A 0 Song"
    if re.search(r'(?:^|[\s-])\d{1,3}[\s._-]+[A-Za-z]', name):
        return True
    return False


# ---------------------------------------------------------------------------
# Info audio (usa afinfo di macOS, built-in)
# ---------------------------------------------------------------------------
_cache_bitrate = {}


def get_bitrate(filepath):
    """Ottiene il bitrate usando afinfo (macOS). Risultato in cache."""
    if filepath in _cache_bitrate:
        return _cache_bitrate[filepath]
    bitrate = 0
    try:
        r = subprocess.run(
            ["afinfo", filepath],
            capture_output=True, text=True, timeout=10,
        )
        for linea in r.stdout.splitlines():
            if "bit rate:" in linea.lower():
                m = re.search(r"(\d+)", linea)
                if m:
                    bitrate = int(m.group(1))
                    break
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    _cache_bitrate[filepath] = bitrate
    return bitrate


def punteggio_audio(filepath):
    """Punteggio qualità audio: formato Apple/lossless + bitrate. Più alto = meglio."""
    ext = os.path.splitext(filepath)[1].lower()
    base = PRIORITA_FORMATO.get(ext, 20)
    bitrate = get_bitrate(filepath)
    # Normalizza bitrate su scala 0-50 (CD quality ≈ 1411 kbps)
    bonus = min(bitrate / 28000, 50) if bitrate > 0 else 0
    return base + bonus


# ---------------------------------------------------------------------------
# Hash
# ---------------------------------------------------------------------------
def calcola_hash(filepath, blocksize=65536):
    sha256 = hashlib.sha256()
    try:
        with open(filepath, "rb") as f:
            while True:
                blocco = f.read(blocksize)
                if not blocco:
                    break
                sha256.update(blocco)
    except (OSError, PermissionError) as e:
        print(f"  ⚠ Errore leggendo {filepath}: {e}")
        return None
    return sha256.hexdigest()


# ---------------------------------------------------------------------------
# Scansione
# ---------------------------------------------------------------------------
def scansiona(cartella):
    """Restituisce dict: percorso_relativo → percorso_assoluto."""
    risultato = {}
    cartella = os.path.abspath(cartella)
    for root, _, files in os.walk(cartella):
        for nome in files:
            abs_path = os.path.join(root, nome)
            if os.path.islink(abs_path):
                continue
            rel_path = os.path.relpath(abs_path, cartella)
            risultato[rel_path] = abs_path
    return risultato


# ---------------------------------------------------------------------------
# Logica principale
# ---------------------------------------------------------------------------
def trova_da_eliminare(cartella_master, cartella_leggera):
    print(f"\n📂 Master (intoccabile): {cartella_master}")
    print(f"📂 Leggera (da pulire):  {cartella_leggera}\n")

    print("🔍 Scansione cartelle...")
    master_files = scansiona(cartella_master)
    leggera_files = scansiona(cartella_leggera)
    print(f"   Master: {len(master_files)} file")
    print(f"   Leggera: {len(leggera_files)} file\n")

    da_eliminare = {}  # abs_path → motivo

    # ------------------------------------------------------------------
    # FILTRO 1: Percorso relativo identico
    # ------------------------------------------------------------------
    print("📋 Filtro 1 — Percorso identico...")
    percorsi_comuni = set(master_files.keys()) & set(leggera_files.keys())
    for rel in percorsi_comuni:
        da_eliminare[leggera_files[rel]] = "percorso identico alla master"
    print(f"   → {len(percorsi_comuni)} file con percorso identico\n")

    # ------------------------------------------------------------------
    # FILTRO 2: Nome con numero traccia (inizio o mezzo)
    # Confronta per titolo estratto + estensione.
    # Es: 'A - A 01 Baciami.aac' → titolo='baciami', ext='.aac'
    #     'Baciami.aac'           → titolo='baciami', ext='.aac' → match!
    # ------------------------------------------------------------------
    print("🔢 Filtro 2 — Numeri traccia ('03 Song.mp3', 'A - A 01 Song.mp3'...)...")
    conteggio_num = 0

    # 2a: leggera ha versione numerata, master ha versione pulita
    # Indice master: (titolo_estratto, estensione) → lista percorsi senza numero
    master_per_titolo = defaultdict(list)
    for rel, absp in master_files.items():
        nome_file = os.path.basename(rel)
        if not ha_prefisso_numerico(nome_file):
            ext = os.path.splitext(nome_file)[1].lower()
            titolo = estrai_titolo(nome_file)
            master_per_titolo[(titolo, ext)].append(absp)

    for rel, absp in leggera_files.items():
        if absp in da_eliminare:
            continue
        nome_file = os.path.basename(rel)
        if ha_prefisso_numerico(nome_file):
            ext = os.path.splitext(nome_file)[1].lower()
            titolo = estrai_titolo(nome_file)
            if (titolo, ext) in master_per_titolo:
                da_eliminare[absp] = f"ha numero traccia, esiste '{titolo}{ext}' nella master"
                conteggio_num += 1

    # 2b: dentro la leggera — file con numero vs file senza nella stessa cartella
    leggera_per_dir = defaultdict(list)
    for rel, absp in leggera_files.items():
        leggera_per_dir[os.path.dirname(rel)].append((rel, absp))

    for dir_rel, files_in_dir in leggera_per_dir.items():
        # Indice dei file SENZA numero: (titolo, ext) → percorso
        puliti = {}
        for rel, absp in files_in_dir:
            nome_file = os.path.basename(rel)
            if not ha_prefisso_numerico(nome_file):
                ext = os.path.splitext(nome_file)[1].lower()
                titolo = estrai_titolo(nome_file)
                puliti[(titolo, ext)] = absp

        for rel, absp in files_in_dir:
            if absp in da_eliminare:
                continue
            nome_file = os.path.basename(rel)
            if ha_prefisso_numerico(nome_file):
                ext = os.path.splitext(nome_file)[1].lower()
                titolo = estrai_titolo(nome_file)
                if (titolo, ext) in puliti:
                    da_eliminare[absp] = f"ha numero traccia, esiste '{titolo}{ext}' nella stessa cartella"
                    conteggio_num += 1

    print(f"   → {conteggio_num} file con numero traccia\n")

    # ------------------------------------------------------------------
    # FILTRO 3: Qualità audio — stesso brano, tiene il migliore
    # ------------------------------------------------------------------
    print("🎵 Filtro 3 — Qualità audio (tiene Apple / bitrate più alto)...")
    conteggio_audio = 0

    # 3a: master vs leggera
    master_audio_per_brano = defaultdict(list)
    for rel, absp in master_files.items():
        if è_audio(absp):
            brano = nome_base_senza_ext(os.path.basename(rel))
            master_audio_per_brano[brano].append(absp)

    for rel, absp in leggera_files.items():
        if absp in da_eliminare or not è_audio(absp):
            continue
        brano = nome_base_senza_ext(os.path.basename(rel))
        if brano in master_audio_per_brano:
            migliore_master = max(master_audio_per_brano[brano], key=punteggio_audio)
            if punteggio_audio(migliore_master) >= punteggio_audio(absp):
                ext_m = os.path.splitext(migliore_master)[1]
                ext_l = os.path.splitext(absp)[1]
                br_m = get_bitrate(migliore_master)
                br_l = get_bitrate(absp)
                da_eliminare[absp] = (
                    f"qualità inferiore ({ext_l} {br_l // 1000}kbps) "
                    f"vs master ({ext_m} {br_m // 1000}kbps)"
                )
                conteggio_audio += 1

    # 3b: dentro la leggera — più versioni dello stesso brano
    leggera_audio_per_brano = defaultdict(list)
    for rel, absp in leggera_files.items():
        if absp not in da_eliminare and è_audio(absp):
            brano = nome_base_senza_ext(os.path.basename(rel))
            leggera_audio_per_brano[brano].append(absp)

    for brano, lista in leggera_audio_per_brano.items():
        if len(lista) < 2:
            continue
        lista_ord = sorted(lista, key=punteggio_audio, reverse=True)
        migliore = lista_ord[0]
        for inferiore in lista_ord[1:]:
            if inferiore not in da_eliminare:
                ext_m = os.path.splitext(migliore)[1]
                ext_i = os.path.splitext(inferiore)[1]
                br_m = get_bitrate(migliore)
                br_i = get_bitrate(inferiore)
                da_eliminare[inferiore] = (
                    f"duplicato audio ({ext_i} {br_i // 1000}kbps) "
                    f"vs ({ext_m} {br_m // 1000}kbps) nella leggera"
                )
                conteggio_audio += 1

    print(f"   → {conteggio_audio} file audio di qualità inferiore\n")

    # ------------------------------------------------------------------
    # FILTRO 4: Hash SHA-256 (duplicati esatti rimasti)
    # ------------------------------------------------------------------
    print("🔐 Filtro 4 — Hash SHA-256 (duplicati esatti)...")

    master_per_dim = defaultdict(list)
    for absp in master_files.values():
        try:
            master_per_dim[os.path.getsize(absp)].append(absp)
        except OSError:
            continue

    leggera_per_dim = defaultdict(list)
    for rel, absp in leggera_files.items():
        if absp in da_eliminare:
            continue
        try:
            leggera_per_dim[os.path.getsize(absp)].append(absp)
        except OSError:
            continue

    dim_comuni = set(master_per_dim.keys()) & set(leggera_per_dim.keys())
    candidati = sum(len(leggera_per_dim[d]) for d in dim_comuni)
    print(f"   {candidati} file candidati per dimensione...")

    hash_master = set()
    for dim in dim_comuni:
        for absp in master_per_dim[dim]:
            h = calcola_hash(absp)
            if h:
                hash_master.add(h)

    conteggio_hash = 0
    cnt = 0
    for dim in dim_comuni:
        for absp in leggera_per_dim[dim]:
            h = calcola_hash(absp)
            if h and h in hash_master:
                da_eliminare[absp] = "duplicato esatto (hash identico)"
                conteggio_hash += 1
            cnt += 1
            if cnt % 500 == 0:
                print(f"   ...{cnt}/{candidati} analizzati")

    print(f"   → {conteggio_hash} duplicati esatti\n")

    return da_eliminare


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Confronta due cartelle e rimuove i duplicati dalla 'leggera'.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Filtri applicati:
  1. Percorso identico tra master e leggera
  2. File con prefisso numerico (es. '03 Song.mp3') quando esiste 'Song.mp3'
  3. File audio di qualità inferiore (tiene formato Apple / bitrate più alto)
  4. Duplicati esatti per hash SHA-256""",
    )
    parser.add_argument("master", help="Cartella master (NON verrà modificata)")
    parser.add_argument("leggera", help="Cartella leggera (i duplicati verranno rimossi da qui)")
    parser.add_argument(
        "--esegui", action="store_true",
        help="Esegue la cancellazione. Senza questo flag, mostra solo un'anteprima.",
    )
    args = parser.parse_args()

    for c in (args.master, args.leggera):
        if not os.path.isdir(c):
            print(f"❌ Errore: '{c}' non è una cartella valida.")
            sys.exit(1)

    master = os.path.abspath(args.master)
    leggera = os.path.abspath(args.leggera)
    if master == leggera:
        print("❌ Errore: le due cartelle devono essere diverse.")
        sys.exit(1)

    da_eliminare = trova_da_eliminare(master, leggera)

    if not da_eliminare:
        print("✅ Nessun duplicato trovato!")
        return

    # Riepilogo
    spazio = sum(os.path.getsize(f) for f in da_eliminare if os.path.exists(f))
    print("=" * 60)
    print(f"🗑  Totale: {len(da_eliminare)} file da eliminare dalla leggera")
    print(f"💾 Spazio recuperabile: {formatta_dim(spazio)}")
    print("=" * 60)

    # Raggruppa per motivo
    per_motivo = defaultdict(list)
    for absp, motivo in da_eliminare.items():
        per_motivo[motivo].append(absp)

    print("\n--- Dettaglio per motivo ---")
    for motivo, files in sorted(per_motivo.items()):
        print(f"\n  [{len(files)} file] {motivo}")
        for i, f in enumerate(sorted(files)):
            rel = os.path.relpath(f, leggera)
            dim = formatta_dim(os.path.getsize(f)) if os.path.exists(f) else "?"
            print(f"    {i + 1:>4}. [{dim:>10}] {rel}")
            if i >= 29 and not args.esegui:
                restanti = len(files) - 30
                if restanti > 0:
                    print(f"          ... e altri {restanti} file")
                break

    if not args.esegui:
        print(f"\n⚠️  Modalità ANTEPRIMA — nessun file eliminato.")
        print(f"   Per eliminare, riesegui con --esegui:")
        print(f'   python3 duplicate_cleaner.py "{master}" "{leggera}" --esegui\n')
        return

    # Conferma
    print(f"\n⚠️  Stai per eliminare {len(da_eliminare)} file dalla cartella leggera.")
    risposta = input("   Confermi? (sì/no): ").strip().lower()
    if risposta not in ("sì", "si", "s", "y", "yes"):
        print("   Operazione annullata.")
        return

    eliminati = errori = 0
    for f in da_eliminare:
        try:
            os.remove(f)
            eliminati += 1
        except OSError as e:
            print(f"  ⚠ Errore eliminando {f}: {e}")
            errori += 1

    print(f"\n✅ Completato: {eliminati} file eliminati, {errori} errori.")
    print(f"💾 Spazio recuperato: {formatta_dim(spazio)}")


if __name__ == "__main__":
    main()
