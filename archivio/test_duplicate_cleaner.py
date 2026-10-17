#!/usr/bin/env python3
"""
Test per duplicate_cleaner.py

Crea cartelle temporanee con file di test e verifica che ogni filtro
identifichi correttamente i duplicati.

Uso:
    python3 test_duplicate_cleaner.py
    python3 -m pytest test_duplicate_cleaner.py -v
"""

import os
import sys
import tempfile
import shutil
import unittest

# Importa le funzioni dallo script
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from duplicate_cleaner import (
    estrai_titolo,
    nome_base,
    nome_base_senza_ext,
    ha_prefisso_numerico,
    trova_da_eliminare,
    calcola_hash,
)


class TestEstrazioneTitolo(unittest.TestCase):
    """Test della funzione estrai_titolo()."""

    def test_prefisso_numerico_classico(self):
        self.assertEqual(estrai_titolo("03 Song.mp3"), "song")
        self.assertEqual(estrai_titolo("03-Song.mp3"), "song")
        self.assertEqual(estrai_titolo("12. Song.mp3"), "song")

    def test_artista_album_numero_titolo(self):
        """Caso specifico dell'utente: 'A - A 01 Baciami.aac'"""
        self.assertEqual(estrai_titolo("A - A 01 Baciami.aac"), "baciami")
        self.assertEqual(estrai_titolo("A - A 0 Baciami.mp3"), "baciami")

    def test_artista_numero_titolo(self):
        self.assertEqual(estrai_titolo("Artist - 01 Song.mp3"), "song")
        self.assertEqual(estrai_titolo("Artist - Album 01 Song.mp3"), "song")

    def test_senza_numero(self):
        self.assertEqual(estrai_titolo("Song.mp3"), "song")
        self.assertEqual(estrai_titolo("Baciami.aac"), "baciami")

    def test_solo_numero(self):
        """File con solo numeri come nome — non deve restituire stringa vuota."""
        result = estrai_titolo("123.mp3")
        self.assertTrue(len(result) > 0)

    def test_case_insensitive(self):
        self.assertEqual(estrai_titolo("BACIAMI.mp3"), "baciami")
        self.assertEqual(estrai_titolo("A - A 01 BACIAMI.aac"), "baciami")


class TestHaPrefissoNumerico(unittest.TestCase):
    """Test di ha_prefisso_numerico()."""

    def test_prefisso_classico(self):
        self.assertTrue(ha_prefisso_numerico("03 Song.mp3"))
        self.assertTrue(ha_prefisso_numerico("03-Song.mp3"))
        self.assertTrue(ha_prefisso_numerico("12. Song.mp3"))

    def test_numero_nel_mezzo(self):
        self.assertTrue(ha_prefisso_numerico("A - A 01 Baciami.aac"))
        self.assertTrue(ha_prefisso_numerico("A - A 0 Baciami.mp3"))
        self.assertTrue(ha_prefisso_numerico("Artist - 01 Song.mp3"))

    def test_senza_numero(self):
        self.assertFalse(ha_prefisso_numerico("Song.mp3"))
        self.assertFalse(ha_prefisso_numerico("Baciami.aac"))
        self.assertFalse(ha_prefisso_numerico("My Song 2.mp3"))  # "2" non seguito da lettera


class TestNomeBaseSenzaExt(unittest.TestCase):
    """Test che file diversi con stesso brano producano lo stesso titolo."""

    def test_stesso_titolo_formati_diversi(self):
        self.assertEqual(
            nome_base_senza_ext("A - A 01 Baciami.aac"),
            nome_base_senza_ext("A - A 0 Baciami.mp3"),
        )

    def test_stesso_titolo_con_e_senza_numero(self):
        self.assertEqual(
            nome_base_senza_ext("03 Song.mp3"),
            nome_base_senza_ext("Song.mp3"),
        )

    def test_stesso_titolo_artista_e_semplice(self):
        self.assertEqual(
            nome_base_senza_ext("Artist - 01 Song.mp3"),
            nome_base_senza_ext("Song.aac"),
        )


# -----------------------------------------------------------------------
# Test di integrazione con file reali su disco
# -----------------------------------------------------------------------
def _crea_file(percorso, contenuto=b"test content"):
    """Helper: crea un file con contenuto specificato."""
    os.makedirs(os.path.dirname(percorso), exist_ok=True)
    with open(percorso, "wb") as f:
        f.write(contenuto)


