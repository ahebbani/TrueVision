import sqlite3
import unittest

import numpy as np

from data_access.db import ensure_all_schemas, insert_face_with_template
from facial_recognition.recognizer import Recognizer, RecognizerConfig


def _make_recognizer() -> Recognizer:
    recognizer = Recognizer.__new__(Recognizer)
    recognizer.cfg = RecognizerConfig(models_dir='.')
    recognizer._last_added_ts = {}
    return recognizer


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(':memory:')
    ensure_all_schemas(conn)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO faces (name, embedding, created_at, seen_count) VALUES (?, ?, datetime('now'), 0)",
        ('test', np.zeros(128, dtype=np.float64).tobytes()),
    )
    conn.commit()
    return conn


def _embedding(val: float) -> np.ndarray:
    emb = np.zeros(128, dtype=np.float64)
    emb[0] = val
    return emb


class TemplateBootstrapTests(unittest.TestCase):
    def test_bootstrap_force_accepts_second_template_even_if_similar(self):
        conn = _make_conn()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO face_embeddings (face_id, embedding, created_at, quality) VALUES (?, ?, datetime('now'), ?)",
            (1, _embedding(0.0).tobytes(), 150.0),
        )
        conn.commit()

        recognizer = _make_recognizer()
        added = recognizer.maybe_add_embedding(conn, 1, _embedding(0.01), 150.0)

        self.assertTrue(added)
        cur.execute("SELECT COUNT(*) FROM face_embeddings WHERE face_id = 1")
        self.assertEqual(cur.fetchone()[0], 2)

    def test_bootstrap_accepts_less_diverse_template_after_force_phase(self):
        conn = _make_conn()
        cur = conn.cursor()
        for idx in range(3):
            cur.execute(
                "INSERT INTO face_embeddings (face_id, embedding, created_at, quality) VALUES (?, ?, datetime('now'), ?)",
                (1, _embedding(float(idx) * 0.10).tobytes(), 150.0),
            )
        conn.commit()

        recognizer = _make_recognizer()
        added = recognizer.maybe_add_embedding(conn, 1, _embedding(0.32), 150.0)

        self.assertTrue(added)
        cur.execute("SELECT COUNT(*) FROM face_embeddings WHERE face_id = 1")
        self.assertEqual(cur.fetchone()[0], 4)

    def test_steady_state_keeps_stricter_diversity_threshold(self):
        conn = _make_conn()
        cur = conn.cursor()
        for idx in range(5):
            cur.execute(
                "INSERT INTO face_embeddings (face_id, embedding, created_at, quality) VALUES (?, ?, datetime('now'), ?)",
                (1, _embedding(float(idx) * 0.25).tobytes(), 150.0),
            )
        conn.commit()

        recognizer = _make_recognizer()
        added = recognizer.maybe_add_embedding(conn, 1, _embedding(0.12), 150.0)

        self.assertFalse(added)
        cur.execute("SELECT COUNT(*) FROM face_embeddings WHERE face_id = 1")
        self.assertEqual(cur.fetchone()[0], 5)

    def test_insert_face_with_template_seeds_face_embeddings_immediately(self):
        conn = sqlite3.connect(':memory:')
        ensure_all_schemas(conn)

        face_id = insert_face_with_template(
            conn,
            'seeded',
            _embedding(0.5).tobytes(),
            quality=123.0,
        )

        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM faces WHERE id = ?", (face_id,))
        self.assertEqual(cur.fetchone()[0], 1)
        cur.execute("SELECT COUNT(*), MIN(quality), MAX(quality) FROM face_embeddings WHERE face_id = ?", (face_id,))
        count, min_quality, max_quality = cur.fetchone()
        self.assertEqual(count, 1)
        self.assertEqual(min_quality, 123.0)
        self.assertEqual(max_quality, 123.0)


if __name__ == '__main__':
    unittest.main()