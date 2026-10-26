# Toy optimization

Mode: demo | State: COMPLETED

Measure the effect of more optimization steps

## Protocol
{
  "schema_version": 1,
  "experiment_id": "aa076c0d-5617-481d-afb1-e422de468924",
  "revision": 1,
  "hypothesis": {
    "schema_version": 1,
    "claim": "Longer training improves validation accuracy of the toy classifier",
    "rationale": "Compare 4 and 40 gradient steps on fixed synthetic data.",
    "refutation": "Paired accuracy gain is below the predeclared delta.",
    "source_refs": []
  },
  "primary_metric": "accuracy",
  "direction": "maximize",
  "seeds": [
    11,
    22,
    33
  ],
  "delta": 0.01,
  "fixed_conditions": [
    "same data split",
    "same seeds",
    "same learning rate",
    "one CPU"
  ],
  "treatment": "Change training iterations from 4 to 40; explicitly report increased compute."
}

## Measured runs
| Run | Variant | Seed | Status | Accuracy | Commit |
|---|---|---|---|---|---|
| 839eb5a1-c70f-4489-97ab-948a61fc95c8 | baseline / smoke | 11 | SUCCEEDED | 0.8 | 62e69d2733dda876aec7bac5e0875ed7870ae5d1 |
| 542b8653-894d-4382-8013-232a729bba72 | treatment / smoke | 11 | SUCCEEDED | 0.975 | 1a5d5ba6abc90604d20164d0540189472a1605bd |
| e1021a15-fbb9-4bb4-87d5-0220e77e4cbf | baseline / full | 11 | SUCCEEDED | 0.8 | 62e69d2733dda876aec7bac5e0875ed7870ae5d1 |
| addfecc2-cea8-4bb7-9f28-7493b6c6fc8c | baseline / full | 22 | SUCCEEDED | 0.925 | 62e69d2733dda876aec7bac5e0875ed7870ae5d1 |
| 887a5541-6dd2-490e-ac7a-662c1797fad5 | baseline / full | 33 | SUCCEEDED | 0.75 | 62e69d2733dda876aec7bac5e0875ed7870ae5d1 |
| 53e8739f-141b-43a9-a500-136f936b56e7 | treatment / full | 11 | SUCCEEDED | 0.975 | 1a5d5ba6abc90604d20164d0540189472a1605bd |
| 90d55e73-e024-4727-ade6-37c9a1faf515 | treatment / full | 22 | SUCCEEDED | 1.0 | 1a5d5ba6abc90604d20164d0540189472a1605bd |
| 9240e957-5184-4264-bded-796c830fb0ca | treatment / full | 33 | SUCCEEDED | 0.975 | 1a5d5ba6abc90604d20164d0540189472a1605bd |

## summary
```json
{
  "paired": {
    "11": {
      "baseline": 0.8,
      "treatment": 0.975
    },
    "22": {
      "baseline": 0.925,
      "treatment": 1.0
    },
    "33": {
      "baseline": 0.75,
      "treatment": 0.975
    }
  },
  "mean_delta": 0.1583333333333333,
  "stdev_delta": 0.07637626158259735,
  "verdict": "supported",
  "rule": "all paired improvements positive and >= practical delta; no significance claim"
}
```

## conclusion
```json
{
  "schema_version": 1,
  "verdict": "supported",
  "claim": "Toy validation comparison only; no scientific generalization or final test claim.",
  "experiment_refs": [
    "aa076c0d-5617-481d-afb1-e422de468924"
  ],
  "limitations": [
    "Three seeds only",
    "Synthetic data",
    "Training compute differs",
    "No final held-out test evaluation"
  ],
  "next_step": "Review the paired measurements before selecting a real benchmark."
}
```

## reviews
```json
[
  {
    "role": "k1_plan",
    "schema_version": 1,
    "verdict": "accept",
    "target_digest": "2880bc2366756638fe17abfe6a9d8cb320dd02de8f8d46d8b288402b0a2bfbd6",
    "checks": [
      "Demo fixture review; not an independent LLM review"
    ],
    "evidence_refs": [
      "aa076c0d-5617-481d-afb1-e422de468924"
    ],
    "issues": [],
    "requested_actions": []
  },
  {
    "role": "k2_code",
    "schema_version": 1,
    "verdict": "accept",
    "target_digest": "c4c02afc153e58b1a305dc9e384c2a3d133cde72b42c0fce3f937e4dd2030aae",
    "checks": [
      "Demo fixture review; not an independent LLM review"
    ],
    "evidence_refs": [
      "1a5d5ba6abc90604d20164d0540189472a1605bd"
    ],
    "issues": [],
    "requested_actions": []
  },
  {
    "role": "k2_result",
    "schema_version": 1,
    "verdict": "accept",
    "target_digest": "b65a614bc55fef8fd0d44402ae407c4e2043d57647828bcc5c881f81e91d6738",
    "checks": [
      "Demo fixture review; not an independent LLM review"
    ],
    "evidence_refs": [
      "e1021a15-fbb9-4bb4-87d5-0220e77e4cbf",
      "addfecc2-cea8-4bb7-9f28-7493b6c6fc8c",
      "887a5541-6dd2-490e-ac7a-662c1797fad5",
      "53e8739f-141b-43a9-a500-136f936b56e7",
      "90d55e73-e024-4727-ade6-37c9a1faf515",
      "9240e957-5184-4264-bded-796c830fb0ca"
    ],
    "issues": [],
    "requested_actions": []
  },
  {
    "role": "k1_conclusion",
    "schema_version": 1,
    "verdict": "accept",
    "target_digest": "1cd37780da39ad07d49190317d53c0ee20888a807a0dc0dbc759164a584f945b",
    "checks": [
      "Demo fixture review; not an independent LLM review"
    ],
    "evidence_refs": [
      "aa076c0d-5617-481d-afb1-e422de468924",
      "839eb5a1-c70f-4489-97ab-948a61fc95c8",
      "542b8653-894d-4382-8013-232a729bba72",
      "e1021a15-fbb9-4bb4-87d5-0220e77e4cbf",
      "addfecc2-cea8-4bb7-9f28-7493b6c6fc8c",
      "887a5541-6dd2-490e-ac7a-662c1797fad5",
      "53e8739f-141b-43a9-a500-136f936b56e7",
      "90d55e73-e024-4727-ade6-37c9a1faf515",
      "9240e957-5184-4264-bded-796c830fb0ca"
    ],
    "issues": [],
    "requested_actions": []
  }
]
```

## Resources
GPU seconds reserved: 0
LLM USD reserved: 0.0
Reservations are conservative ceilings, not measured billing.

## Reproduction
assr export 6181e93f-6bf6-4eed-852e-8341293a341f --output campaign.json
Use RunSpec with the same administrator profile and source/image/data/evaluator digests.
Demo measurements are CPU fixtures, not GPU acceptance or scientific validation.