"""Small measured CPU fixture. No torch, network, API keys, or fabricated metrics."""
import argparse
import json
import math
from pathlib import Path
import random

STEPS = 4


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--data', default='/data')
    p.add_argument('--out', default='/output')
    p.add_argument('--config', default='/config/config.json')
    a = p.parse_args()
    config = json.loads(Path(a.config).read_text())
    rows = json.loads((Path(a.data) / 'train.json').read_text())
    validation = json.loads((Path(a.data) / 'validation.json').read_text())
    rng = random.Random(config['seed'])
    w = [rng.uniform(-1.5, 1.5), rng.uniform(-1.5, 1.5)]
    for _ in range(STEPS):
        grad = [0.0, 0.0]
        for row in rows:
            z = sum(x*y for x,y in zip(w, row['x']))
            prob = 1 / (1 + math.exp(-max(-30, min(30, z))))
            for i in range(2):
                grad[i] += (prob-row['label'])*row['x'][i]
        w = [x - 0.25*g/len(rows) for x,g in zip(w,grad)]
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    predictions = [{'id': r['id'], 'prediction': int(sum(x*y for x,y in zip(w,r['x'])) >= 0)} for r in validation]
    (out / 'predictions.json').write_text(json.dumps(predictions))
    (out / 'training.json').write_text(json.dumps({'steps': STEPS, 'weights': w}))


if __name__ == '__main__':
    main()