class TestFiltro1PercorsoIdentico(unittest.TestCase):
    """Filtro 1: file con stesso percorso relativo in master e leggera."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.master = os.path.join(self.tmpdir, "master")
        self.leggera = os.path.join(self.tmpdir, "leggera")

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_percorso_identico(self):
        _crea_file(os.path.join(self.master, "sub", "file.txt"), b"hello")
        _crea_file(os.path.join(self.leggera, "sub", "file.txt"), b"hello")

        result = trova_da_eliminare(self.master, self.leggera)

        leggera_file = os.path.join(self.leggera, "sub", "file.txt")
        self.assertIn(leggera_file, result)
        self.assertIn("percorso identico", result[leggera_file])

    def test_percorso_diverso_non_trovato(self):
        _crea_file(os.path.join(self.master, "a.txt"), b"hello")
        _crea_file(os.path.join(self.leggera, "b.txt"), b"hello")

        result = trova_da_eliminare(self.master, self.leggera)

        # Non trovato dal filtro 1, ma trovato dal filtro 4 (hash identico)
        leggera_file = os.path.join(self.leggera, "b.txt")
        self.assertIn(leggera_file, result)
        self.assertIn("hash identico", result[leggera_file])


class TestFiltro2NumeroTraccia(unittest.TestCase):
    """Filtro 2: file con numero traccia vs versione pulita."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.master = os.path.join(self.tmpdir, "master")
        self.leggera = os.path.join(self.tmpdir, "leggera")

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_prefisso_classico_master_vs_leggera(self):
        """'Baciami.aac' in master, '03 Baciami.aac' in leggera → elimina dalla leggera."""
        _crea_file(os.path.join(self.master, "Baciami.aac"), b"master_content")
        _crea_file(os.path.join(self.leggera, "03 Baciami.aac"), b"leggera_content")

        result = trova_da_eliminare(self.master, self.leggera)

        leggera_file = os.path.join(self.leggera, "03 Baciami.aac")
        self.assertIn(leggera_file, result)
        self.assertIn("numero traccia", result[leggera_file])

    def test_artista_album_numero_vs_pulito(self):
        """Caso utente: 'Baciami.aac' in master, 'A - A 01 Baciami.aac' in leggera."""
        _crea_file(os.path.join(self.master, "Baciami.aac"), b"master_v")
        _crea_file(os.path.join(self.leggera, "A - A 01 Baciami.aac"), b"leggera_v")

        result = trova_da_eliminare(self.master, self.leggera)

        leggera_file = os.path.join(self.leggera, "A - A 01 Baciami.aac")
        self.assertIn(leggera_file, result)
        self.assertIn("numero traccia", result[leggera_file])

    def test_dentro_leggera_stesso_dir(self):
        """'Song.mp3' e '03 Song.mp3' nella stessa cartella leggera → elimina il numerato."""
        _crea_file(os.path.join(self.master, "dummy.txt"), b"x")  # master non vuota
        _crea_file(os.path.join(self.leggera, "Song.mp3"), b"clean_version")
        _crea_file(os.path.join(self.leggera, "03 Song.mp3"), b"numbered_version")

        result = trova_da_eliminare(self.master, self.leggera)

        numerato = os.path.join(self.leggera, "03 Song.mp3")
        pulito = os.path.join(self.leggera, "Song.mp3")
        self.assertIn(numerato, result)
        self.assertNotIn(pulito, result)

    def test_master_non_toccata(self):
        """La master non viene mai segnata per eliminazione."""
        _crea_file(os.path.join(self.master, "03 Song.mp3"), b"master_num")
        _crea_file(os.path.join(self.leggera, "Song.mp3"), b"leggera_clean")

        result = trova_da_eliminare(self.master, self.leggera)

        master_file = os.path.join(self.master, "03 Song.mp3")
        self.assertNotIn(master_file, result)


