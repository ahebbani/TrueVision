import os, json, shutil, sys
ROOT = os.path.dirname(os.path.dirname(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from speaker_transcriber import SpeakerDB, SpeakerEncoder, SAMPLE_RATE
import numpy as np

DB_PATH = os.path.join(ROOT, 'speakers.json')
BACKUP = DB_PATH + '.bak'

CUT_OFF = 0.50

print('Loading DB:', DB_PATH)
with open(DB_PATH,'r',encoding='utf-8') as f:
    db = json.load(f)

# backup
shutil.copy2(DB_PATH, BACKUP)
print('Backup saved to', BACKUP)

enc = SpeakerEncoder(device='cpu')
new_db = {}
for name, vecs in db.items():
    key = name
    kept = []
    if isinstance(vecs, list) and vecs and isinstance(vecs[0], list):
        # compute centroid of existing vectors
        arrs = [np.array(v, dtype=np.float32) for v in vecs]
        centroid = np.mean(np.stack(arrs, axis=0), axis=0)
        centroid = centroid / (np.linalg.norm(centroid)+1e-9)
        for v in vecs:
            score = float(np.dot(centroid, np.array(v, dtype=np.float32) / (np.linalg.norm(np.array(v, dtype=np.float32))+1e-9)))
            if score >= CUT_OFF:
                kept.append(v)
            else:
                print(f"Pruning {name} embedding with score={score:.3f}")
    else:
        kept = vecs
    if kept:
        new_db[key] = kept

with open(DB_PATH,'w',encoding='utf-8') as f:
    json.dump(new_db,f,indent=2)

print('Prune complete. Updated DB saved. Original backed up.')
