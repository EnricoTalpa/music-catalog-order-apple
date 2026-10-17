#!/usr/bin/env python3
"""Esegue in parallelo le copie di un piano organizza, per un prefisso di origine.

I piani generati da `organizza --piano` contengono righe:
    mkdir -p 'DEST_DIR'
    [ -f 'SRC' ] && mv -n 'SRC' 'DEST'

Quando SRC è su un'altra macchina in rete (montata via SMB), eseguire
tutte le righe da qui è lento con mv. Questo strumento:

1. estrae le operazioni il cui SRC inizia con il prefisso dato
2. le converte in COPIE (cp -p: niente sovrascritture, mtime preservato)
3. le distribuisce su N worker in parallelo
4. aspetta e verifica: quante destinazioni esistono e quante mancano

Le sorgenti NON vengono cancellate: si eliminano dopo la verifica
(con trova_e_cancella_copiate.py o a mano).

Uso:
    python3 lancia_copie.py piano.sh /Volumes/AltroMac [--worker 6]
"""

import os
import sys
import subprocess
import argparse
import tempfile


def principale():
    parser = argparse.ArgumentParser(description="Copie parallele da un piano")
    parser.add_argument("piano", help="File piano generato da organizza --piano")
    parser.add_argument("prefisso", help="Prefisso dei percorsi di origine da estrarre")
    parser.add_argument("--worker", type=int, default=6)
    args = parser.parse_args()

    # Estrai le coppie (mkdir, riga mv) del prefisso
    mkdir_pendente = None
    operazioni = []  # (mkdir_line, mv_line)
    with open(args.piano, encoding="utf-8") as f:
        for linea in f:
            linea = linea.rstrip("\n")
            if linea.startswith("mkdir -p "):
                mkdir_pendente = linea
            elif linea.startswith("[ -f "):
                if args.prefisso in linea:
                    if mkdir_pendente:
                        operazioni.append((mkdir_pendente, linea))
                mkdir_pendente = None

    if not operazioni:
        print("❌ Nessuna operazione trovata per il prefisso dato.")
        sys.exit(1)

    print(f"Operazioni trovate: {len(operazioni)} (worker: {args.worker})")

    # mkdir una volta sola (idempotente, veloce)
    mkdirs = sorted({m for m, _ in operazioni})
    for m in mkdirs:
        subprocess.run(["bash", "-c", m], check=False)
    print(f"Directory di destinazione create: {len(mkdirs)}")

    # Converti mv → cp -p e dividi in worker
    n = min(args.worker, len(operazioni))
    righe = [mv.replace("&& mv -n ", "&& cp -p ", 1) for _, mv in operazioni]
    cartella = tempfile.mkdtemp(prefix="copie_")
    worker_files = []
    for i in range(n):
        chunk = righe[i::n]
        nome = os.path.join(cartella, f"worker_{i:02d}")
        with open(nome, "w", encoding="utf-8") as f:
            f.write("\n".join(chunk) + "\n")
        worker_files.append((nome, len(chunk)))
        print(f"  worker_{i:02d}: {len(chunk)} copie")

    # Lancia in parallelo e aspetta
    processi = [(nome, subprocess.Popen(["bash", nome],
                                        stdout=open(nome + ".log", "w"),
                                        stderr=subprocess.STDOUT))
                for nome, _ in worker_files]
    errori_worker = 0
    for nome, p in processi:
        rc = p.wait()
        righe_log = 0
        try:
            with open(nome + ".log", encoding="utf-8") as f:
                righe_log = sum(1 for _ in f)
        except OSError:
            pass
        if rc != 0 or righe_log:
            errori_worker += 1
            print(f"⚠️  worker {os.path.basename(nome)}: exit={rc}, righe di log={righe_log}")

    # Verifica: quante destinazioni esistono
    ok = mancano = 0
    esempi_mancanti = []
    for _, mv in operazioni:
        # la destinazione è l'ultimo percorso tra apici della riga
        pezzi = mv.split("'")
        dest = pezzi[-2] if len(pezzi) >= 2 else ""
        if dest and os.path.exists(dest):
            ok += 1
        else:
            mancano += 1
            if len(esempi_mancanti) < 5:
                esempi_mancanti.append(dest)

    print()
    print(f"✅ destinazioni presenti: {ok}")
    if mancano:
        print(f"❌ destinazioni MANCANTI: {mancano}")
        for e in esempi_mancanti:
            print(f"   {e}")
    print("\nLe sorgenti NON sono state cancellate: verificare e poi pulire.")


if __name__ == "__main__":
    principale()