class TestFiltro3QualitaAudio(unittest.TestCase):
    """Filtro 3: stesso brano, formati diversi → tiene qualità migliore."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.master = os.path.join(self.tmpdir, "master")
        self.leggera = os.path.join(self.tmpdir, "leggera")

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_aac_batte_mp3_caso_utente(self):
        """Caso utente: 'A - A 01 Baciami.aac' e 'A - A 0 Baciami.mp3'
        nella leggera → tiene .aac (punteggio 50) elimina .mp3 (punteggio 40)."""
        _crea_file(os.path.join(self.master, "dummy.txt"), b"x")
        _crea_file(os.path.join(self.leggera, "A - A 01 Baciami.aac"), b"aac_content_xx")
        _crea_file(os.path.join(self.leggera, "A - A 0 Baciami.mp3"), b"mp3_content_yy")

        result = trova_da_eliminare(self.master, self.leggera)

        mp3_file = os.path.join(self.leggera, "A - A 0 Baciami.mp3")
        aac_file = os.path.join(self.leggera, "A - A 01 Baciami.aac")
        self.assertIn(mp3_file, result)
        self.assertNotIn(aac_file, result)

    def test_aiff_batte_mp3_master_vs_leggera(self):
        """'Song.aiff' nella master, 'Song.mp3' nella leggera → elimina mp3."""
        _crea_file(os.path.join(self.master, "Song.aiff"), b"lossless_data")
        _crea_file(os.path.join(self.leggera, "Song.mp3"), b"lossy_data_xx")

        result = trova_da_eliminare(self.master, self.leggera)

        mp3_file = os.path.join(self.leggera, "Song.mp3")
        self.assertIn(mp3_file, result)
        self.assertIn("qualità inferiore", result[mp3_file])

    def test_m4a_batte_mp3_dentro_leggera(self):
        """'Song.m4a' e 'Song.mp3' nella leggera → tiene .m4a, elimina .mp3."""
        _crea_file(os.path.join(self.master, "dummy.txt"), b"x")
        _crea_file(os.path.join(self.leggera, "Song.m4a"), b"apple_aac_data")
        _crea_file(os.path.join(self.leggera, "Song.mp3"), b"mp3_data_xxxxx")

        result = trova_da_eliminare(self.master, self.leggera)

        mp3_file = os.path.join(self.leggera, "Song.mp3")
        m4a_file = os.path.join(self.leggera, "Song.m4a")
        self.assertIn(mp3_file, result)
        self.assertNotIn(m4a_file, result)


class TestFiltro4HashEsatto(unittest.TestCase):
    """Filtro 4: file identici per contenuto (hash SHA-256)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.master = os.path.join(self.tmpdir, "master")
        self.leggera = os.path.join(self.tmpdir, "leggera")

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_contenuto_identico_nomi_diversi(self):
        """Stesso contenuto, nomi diversi → duplicato esatto."""
        contenuto = b"identical binary content here 1234567890"
        _crea_file(os.path.join(self.master, "originale.dat"), contenuto)
        _crea_file(os.path.join(self.leggera, "copia.dat"), contenuto)

        result = trova_da_eliminare(self.master, self.leggera)

        copia = os.path.join(self.leggera, "copia.dat")
        self.assertIn(copia, result)
        self.assertIn("hash identico", result[copia])

    def test_contenuto_diverso_non_trovato(self):
        """Contenuto diverso, stessa dimensione non basta → non duplicato."""
        _crea_file(os.path.join(self.master, "a.dat"), b"contenuto AAA 123")
        _crea_file(os.path.join(self.leggera, "b.dat"), b"contenuto BBB 456")

        result = trova_da_eliminare(self.master, self.leggera)

        b_file = os.path.join(self.leggera, "b.dat")
        self.assertNotIn(b_file, result)


class TestScenarioCompleto(unittest.TestCase):
    """Test end-to-end che simula il caso dell'utente con più filtri attivi."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.master = os.path.join(self.tmpdir, "master")
        self.leggera = os.path.join(self.tmpdir, "leggera")

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_scenario_utente(self):
        """
        Master:
          Baciami.aac  (versione pulita)

        Leggera:
          A - A 01 Baciami.aac  (numerato, stessa ext → filtro 2)
          A - A 0 Baciami.mp3   (numerato, mp3 < aac → filtro 3)
          Baciami.aac            (percorso diverso ma stesso contenuto → filtro 4)
          Altro.flac             (non duplicato → rimane)
        """
        master_content = b"master baciami aac content!!"
        _crea_file(os.path.join(self.master, "Baciami.aac"), master_content)

        _crea_file(os.path.join(self.leggera, "A - A 01 Baciami.aac"), b"leggera_aac_01_data")
        _crea_file(os.path.join(self.leggera, "A - A 0 Baciami.mp3"), b"leggera_mp3_00_data")
        _crea_file(os.path.join(self.leggera, "Baciami.aac"), master_content)  # hash identico
        _crea_file(os.path.join(self.leggera, "Altro.flac"), b"completely different")

        result = trova_da_eliminare(self.master, self.leggera)

        # Deve eliminare: numerato aac, mp3 bassa qualità, hash identico
        self.assertIn(os.path.join(self.leggera, "A - A 01 Baciami.aac"), result)
        self.assertIn(os.path.join(self.leggera, "A - A 0 Baciami.mp3"), result)
        self.assertIn(os.path.join(self.leggera, "Baciami.aac"), result)

        # NON deve eliminare: file diverso
        self.assertNotIn(os.path.join(self.leggera, "Altro.flac"), result)

        # NON deve toccare la master
        self.assertNotIn(os.path.join(self.master, "Baciami.aac"), result)

    def test_nessun_falso_positivo_titoli_diversi(self):
        """Due brani diversi non devono essere confusi anche se hanno numero."""
        _crea_file(os.path.join(self.master, "Love.mp3"), b"love_song_data")
        _crea_file(os.path.join(self.leggera, "A - A 01 Hate.mp3"), b"hate_song_data")

        result = trova_da_eliminare(self.master, self.leggera)

        # "Hate" ≠ "Love" → non deve essere eliminato (dal filtro 2)
        hate_file = os.path.join(self.leggera, "A - A 01 Hate.mp3")
        if hate_file in result:
            # Potrebbe essere trovato solo se hash identico (improbabile con contenuti diversi)
            self.assertNotIn("numero traccia", result[hate_file])


if __name__ == "__main__":
    unittest.main(verbosity=2)
