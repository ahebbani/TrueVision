import os
import json
import glob

ROOT = os.path.dirname(os.path.dirname(__file__)) if os.path.basename(__file__)=="remove_bob.py" else os.getcwd()
SPEAKERS = os.path.join(ROOT, 'speakers.json')
print('Working dir:', ROOT)
if os.path.exists(SPEAKERS):
    with open(SPEAKERS, 'r', encoding='utf-8') as f:
        try:
            data = json.load(f)
        except Exception as e:
            print('Failed to load JSON:', e)
            data = {}
    if 'bob' in data:
        print("Removing 'bob' from speakers.json")
        data.pop('bob', None)
        with open(SPEAKERS, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
    else:
        print("No 'bob' key found in speakers.json")
else:
    print('speakers.json not found')

# delete bob wavs
patterns = ['calibrate_bob_*.wav', 'enroll_bob.wav', 'calibrate_bob_*.wav']
removed = []
for p in patterns:
    for path in glob.glob(os.path.join(ROOT, p)):
        try:
            os.remove(path)
            removed.append(path)
        except Exception as e:
            print('Failed to remove', path, e)

if removed:
    print('Removed files:')
    for r in removed:
        print(' -', r)
else:
    print('No bob WAV files found to remove')

print('Done')
