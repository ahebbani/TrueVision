from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import cv2
import dlib
import numpy as np


@dataclass
class RecognizerConfig:
    models_dir: str
    detector_mode: str = 'auto'  # 'auto' | 'hog' | 'cnn'
    match_threshold: float = 0.6
    quality_min_var: float = 120.0
    diversity_min_dist: float = 0.20
    add_cooldown_sec: float = 5.0
    bootstrap_template_count: int = 5
    bootstrap_force_until_count: int = 3
    bootstrap_quality_min_var: float = 60.0
    bootstrap_diversity_min_dist: float = 0.08
    bootstrap_add_cooldown_sec: float = 0.75
    verbose: bool = False


@dataclass
class FaceInfo:
    rect: Tuple[int, int, int, int]
    embedding: np.ndarray
    person_id: Optional[int]
    name: str
    seen_count: Optional[int]
    last_seen_at: Optional[str]
    distance: Optional[float]
    quality: Optional[float]


class Recognizer:
    def __init__(self, cfg: RecognizerConfig):
        self.cfg = cfg
        self.detector = dlib.get_frontal_face_detector()
        self.predictor_path = os.path.join(cfg.models_dir, 'shape_predictor_68_face_landmarks.dat')
        self.face_model_path = os.path.join(cfg.models_dir, 'dlib_face_recognition_resnet_model_v1.dat')
        self.cnn_path = os.path.join(cfg.models_dir, 'mmod_human_face_detector.dat')

        if not (os.path.exists(self.predictor_path) and os.path.exists(self.face_model_path)):
            raise RuntimeError("Missing dlib model files in models_dir")

        self.predictor = dlib.shape_predictor(self.predictor_path)
        self.face_rec_model = dlib.face_recognition_model_v1(self.face_model_path)
        self.cnn_detector = None
        if self.cfg.detector_mode in ('auto', 'cnn') and os.path.exists(self.cnn_path):
            try:
                self.cnn_detector = dlib.cnn_face_detection_model_v1(self.cnn_path)
            except Exception:
                self.cnn_detector = None

        self._last_added_ts: Dict[int, float] = {}

    def _detect(self, gray) -> List[dlib.rectangle]:
        if self.cnn_detector is not None and self.cfg.detector_mode in ('auto', 'cnn'):
            dets = self.cnn_detector(gray, 1)
            return [d.rect for d in dets]
        return list(self.detector(gray))

    def _load_db_cache(self, cursor) -> Tuple[Dict[int, List[np.ndarray]], Dict[int, Tuple[str, Optional[int], Optional[str]]]]:
        cursor.execute(
            """
            SELECT fe.face_id, f.name, fe.embedding, f.seen_count, f.last_seen_at
            FROM face_embeddings fe
            JOIN faces f ON f.id = fe.face_id
            """
        )
        rows = cursor.fetchall()
        embeddings_by_person: Dict[int, List[np.ndarray]] = {}
        meta_by_person: Dict[int, Tuple[str, Optional[int], Optional[str]]] = {}
        for person_id, name, db_embedding, db_seen_count, db_last_seen_at in rows:
            emb = np.frombuffer(db_embedding, dtype=np.float64)
            embeddings_by_person.setdefault(person_id, []).append(emb)
            meta_by_person[person_id] = (name, db_seen_count, db_last_seen_at)
        return embeddings_by_person, meta_by_person

    def detect_and_recognize(self, conn, frame_bgr) -> List[FaceInfo]:
        cursor = conn.cursor()
        gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
        rects = self._detect(gray)
        emb_cache, meta_cache = self._load_db_cache(cursor)

        infos: List[FaceInfo] = []
        frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        for rect in rects:
            landmarks = self.predictor(gray, rect)
            emb_live = np.array(self.face_rec_model.compute_face_descriptor(frame_rgb, landmarks))

            recognized_name = "Unknown"
            recognized_id = None
            recognized_seen_count = None
            recognized_last_seen_str = None
            min_distance = float("inf")

            for person_id, person_embs in emb_cache.items():
                for db_emb in person_embs:
                    distance = np.linalg.norm(emb_live - db_emb)
                    if distance < self.cfg.match_threshold and distance < min_distance:
                        name, db_seen_count, db_last_seen_at = meta_cache.get(person_id, ("Unknown", None, None))
                        recognized_name = name
                        recognized_id = person_id
                        recognized_seen_count = db_seen_count
                        recognized_last_seen_str = db_last_seen_at
                        min_distance = distance

            x, y, w, h = (rect.left(), rect.top(), rect.width(), rect.height())
            x0, y0 = max(0, x), max(0, y)
            x1, y1 = min(frame_bgr.shape[1], x + w), min(frame_bgr.shape[0], y + h)
            face_gray = gray[y0:y1, x0:x1]
            quality = None
            if face_gray.size > 0:
                quality = float(cv2.Laplacian(face_gray, cv2.CV_64F).var())

            infos.append(
                FaceInfo(
                    rect=(x, y, w, h),
                    embedding=emb_live,
                    person_id=recognized_id,
                    name=recognized_name,
                    seen_count=recognized_seen_count,
                    last_seen_at=recognized_last_seen_str,
                    distance=(min_distance if recognized_id is not None else None),
                    quality=quality,
                )
            )
        return infos

    def update_seen(self, conn, person_id: int):
        cur = conn.cursor()
        cur.execute(
            "UPDATE faces SET last_seen_at = datetime('now'), seen_count = seen_count + 1 WHERE id = ?",
            (person_id,),
        )
        conn.commit()

    def maybe_add_embedding(self, conn, person_id: int, emb_live: np.ndarray, quality: Optional[float]):
        cur = conn.cursor()
        cur.execute(
            "SELECT embedding FROM face_embeddings WHERE face_id = ?",
            (person_id,),
        )
        rows = cur.fetchall()
        template_count = len(rows)
        in_bootstrap = template_count < self.cfg.bootstrap_template_count
        force_bootstrap = template_count < self.cfg.bootstrap_force_until_count

        quality_min_var = (
            self.cfg.bootstrap_quality_min_var if in_bootstrap else self.cfg.quality_min_var
        )
        diversity_min_dist = (
            self.cfg.bootstrap_diversity_min_dist if in_bootstrap else self.cfg.diversity_min_dist
        )
        add_cooldown_sec = (
            self.cfg.bootstrap_add_cooldown_sec if in_bootstrap else self.cfg.add_cooldown_sec
        )

        if quality is None or quality < quality_min_var:
            if self.cfg.verbose:
                print(f"[templates] skip: low quality (var={quality:.2f} < {quality_min_var})")
            return False
        now_add = time.time()
        last_ts = self._last_added_ts.get(person_id, 0.0)
        if (now_add - last_ts) < add_cooldown_sec:
            if self.cfg.verbose:
                print(f"[templates] skip: cooldown ({now_add - last_ts:.2f}s < {add_cooldown_sec}s)")
            return False

        is_diverse = True
        if rows and not force_bootstrap:
            dists = [np.linalg.norm(emb_live - np.frombuffer(r[0], dtype=np.float64)) for r in rows]
            if dists:
                mind = min(dists)
                is_diverse = (mind >= diversity_min_dist)
                if self.cfg.verbose:
                    phase = 'bootstrap' if in_bootstrap else 'steady'
                    print(f"[templates] diversity check ({phase}): min_dist={mind:.3f} threshold={diversity_min_dist} -> {'OK' if is_diverse else 'skip'}")
        elif rows and force_bootstrap and self.cfg.verbose:
            print(f"[templates] diversity bypass during bootstrap ({template_count} < {self.cfg.bootstrap_force_until_count})")
        if not is_diverse:
            return False

        try:
            cur.execute(
                "INSERT INTO face_embeddings (face_id, embedding, created_at, quality) VALUES (?, ?, datetime('now'), ?)",
                (person_id, emb_live.astype(np.float64).tobytes(), quality),
            )
            conn.commit()
            self._last_added_ts[person_id] = now_add
            if self.cfg.verbose:
                print(f"[templates] added template for person {person_id} (quality={quality:.1f})")
            return True
        except Exception:
            if self.cfg.verbose:
                print(f"[templates] insert failed for person {person_id}")
            return False
