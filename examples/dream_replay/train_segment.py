# SPDX-License-Identifier: MPL-2.0
"""One deterministic CPU segment; PyTorch is imported only by the worker."""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import math
import platform
import sys
import time
from pathlib import Path

from common import (
    SOURCES,
    adapter_digest,
    artifact_path,
    checked_bytes,
    dataset_info,
    digest,
    encoded,
    learning_rate,
    recipe_for,
    splits,
    validate_manifest,
)


def work(args) -> dict:
    started = time.monotonic()
    manifest = json.loads(checked_bytes(args.manifest, args.manifest_sha256))
    validate_manifest(manifest)
    if manifest["adapter_sha256"] != adapter_digest():
        raise ValueError("adapter changed since manifest was recorded")
    config = manifest["config"]
    recipe = recipe_for(manifest, args.recipe)
    horizon = config["quantum"] * config["horizon"]
    valid_range = (
        0 < args.start == args.end <= horizon
        if args.test_only
        else 0 <= args.start < args.end <= horizon
    )
    if not valid_range:
        raise ValueError("invalid segment boundaries")
    if bool(args.checkpoint) != bool(args.checkpoint_sha256) or bool(args.start) != bool(
        args.checkpoint
    ):
        raise ValueError("continuation requires checkpoint and expected digest")
    prepared = Path(manifest["prepared"])
    model_bytes = checked_bytes(prepared / "model.py", SOURCES["model.py"][1])
    data = checked_bytes(prepared / "input.txt", manifest["dataset"]["sha256"])
    if dataset_info(data) != manifest["dataset"]:
        raise ValueError("dataset split or vocabulary differs from the recorded manifest")
    checkpoint_bytes = None
    if args.checkpoint:
        checkpoint_bytes = checked_bytes(
            artifact_path(args.manifest.parent, args.checkpoint), args.checkpoint_sha256
        )
    output = artifact_path(args.manifest.parent, args.output)
    if not args.test_only and output.exists():
        raise ValueError("checkpoint destination already exists")

    import torch

    torch.set_num_threads(1)
    torch.set_num_interop_threads(1)
    torch.use_deterministic_algorithms(True)
    torch.manual_seed(manifest["seed"])
    batch_rng = torch.Generator(device="cpu").manual_seed(manifest["seed"] + 1)
    runtime = {
        "torch": str(torch.__version__),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "device": "cpu",
        "dtype": "float32",
        "threads": 1,
    }
    contract = digest(encoded({"manifest": args.manifest_sha256, "recipe": args.recipe}))
    # Execute exactly the pinned bytes checked above, independent of PYTHONPATH.
    spec = importlib.util.spec_from_loader("dream_nanogpt", loader=None)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    exec(compile(model_bytes, str(prepared / "model.py"), "exec"), module.__dict__)
    vocab = manifest["dataset"]["vocabulary"]
    ids = {char: index + 1 for index, char in enumerate(vocab)}
    tensors = {
        name: torch.tensor([ids.get(char, 0) for char in text], dtype=torch.long)
        for name, text in splits(data).items()
    }
    with contextlib.redirect_stdout(sys.stderr):
        model = module.GPT(
            module.GPTConfig(
                block_size=config["context"],
                vocab_size=len(vocab) + 1,
                n_layer=config["layers"],
                n_head=config["heads"],
                n_embd=config["width"],
                dropout=recipe["dropout"],
                bias=False,
            )
        )
    optimizer = torch.optim.AdamW(
        model.parameters(),
        lr=recipe["lr"],
        betas=(0.9, 0.95),
        weight_decay=0.1,
        foreach=False,
    )
    if checkpoint_bytes is not None:
        saved = torch.load(io.BytesIO(checkpoint_bytes), map_location="cpu", weights_only=True)
        if saved["contract"] != contract or saved["step"] != args.start:
            raise ValueError("checkpoint recipe or update boundary mismatch")
        if saved["runtime"] != runtime:
            raise ValueError("checkpoint runtime differs from this worker")
        model.load_state_dict(saved["model"])
        optimizer.load_state_dict(saved["optimizer"])
        torch.set_rng_state(saved["torch_rng"])
        batch_rng.set_state(saved["batch_rng"])

    def batch(tokens, indices):
        n = config["context"]
        return (
            torch.stack([tokens[i : i + n] for i in indices]),
            torch.stack([tokens[i + 1 : i + n + 1] for i in indices]),
        )

    def check_time():
        if time.monotonic() - started > args.max_seconds:
            raise TimeoutError("worker time allowance exceeded; attempt retained as failed")

    def evaluate(split):
        model.eval()
        tokens = tensors[split]
        starts = (
            torch.linspace(0, len(tokens) - config["context"] - 1, config["eval_windows"])
            .long()
            .tolist()
        )
        losses = []
        with torch.no_grad():
            for start in starts:
                check_time()
                _, loss = model(*batch(tokens, [start]))
                losses.append(loss.item())
        return sum(losses) / len(losses)

    if args.test_only:
        test_loss = evaluate("test")
        if not math.isfinite(test_loss):
            raise ValueError("nonfinite test loss")
        return {
            "kind": "final-test",
            "manifest_sha256": args.manifest_sha256,
            "recipe": args.recipe,
            "input_sha256": args.checkpoint_sha256,
            "test_loss": test_loss,
            "runtime": runtime,
            "evaluation_tokens": config["eval_windows"] * config["context"],
        }

    train_started = time.monotonic()
    model.train()
    batch_trace = []
    for step in range(args.start, args.end):
        check_time()
        indices = torch.randint(
            len(tensors["train"]) - config["context"], (config["batch"],), generator=batch_rng
        ).tolist()
        batch_trace.extend(indices)
        for group in optimizer.param_groups:
            group["lr"] = learning_rate(recipe, step, horizon)
        optimizer.zero_grad(set_to_none=True)
        _, loss = model(*batch(tensors["train"], indices))
        if not math.isfinite(loss.item()):
            raise ValueError("nonfinite training loss")
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
        optimizer.step()
    training_seconds = time.monotonic() - train_started
    eval_started = time.monotonic()
    validation_loss = evaluate("validation")
    if not math.isfinite(validation_loss):
        raise ValueError("nonfinite validation loss")
    evaluation_seconds = time.monotonic() - eval_started
    saved = {
        "contract": contract,
        "runtime": runtime,
        "step": args.end,
        "model": model.state_dict(),
        "optimizer": optimizer.state_dict(),
        "torch_rng": torch.get_rng_state(),
        "batch_rng": batch_rng.get_state(),
    }
    checkpoint_started = time.monotonic()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("xb") as handle:
        torch.save(saved, handle)
    output_digest = digest(output.read_bytes())
    return {
        "kind": "segment",
        "manifest_sha256": args.manifest_sha256,
        "recipe": args.recipe,
        "start": args.start,
        "end": args.end,
        "training_tokens": (args.end - args.start) * config["batch"] * config["context"],
        "evaluation_tokens": config["eval_windows"] * config["context"],
        "validation_loss": validation_loss,
        "runtime": runtime,
        "input_sha256": args.checkpoint_sha256,
        "checkpoint": args.output,
        "checkpoint_sha256": output_digest,
        "batch_trace_sha256": digest(encoded(batch_trace)),
        "training_seconds": training_seconds,
        "evaluation_seconds": evaluation_seconds,
        "checkpoint_seconds": time.monotonic() - checkpoint_started,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--recipe", required=True)
    parser.add_argument("--start", required=True, type=int)
    parser.add_argument("--end", required=True, type=int)
    parser.add_argument("--checkpoint")
    parser.add_argument("--checkpoint-sha256")
    parser.add_argument("--output", required=True)
    parser.add_argument("--test-only", action="store_true")
    parser.add_argument("--max-seconds", type=float, default=60)
    args = parser.parse_args()
    try:
        if not math.isfinite(args.max_seconds) or args.max_seconds <= 0:
            raise ValueError("max-seconds must be positive and finite")
        print(json.dumps(work(args), allow_nan=False))
    except (OSError, ValueError, RuntimeError, KeyError, StopIteration, ImportError) as exc:
        print(f"training segment failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
