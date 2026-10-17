#!/usr/bin/env python3
"""Test dello scenario reale: DUE cartelle → UNA sola unita senza doppioni.

1. Crea due cartelle (A e B) con brani sovrapposti:
   stesso brano in entrambe (qualità diversa), brani solo-A, solo-B,
   stesso titolo ma artisti diversi (NON è un doppione).
2. Le scandisce entrambe nello stesso catalogo.
3. organizza in una cartella unita (copia, struttura Apple Music).
4. Verifica: ogni brano una sola volta, tenuta la copia migliore,
   nessun file "(2)", artisti distinti non fusi, originali intatti.
"""
import os
import sys
import argparse
import builtins
import tempfile

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = os.path.join(tempfile.gettempdir(), "test_merge")

sys.path.insert(0, PROJ)
import music_catalog as mc  # noqa: E402
from mutagen import File as MutagenFile  # noqa: E402

A = os.path.join(TMP, "cartella_A")
B = os.path.join(TMP, "cartella_B")
UNITA = os.path.join(TMP, "unita")
DB = os.path.join(TMP, "test_catalog.db")

# (cartella, percorso, tags) — nessun doppione interno alle singole cartelle
FILE_TEST = [
    # stesso brano in A e B: B ha l'm4a (qualità migliore) → deve tenersi quello
    (A, "X/Album/03 Song.mp3",   {"artist": "X", "title": "Song",   "album": "Album",   "tracknumber": "3"}),
    (B, "X/Album/03 Song.m4a",  {"artist": "X", "title": "Song",   "album": "Album",   "tracknumber": "3"}),
    # solo in A
    (A, "X/Album/04 Another.mp3", {"artist": "X", "title": "Another", "album": "Album", "tracknumber": "4"}),
    # solo in B
    (B, "X/Album/05 Third.m4a",  {"artist": "X", "title": "Third",  "album": "Album",   "tracknumber": "5"}),
    # stesso titolo, artisti DIVERSI → entrambi vanno tenuti
    (A, "X/Album/06 Love.mp3",   {"artist": "X", "title": "Love",   "album": "Album",   "tracknumber": "6"}),
    (B, "Y/Album Y/07 Love.mp3", {"artist": "Y", "title": "Love",   "album": "Album Y", "tracknumber": "7"}),
]


def crea_cartelle():
    import shutil
    if os.path.exists(TMP):
        shutil.rmtree(TMP)
    for base, rel, tags in FILE_TEST:
        path = os.path.join(base, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        ext = os.path.splitext(path)[1]
        codec = ["-c:a", "libmp3lame"] if ext == ".mp3" else ["-c:a", "aac"]
        rc = os.system(
            f'ffmpeg -v error -y -f lavfi -i "sine=frequency=440:duration=1" '
            f'{" ".join(codec)} -b:a 128k "{path}"'
        )
        assert rc == 0, f"ffmpeg fallito su {path}"
        a = MutagenFile(path, easy=True)
        for k, v in tags.items():
            a[k] = v
        a.save()
    print(f"✅ Cartelle di test create:\n   A: {A}\n   B: {B}\n")


def tutti_file(radice):
    trovati = []
    for root, _, files in os.walk(radice):
        for f in files:
            trovati.append(os.path.relpath(os.path.join(root, f), radice))
    return sorted(trovati)


def verifica(risultato, atteso, descrizione):
    ok = risultato == atteso
    print(f"   {'✅' if ok else '❌'} {descrizione}: {risultato!r}"
          + ("" if ok else f"  (atteso {atteso!r})"))
    return ok


def main():
    crea_cartelle()
    builtins.input = lambda *a, **k: "si"  # conferma automatica per --esegui

    conn = mc.init_db(DB)

    print("=" * 60)
    print("PASSO 1 — scan della cartella A")
    print("=" * 60)
    mc.cmd_scan(argparse.Namespace(cartella=A), conn)

    print("=" * 60)
    print("PASSO 2 — scan della cartella B (stesso catalogo)")
    print("=" * 60)
    mc.cmd_scan(argparse.Namespace(cartella=B), conn)

    print("\n" + "=" * 60)
    print("PASSO 3 — organizza: le due cartelle in una sola (copia)")
    print("=" * 60)
    mc.cmd_organizza(argparse.Namespace(destinazione=UNITA, sposta=False, esegui=True), conn)

    print("\n" + "=" * 60)
    print("VERIFICHE sulla cartella unita")
    print("=" * 60)

    ok = True
    file_unita = tutti_file(UNITA)
    print(f"   📁 Contenuto di 'unita':\n       " + "\n       ".join(file_unita) + "\n")

    # 1. Nessun file "(2)" (sintomo di doppioni copiati)
    con_2 = [f for f in file_unita if "(2)" in f]
    ok &= verifica(con_2, [], "nessun file '(2)' (doppioni copiati)")

    # 2. Di 'Song' esiste una sola versione: l'm4a (il migliore), non l'mp3
    song = [f for f in file_unita if "Song." in f]
    ok &= verifica(song, ["X/Album/03 Song.m4a"], "di 'Song' solo la copia migliore (m4a)")

    # 3. Brani unici di A e B presenti entrambi
    ok &= verifica(any("Another" in f for f in file_unita), True, "'Another' (solo A) presente")
    ok &= verifica(any("Third" in f for f in file_unita), True, "'Third' (solo B) presente")

    # 4. Stesso titolo, artisti diversi: ENTRAMBI presenti (non è un doppione)
    love = [f for f in file_unita if "Love" in f]
    ok &= verifica(len(love), 2, "i due 'Love' di artisti diversi entrambi presenti")

    # 5. Totale: 5 brani (6 file meno 1 doppione)
    ok &= verifica(len(file_unita), 5, "5 brani totali nella cartella unita")

    # 6. Struttura Apple Music: Artista/Album/brani
    struttura_ok = all(len(f.split(os.sep)) == 3 for f in file_unita)
    ok &= verifica(struttura_ok, True, "struttura Artista/Album/Brano")

    # 7. Le cartelle originali A e B restano intatte (copia, non sposta)
    originali = [os.path.join(base, rel) for base, rel, _ in FILE_TEST]
    ok &= verifica(all(os.path.exists(f) for f in originali), True, "originali di A e B intatti")

    # 8. Anche l'anteprima di 'duplicati' ora non segnala nulla di residuo
    residui = mc._trova_duplicati(conn)
    ok &= verifica(len(residui), 1, "anteprima duplicati: 1 gruppo residuo (l'mp3 di Song)")

    conn.close()
    print("\n" + "=" * 60)
    print(f"{'✅ TUTTI I TEST PASSATI' if ok else '❌ ALCUNI TEST FALLITI'}")
    print("=" * 60)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
