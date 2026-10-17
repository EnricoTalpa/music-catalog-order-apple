#!/usr/bin/env python3
"""Riscrive i percorsi del catalogo da forma server a forma Mac.

La scansione lato server registra percorsi tipo /mnt/storage/...;
dal Mac la stessa libreria è montata come /Volumes/Storage/....
Questo script traduce il prefisso, così il catalogo è usabile dal Mac.

Uso:  python3 fix_percorsi.py [--inverti]
      --inverti riporta i percorsi in forma server (/mnt/storage)
"""

import os
import sys
import sqlite3
import argparse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "music_catalog.db")

DA = "/mnt/storage"
A = "/Volumes/Storage"


def main():
    parser = argparse.ArgumentParser(description="Traduce i percorsi del catalogo")
    parser.add_argument("--inverti", action="store_true",
                        help="Riporta i percorsi in forma server (/mnt/storage)")
    parser.add_argument("--da", dest="da", default=DA,
                        help=f"Prefisso da tradurre (default: {DA})")
    parser.add_argument("--a", dest="a", default=A,
                        help=f"Prefisso di destinazione (default: {A})")
    args = parser.parse_args()

    vecchio, nuovo = (args.a, args.da) if args.inverti else (args.da, args.a)

    conn = sqlite3.connect(DB_PATH)
    righe = conn.execute("SELECT COUNT(*) FROM brani").fetchone()[0]
    da_modificare = conn.execute(
        "SELECT COUNT(*) FROM brani WHERE percorso LIKE ?", (vecchio + "/%",)
    ).fetchone()[0]
    print(f"Brani nel catalogo: {righe}")
    print(f"Percorsi che iniziano per {vecchio}: {da_modificare}")

    if da_modificare == 0:
        print("Niente da tradurre.")
        conn.close()
        return

    conn.execute(
        f"UPDATE brani SET percorso = ? || substr(percorso, ?) WHERE percorso LIKE ?",
        (nuovo, len(vecchio) + 1, vecchio + "/%"),
    )
    conn.commit()
    verifica = conn.execute(
        "SELECT COUNT(*) FROM brani WHERE percorso LIKE ?", (nuovo + "/%",)
    ).fetchone()[0]
    print(f"✅ Tradotti: {da_modificare} → ora {verifica} percorsi iniziano per {nuovo}")
    conn.close()


if __name__ == "__main__":
    main()
