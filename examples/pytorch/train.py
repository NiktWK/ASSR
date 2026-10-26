"""Example live binary-classification adapter. Image must supply torch and CUDA.
Inputs: /data/{train,validation}.json; /config/config.json. Output: JSON predictions.
The evaluator alone receives labels. No model weights are deserialized by the evaluator.
"""

import json
from pathlib import Path

import torch
from torch import nn

STEPS = 40


def main():
    config = json.loads(Path("/config/config.json").read_text())
    torch.manual_seed(config["seed"])
    if not torch.cuda.is_available():
        raise RuntimeError("This live profile requires CUDA")
    device = torch.device("cuda")
    train = json.loads(Path("/data/train.json").read_text())
    val = json.loads(Path("/data/validation.json").read_text())
    x = torch.tensor([r["x"] for r in train], device=device, dtype=torch.float32)
    y = torch.tensor([r["label"] for r in train], device=device, dtype=torch.float32).reshape(-1, 1)
    v = torch.tensor([r["x"] for r in val], device=device, dtype=torch.float32)
    model = nn.Linear(x.shape[1], 1).to(device)
    optimizer = torch.optim.SGD(model.parameters(), lr=0.1)
    steps = 2 if config["phase"] == "smoke" else STEPS
    for _ in range(steps):
        optimizer.zero_grad()
        logits = model(x)
        if logits.shape != y.shape:
            raise RuntimeError("shape mismatch")
        loss = nn.functional.binary_cross_entropy_with_logits(logits, y)
        if not torch.isfinite(loss):
            raise RuntimeError("non-finite loss")
        loss.backward()
        if not all(torch.isfinite(p.grad).all() for p in model.parameters()):
            raise RuntimeError("non-finite gradients")
        optimizer.step()
    predictions = (model(v).detach().cpu().reshape(-1) > 0).to(torch.int).tolist()
    Path("/output/predictions.json").write_text(
        json.dumps([{"id": row["id"], "prediction": pred} for row, pred in zip(val, predictions)])
    )
    Path("/output/training.json").write_text(
        json.dumps(
            {
                "steps": steps,
                "loss": loss.item(),
                "torch_version": torch.__version__,
                "cuda_version": torch.version.cuda,
                "gpu": torch.cuda.get_device_name(),
            }
        )
    )


if __name__ == "__main__":
    main()
