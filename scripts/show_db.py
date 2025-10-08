import json
with open('speakers.json','r',encoding='utf-8') as f:
    d=json.load(f)
print('Speakers in DB:', list(d.keys()))
for k,v in d.items():
    if isinstance(v,list) and v and isinstance(v[0],list):
        print(f" - {k}: {len(v)} embeddings")
    else:
        print(f" - {k}: legacy single vector")
