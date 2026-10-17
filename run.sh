#!/bin/bash
# Script Musica — Catalogo e pulizia duplicati
# Progetto: /Volumes/PyLab/projects/Script Musica duplicati file/
# Venv:     ~/PyLab-venvs/musica-duplicati/  (locale, non su NFS)

PROJECT_DIR="/Volumes/PyLab/projects/Script Musica duplicati file"
VENV_DIR="$HOME/PyLab-venvs/musica-duplicati"
PYTHON="$VENV_DIR/bin/python3"

if [ ! -d "$PROJECT_DIR" ]; then
    echo "❌ /Volumes/PyLab non montato."
    echo "   sudo launchctl start com.enrico.nfs-pylab"
    exit 1
fi

if [ ! -x "$PYTHON" ]; then
    echo "❌ Venv non trovato: $VENV_DIR"
    echo "   Ricrea con: python3.14 -m venv ~/PyLab-venvs/musica-duplicati && pip install -r requirements.txt"
    exit 1
fi

cd "$PROJECT_DIR"

# Uso: ./run.sh scan /cartella | report | duplicati | lookup | fix | organizza /dest | export | reset
if [ "$1" = "catalog" ] || [ "$1" = "scan" ] || [ "$1" = "report" ] || [ "$1" = "duplicati" ] || [ "$1" = "lookup" ] || [ "$1" = "fix" ] || [ "$1" = "organizza" ] || [ "$1" = "export" ] || [ "$1" = "reset" ]; then
    "$PYTHON" music_catalog.py "$@"
else
    echo "Uso:"
    echo "  ./run.sh scan /cartella/musica     — cataloga brani"
    echo "  ./run.sh report                    — brani con tag mancanti"
    echo "  ./run.sh duplicati [--revisione /cartella] [--esegui]"
    echo "                                    — trova doppioni nella libreria"
    echo "  ./run.sh lookup                    — cerca info su MusicBrainz"
    echo "  ./run.sh fix                       — applica correzioni tag"
    echo "  ./run.sh organizza /destinazione   — copia in struttura Apple Music"
    echo "  ./run.sh export [file.csv]        — esporta catalogo"
    echo "  ./run.sh reset [--esegui]          — svuota il catalogo"
fi
