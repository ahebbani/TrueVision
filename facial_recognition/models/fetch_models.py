#!/usr/bin/env python3
"""
Download and extract required dlib model files into this directory.

Models:
- shape_predictor_68_face_landmarks.dat
- dlib_face_recognition_resnet_model_v1.dat
- (optional) mmod_human_face_detector.dat

Usage:
  python -m facial_recognition.models.fetch_models
  # or
  python facial_recognition/models/fetch_models.py
"""
from __future__ import annotations

import bz2
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))

MODELS = {
    "shape_predictor_68_face_landmarks.dat": "http://dlib.net/files/shape_predictor_68_face_landmarks.dat.bz2",
    "dlib_face_recognition_resnet_model_v1.dat": "http://dlib.net/files/dlib_face_recognition_resnet_model_v1.dat.bz2",
}

OPTIONAL = {
    "mmod_human_face_detector.dat": "http://dlib.net/files/mmod_human_face_detector.dat.bz2",
}


def _download(url: str, out_path: str):
    print(f"Downloading {url} -> {out_path}...")
    urllib.request.urlretrieve(url, out_path)


def _decompress_bz2(in_path: str, out_path: str):
    print(f"Extracting {in_path} -> {out_path}...")
    with bz2.open(in_path, 'rb') as f_in, open(out_path, 'wb') as f_out:
        f_out.write(f_in.read())


def ensure_models(include_optional: bool = False):
    os.makedirs(HERE, exist_ok=True)

    to_get = dict(MODELS)
    if include_optional:
        to_get.update(OPTIONAL)

    for dat_name, url in to_get.items():
        dat_path = os.path.join(HERE, dat_name)
        if os.path.exists(dat_path):
            print(f"OK: {dat_name} already present")
            continue
        bz2_path = dat_path + ".bz2"
        try:
            _download(url, bz2_path)
            _decompress_bz2(bz2_path, dat_path)
            try:
                os.remove(bz2_path)
            except Exception:
                pass
            print(f"DONE: {dat_name}")
        except Exception as e:
            print(f"ERROR: Failed to fetch {dat_name}: {e}")


def main():
    include_optional = os.environ.get("INCLUDE_CNN", "0").lower() in ("1", "true", "yes")
    ensure_models(include_optional)


if __name__ == "__main__":
    main()
