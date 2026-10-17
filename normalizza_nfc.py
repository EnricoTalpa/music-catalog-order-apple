#!/usr/bin/env python3
"""Normalizza in NFC (Unicode precomposto) i nomi di file e cartelle.

Su alcuni dischi convivono la forma NFC ('à' = 1 carattere) e NFD
('a' + accento separato): al Mac via SMB appaiono come lo stesso nome e
alcuni file diventano invisibili o si 'fondono' nei cataloghi. Questo
strumento porta tutto in NFC, così i nomi sono univoci ovunque.

Quando la forma NFC di un nome esiste già (due file quasi omonimi, uno NFC
e uno NFD), il file NFD viene spostato nella cartella --twin-dir invece di
essere sovrascritto: sono quasi sempre copie spurie, ma non si cancella nulla.

Uso (dal server, dove i percorsi sono esatti):
    python3 normalizza_nfc.py /cartella                  # solo rapporto
    python3 normalizza_nfc.py /cartella --applica        # rinomina davvero
    python3 normalizza_nfc.py /cartella --applica --twin-dir /cartella/twin
"""

import os
import sys
import argparse
import unicodedata


def nfc(nome):
    return unicodedata.normalize("NFC", nome)


def principale():
    parser = argparse.ArgumentParser(description="Normalizza i nomi in NFC")
    parser.add_argument("cartella")
    parser.add_argument("--applica", action="store_true",
                        help="Esegue le rinominate (default: solo rapporto)")
    parser.add_argument("--twin-dir", dest="twin_dir", default=None,
                        help="Dove spostare i file NFD che collidono con un omonimo NFC")
    args = parser.parse_args()

    radice = os.path.abspath(args.cartella)
    if not os.path.isdir(radice):
        print(f"❌ '{radice}' non è una cartella.")
        sys.exit(1)

    da_rinominare_file = []   # (percorso, nuovo_percorso)
    da_rinominare_dir = []
    twins = []                # (percorso, percorso_nfc_esistente)
    errori = []

    for root, dirs, files in os.walk(radice):
        for nome in files:
            if nome != nfc(nome):
                p = os.path.join(root, nome)
                nuovo = os.path.join(root, nfc(nome))
                if os.path.exists(nuovo):
                    twins.append((p, nuovo))
                else:
                    da_rinominare_file.append((p, nuovo))
        for nome in dirs:
            if nome != nfc(nome):
                p = os.path.join(root, nome)
                nuovo = os.path.join(root, nfc(nome))
                if os.path.exists(nuovo):
                    # cartelle omonime: la gestiamo solo a livello di rapporto
                    twins.append((p, nuovo))
                else:
                    da_rinominare_dir.append((p, nuovo))

    print(f"Cartella: {radice}")
    print(f"  file da rinominare in NFC : {len(da_rinominare_file)}")
    print(f"  cartelle da rinominare     : {len(da_rinominare_dir)}")
    print(f"  'gemelli' NFC/NFD (collisioni): {len(twins)}")
    if twins:
        print("\n--- Gemelli (il primo è quello che verrebbe messo da parte) ---")
        for p, esistente in twins[:30]:
            print(f"  {os.path.relpath(p, radice)}")
            print(f"    ↳ già esiste: {os.path.relpath(esistente, radice)}")
        if len(twins) > 30:
            print(f"  ... e altri {len(twins) - 30}")

    if not args.applica:
        print("\n⚠️  Modalità rapporto — nessuna modifica.")
        print("   Per applicare: aggiungi --applica")
        return

    # 1. Sposta i gemelli NFD nella twin-dir (niente sovrascritture)
    if twins and args.twin_dir:
        import shutil
        spostati = 0
        for p, esistente in twins:
            if os.path.isdir(p):
                continue  # le cartelle omonime restano, gestite a occhio
            try:
                rel = os.path.relpath(p, radice)
                dest = os.path.join(os.path.abspath(args.twin_dir), rel)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.move(p, dest)
                spostati += 1
            except (OSError, shutil.Error) as e:
                errori.append(f"{p}: {e}")
        print(f"\n📦 {spostati} file gemelli spostati in {args.twin_dir}")
    elif twins:
        print("\n⚠️  Gemelli presenti ma nessuna --twin-dir: NON tocco i gemelli.")

    # 2. Rinomina i file
    fatte = 0
    for p, nuovo in da_rinominare_file:
        try:
            os.rename(p, nuovo)
            fatte += 1
        except OSError as e:
            errori.append(f"{p}: {e}")
    print(f"✏️  {fatte} file rinominati in NFC")

    # 3. Rinomina le cartelle (dalle più profonde)
    fatte = 0
    for p, nuovo in sorted(da_rinominare_dir, key=lambda x: -len(x[0])):
        try:
            os.rename(p, nuovo)
            fatte += 1
        except OSError as e:
            errori.append(f"{p}: {e}")
    print(f"✏️  {fatte} cartelle rinominate in NFC")

    if errori:
        print(f"\n⚠️  {len(errori)} errori:")
        for e in errori[:20]:
            print(f"  {e}")
    else:
        print("\n✅ Fatto, nessun errore.")


if __name__ == "__main__":
    principale()
