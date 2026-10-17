#!/usr/bin/env python3
"""Trova i file audio rotti (troppo piccoli) e li ripara con una copia sana.

I download cloud interrotti lasciano file di 0-30 KB che non suonano nulla,
ma col formato "buono" (m4a) possono persino vincere i confronti di qualità
(è successo: un m4a di 20 KB ha battuto un mp3 sano).

Questo strumento:

1. cerca i file audio sotto la soglia (default 100 KB) in una cartella
2. per ognuno cerca nel catalogo una copia SANA dello stesso brano
   (stesso artista + titolo + album, dimensione sensata, file esistente),
   tra tutte le righe: libreria unita, Da_Rivedere, sorgenti
3. con --esegui: il rotto va in --rotti-dir (mai cancellato), la copia
   sana prende il suo posto con lo stesso percorso e nome

Uso:
    python3 ripara_rotti.py /cartella                     # solo rapporto
    python3 ripara_rotti.py /cartella --esegui            # ripara
    python3 ripara_rotti.py /cartella --esegui --rotti-dir /rotti
"""

import os
import sys
import shutil
import argparse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import music_catalog as mc  # noqa: E402

ESTENSIONI = mc.ESTENSIONI_AUDIO


def principale():
    parser = argparse.ArgumentParser(description="Ripara i file audio rotti")
    parser.add_argument("cartella")
    parser.add_argument("--soglia-kb", dest="soglia_kb", type=int, default=100)
    parser.add_argument("--min-sano-kb", dest="min_sano_kb", type=int, default=500)
    parser.add_argument("--rotti-dir", dest="rotti_dir", default=None)
    parser.add_argument("--esegui", action="store_true")
    args = parser.parse_args()

    radice = os.path.abspath(args.cartella)
    soglia = args.soglia_kb * 1024
    min_sano = args.min_sano_kb * 1024

    # 1. trova i rotti
    rotti = []
    for root, _, files in os.walk(radice):
        for nome in files:
            p = os.path.join(root, nome)
            if os.path.splitext(nome)[1].lower() not in ESTENSIONI:
                continue
            try:
                dim = os.path.getsize(p)
            except OSError:
                continue
            if dim < soglia:
                rotti.append((p, dim))
    rotti.sort()
    print(f"Cartella: {radice}")
    print(f"File sospetti (< {args.soglia_kb} KB): {len(rotti)}\n")

    if not rotti:
        print("✅ Nessun file sospetto.")
        return

    conn = mc.init_db()

    recuperabili = []
    non_ricuperabili = []
    for p, dim in rotti:
        # info del brano rotto dal catalogo
        row = conn.execute(
            "SELECT titolo, artista, album FROM brani WHERE percorso = ?", (p,)
        ).fetchone()
        if row is None:
            non_ricuperabili.append((p, dim, "non nel catalogo"))
            continue
        t = mc.normalizza_testo(row["titolo"])
        a = mc.normalizza_testo(row["artista"])
        al = mc.normalizza_testo(row["album"])
        if not t:
            non_ricuperabili.append((p, dim, "senza tag titolo"))
            continue
        # copie sane dello stesso brano, ovunque (libreria, Da_Rivedere, sorgenti)
        sane = []
        for r in conn.execute("""
            SELECT percorso, dimensione, bitrate, formato, nome_file, stato,
                   titolo, artista, album
            FROM brani
        """):
            if r["percorso"] == p or not r["percorso"]:
                continue
            # prima il confronto dei tag (economico), poi l'esistenza
            rt = mc.normalizza_testo(r["titolo"])
            if rt != t:
                continue
            ra = mc.normalizza_testo(r["artista"])
            ral = mc.normalizza_testo(r["album"])
            if a and ra and ra != a:
                continue
            if al and ral and ral != al:
                continue
            if (r["dimensione"] or 0) < min_sano:
                continue
            if not os.path.exists(r["percorso"]):
                continue
            sane.append(r)
        if sane:
            migliore = max(sane, key=mc.punteggio_brano)
            recuperabili.append((p, dim, migliore))
        else:
            non_ricuperabili.append((p, dim, "nessuna copia sana trovata"))

    # --- rapporto ---
    print("--- Riparabili ---")
    for p, dim, sano in recuperabili:
        print(f"  ✅ {os.path.relpath(p, radice)}  ({dim // 1024} KB)")
        print(f"     ← {sano['percorso']}  ({sano['dimensione'] // 1024} KB)")
    print("\n--- NON riparabili (nessuna copia sana) ---")
    for p, dim, motivo in non_ricuperabili:
        print(f"  ❌ {os.path.relpath(p, radice)}  ({dim // 1024} KB)  [{motivo}]")

    if not args.esegui:
        print(f"\n⚠️  Rapporto soltanto — {len(recuperabili)} riparabili, "
              f"{len(non_ricuperabili)} no. Aggiungi --esegui per riparare.")
        conn.close()
        return

    # --- esecuzione ---
    rotti_dir = os.path.abspath(args.rotti_dir or os.path.join(radice, "..", "Da_Rivedere", "rotti"))
    sostituiti = errori = 0
    for p, dim, sano in recuperabili:
        try:
            rel = os.path.relpath(p, radice)
            dest_rotto = os.path.join(rotti_dir, rel)
            os.makedirs(os.path.dirname(dest_rotto), exist_ok=True)
            n = 2
            while os.path.exists(dest_rotto):
                base, ext = os.path.splitext(dest_rotto)
                dest_rotto = f"{base} ({n}){ext}"
                n += 1
            shutil.move(p, dest_rotto)
            shutil.copy2(sano["percorso"], p)
            sostituiti += 1
        except (OSError, shutil.Error) as e:
            print(f"  ⚠ Errore su {p}: {e}")
            errori += 1
    print(f"\n✅ {sostituiti} file riparati, {errori} errori.")
    print(f"   I rotti sono in: {rotti_dir}")
    conn.close()


if __name__ == "__main__":
    principale()
