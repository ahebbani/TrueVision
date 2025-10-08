import os, wave, json, sys
import numpy as np
# ensure project root is importable when running from scripts/
ROOT = os.path.dirname(os.path.dirname(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from speaker_transcriber import SpeakerEncoder, SpeakerDB, SAMPLE_RATE

print('Loading speaker DB')
db = SpeakerDB('speakers.json')
names, mat = db.get_matrix()
print('Speakers:', names)
enc = SpeakerEncoder(device='cpu')

wav_files = [f for f in os.listdir('.') if f.lower().endswith('.wav') and (f.startswith('calibrate_') or f.startswith('enroll_') or f.startswith('test_'))]
if not wav_files:
    print('No calibration/enroll wavs found in folder.')
else:
    for w in sorted(wav_files):
        try:
            with wave.open(w,'rb') as wf:
                pcm = wf.readframes(wf.getnframes())
        except Exception as e:
            print('Failed reading',w,e)
            continue
        emb = enc.embed_wav_pcm(pcm, SAMPLE_RATE)
        if mat.shape[0]==0:
            print(w, ': no enrolled speakers')
            continue
        sims = mat.dot(emb/(np.linalg.norm(emb)+1e-9))
        best_idx = int(np.argmax(sims))
        best_name = names[best_idx]
        best_score = float(sims[best_idx])
        print(f"{w}: best={best_name} score={best_score:.4f} ({best_score*100:.1f}%)")
        for n,s in zip(names,sims.tolist()):
            print(f"  -> {n}: {s:.4f} ({s*100:.1f}%)")
print('Done')
