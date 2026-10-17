#!/usr/bin/env python3
"""Test end-to-end del comando 'duplicati' di music_catalog.py.

Crea una libreria di test, la scandisce in un DB di test (non tocca il DB vero),
lancia 'duplicati' in anteprima e poi in modalità --revisione --esegui,
e verifica i risultati attesi.
"""
import os
import sys
import csv
import argparse
import tempfile

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = os.path.join(tempfile.gettempdir(), "test_duplicati")

sys.path.insert(0, PROJ)
import music_catalog as mc  # noqa: E402
from mutagen import File as MutagenFile  # noqa: E402

LIB = os.path.join(TMP, "lib")
REV = os.path.join(TMP, "revisione")
DB = os.path.join(TMP, "test_catalog.db")

# (percorso, tags) — None = senza tag
FILE_TEST = [
    ("Album A/03 Song.mp3", {"artist": "X", "title": "Song", "album": "Greatest"}),   # dup: numero + qualità
    ("Album A/Song.m4a",    {"artist": "X", "title": "Song", "album": "Greatest"}),   # TENUTO (m4a > mp3)
    ("Album B/07 Song.mp3", {"artist": "X", "title": "Song", "album": "Greatest"}),   # dup: numero traccia
    ("Album C/12 Another.mp3", None),                            # dup: fallback nome file
    ("Album C/Another.mp3", None),                              # TENUTO: fallback
    ("Album D/Love.mp3",    {"artist": "X", "title": "Love", "album": "Album D"}),   # TENUTO
    ("Album E/Love.mp3",    {"artist": "Y", "title": "Love", "album": "Album E"}),   # TENUTO: altro artista!
    ("Album F/Love.mp3",    {"artist": "X", "title": "Love", "album": "Album F"}),   # TENUTO: stesso artista, ALTRO ALBUM (registrazione diversa!)
]


def crea_libreria():
    import shutil
    if os.path.exists(TMP):
        shutil.rmtree(TMP)
    os.makedirs(LIB)
    for rel, tags in FILE_TEST:
        path = os.path.join(LIB, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        ext = os.path.splitext(path)[1]
        codec = ["-c:a", "libmp3lame"] if ext == ".mp3" else ["-c:a", "aac"]
        rc = os.system(
            f'ffmpeg -v error -y -f lavfi -i "sine=frequency=440:duration=1" '
            f'{" ".join(codec)} -b:a 128k "{path}"'
        )
        assert rc == 0, f"ffmpeg fallito su {path}"
        if tags:
            a = MutagenFile(path, easy=True)
            for k, v in tags.items():
                a[k] = v
            a.save()
    print(f"✅ Libreria di test creata: {LIB} ({len(FILE_TEST)} brani)\n")


def verifica(risultato, atteso, descrizione):
    ok = risultato == atteso
    print(f"   {'✅' if ok else '❌'} {descrizione}: {risultato!r}"
          + ("" if ok else f"  (atteso {atteso!r})"))
    return ok


def main():
    crea_libreria()

    conn = mc.init_db(DB)

    print("=" * 60)
    print("TEST 1 — scan della libreria di test")
    print("=" * 60)
    mc.cmd_scan(argparse.Namespace(cartella=LIB), conn)

    print("\n" + "=" * 60)
    print("TEST 2 — duplicati in ANTEPRIMA (non tocca nulla)")
    print("=" * 60)
    mc.cmd_duplicati(argparse.Namespace(esegui=False, revisione=None), conn)

    tutti_ok = True

    # I file originali devono esserci ancora tutti
    esistenti = [rel for rel, _ in FILE_TEST if os.path.exists(os.path.join(LIB, rel))]
    tutti_ok &= verifica(len(esistenti), len(FILE_TEST), "tutti i file ancora in libreria dopo anteprima")

    print("\n" + "=" * 60)
    print("TEST 3 — duplicati --revisione --esegui (sposta, non cancella)")
    print("=" * 60)
    mc.cmd_duplicati(argparse.Namespace(esegui=True, revisione=REV), conn)

    print("\n" + "=" * 60)
    print("VERIFICHE")
    print("=" * 60)

    attesi_tenuti = {
        "Album A/Song.m4a", "Album C/Another.mp3",
        "Album D/Love.mp3", "Album E/Love.mp3", "Album F/Love.mp3",
    }
    attesi_spostati = {
        "Album A/03 Song.mp3", "Album B/07 Song.mp3", "Album C/12 Another.mp3",
    }

    # 1. I tenuti sono ancora in libreria
    for rel in attesi_tenuti:
        tutti_ok &= verifica(
            os.path.exists(os.path.join(LIB, rel)), True, f"tenuto resta in libreria: {rel}"
        )

    # 2. I duplicati non sono più in libreria
    for rel in attesi_spostati:
        tutti_ok &= verifica(
            os.path.exists(os.path.join(LIB, rel)), False, f"duplicato rimosso da libreria: {rel}"
        )

    # 3. I duplicati sono nella cartella revisione
    trovati_rev = set()
    for root, _, files in os.walk(REV):
        for f in files:
            trovati_rev.add(os.path.relpath(os.path.join(root, f), REV))
    print(f"   📁 Contenuto revisione: {sorted(trovati_rev)}")
    tutti_ok &= verifica(len([f for f in trovati_rev if f.endswith('.mp3') or f.endswith('.m4a')]),
                         3, "3 file spostati in revisione")

    # 4. Il manifest CSV esiste ed elenca le origini
    manifest_path = os.path.join(REV, "da_rivedere.csv")
    tutti_ok &= verifica(os.path.exists(manifest_path), True, "manifest da_rivedere.csv creato")
    if os.path.exists(manifest_path):
        with open(manifest_path, newline="", encoding="utf-8") as f:
            righe = list(csv.DictReader(f))
        print(f"   📄 Manifest: {len(righe)} righe")
        tutti_ok &= verifica(len(righe), 3, "manifest con 3 voci")

    # 5. Il DB riflette lo spostamento
    n_rev = conn.execute("SELECT COUNT(*) FROM brani WHERE stato = 'in_revisione'").fetchone()[0]
    tutti_ok &= verifica(n_rev, 3, "3 brani marcati 'in_revisione' nel DB")

    # 6. Dopo pulizia: nessun duplicato residuo
    print("\n" + "=" * 60)
    print("TEST 4 — dopo pulizia: nessun duplicato residuo")
    print("=" * 60)
    residui = mc._trova_duplicati(conn)
    tutti_ok &= verifica(len(residui), 0, "duplicati residui dopo la pulizia")

    conn.close()
    print("\n" + "=" * 60)
    print(f"{'✅ TUTTI I TEST PASSATI' if tutti_ok else '❌ ALCUNI TEST FALLITI'}")
    print("=" * 60)
    sys.exit(0 if tutti_ok else 1)


if __name__ == "__main__":
    main()
