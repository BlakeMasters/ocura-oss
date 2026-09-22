# SPDX-License-Identifier: MPL-2.0
"""Fixed CPU evaluator for bounded, agent-written learning-rate schedules.

Only the schedule may change between attempts. Children restore model, AdamW,
update index, CPU RNG, and batch RNG from verified checkpoint bytes. This module
does not import PyTorch until the immutable request and its inputs are checked.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import math
import platform
import re
import sys
import time
from pathlib import Path

try:
    from .rsi_core import compile_function, validate_source
except ImportError:
    from rsi_core import compile_function, validate_source

HERE = Path(__file__).resolve().parent
# A private module name avoids the other example's generic `common` import.
_spec = importlib.util.spec_from_file_location(
    "_dream_rsi_pinned_inputs", HERE.parent / "dream_replay" / "common.py"
)
_common = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_common)
SOURCES = _common.SOURCES
checked_bytes = _common.checked_bytes
dataset_info = _common.dataset_info
digest = _common.digest
encoded = _common.encoded
splits = _common.splits

SCHEMA = "dream-rsi-training-v1"
PROFILES = {
    "smoke": {
        "layers": 1,
        "heads": 2,
        "width": 32,
        "context": 16,
        "batch": 2,
        "eval_windows": 4,
        "dropout": 0.1,
        "quantum": 2,
    },
    "cpu": {
        "layers": 2,
        "heads": 4,
        "width": 128,
        "context": 64,
        "batch": 8,
        "eval_windows": 8,
        "dropout": 0.1,
        "quantum": 32,
    },
}
MIN_LR, MAX_LR = 0.00001, 0.02
REQUEST_FIELDS = {
    "operation",
    "profile",
    "prepared",
    "seed",
    "updates",
    "total_steps",
    "schedule_source",
    "parent_checkpoint",
    "parent_checkpoint_sha256",
    "output_checkpoint",
    "max_seconds",
}


def configuration(profile: str) -> dict:
    if not isinstance(profile, str) or profile not in PROFILES:
        raise ValueError("profile must be smoke or cpu")
    return dict(PROFILES[profile])


def _absolute_path(value, name: str) -> Path:
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        raise ValueError(f"{name} must be an absolute path")
    return Path(value)


def validate_request(request: dict) -> dict:
    """Validate the control surface without importing or starting training."""
    if not isinstance(request, dict) or set(request) - REQUEST_FIELDS:
        raise ValueError("request must contain only the documented evaluator fields")
    operation = request.get("operation")
    if operation not in ("initialize", "train", "test"):
        raise ValueError("operation must be initialize, train, or test")
    config = configuration(request.get("profile"))
    for key, low, high in (("seed", 0, 2**32 - 1), ("total_steps", 1, 4096)):
        value = request.get(key)
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"{key} must be an integer in [{low}, {high}]")
    updates = request.get("updates")
    if type(updates) is not int or not 0 <= updates <= min(256, request["total_steps"]):
        raise ValueError("updates must be an integer in [0, min(256, total_steps)]")
    if (operation == "train") != (updates > 0):
        raise ValueError("train needs positive updates; initialize/test need zero updates")
    _absolute_path(request.get("prepared"), "prepared")
    parent, parent_sha = request.get("parent_checkpoint"), request.get("parent_checkpoint_sha256")
    if operation == "initialize":
        if parent is not None or parent_sha is not None:
            raise ValueError("initialize cannot restore a parent checkpoint")
    else:
        _absolute_path(parent, "parent_checkpoint")
        if not isinstance(parent_sha, str) or re.fullmatch(r"[0-9a-f]{64}", parent_sha) is None:
            raise ValueError("parent_checkpoint_sha256 must be a lowercase SHA-256 digest")
    output = request.get("output_checkpoint")
    if operation == "test":
        if output is not None:
            raise ValueError("test does not write a checkpoint")
    else:
        _absolute_path(output, "output_checkpoint")
        if parent is not None and Path(parent).resolve() == Path(output).resolve():
            raise ValueError("output must not overwrite the parent checkpoint")
    allowance = request.get("max_seconds", 60)
    if type(allowance) not in (int, float) or not math.isfinite(allowance) or allowance <= 0:
        raise ValueError("max_seconds must be positive and finite")
    source = request.get("schedule_source")
    if not isinstance(source, str):
        raise ValueError("schedule_source must be a string")
    validate_source(source, "learning_rate")
    return config


def checked_schedule(source: str, total_steps: int, check_time=lambda: None) -> list[float]:
    """Materialize the whole bounded horizon, rejecting invalid future values too."""
    function = compile_function(source, "learning_rate")
    values = []
    for step in range(total_steps):
        check_time()
        value = function(step, total_steps)
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError(f"learning_rate at step {step} must return a finite number")
        if not MIN_LR <= value <= MAX_LR:
            raise ValueError(f"learning_rate at step {step} must be in [{MIN_LR}, {MAX_LR}]")
        values.append(float(value))
    return values


def execute(request: dict) -> dict:
    started = time.monotonic()
    config = validate_request(request)
    operation = request["operation"]

    def check_time():
        if time.monotonic() - started > request.get("max_seconds", 60):
            raise TimeoutError("CPU evaluator time allowance exceeded")

    rates = checked_schedule(request["schedule_source"], request["total_steps"], check_time)
    prepared = Path(request["prepared"])
    model_bytes = checked_bytes(prepared / "model.py", SOURCES["model.py"][1])
    checked_bytes(prepared / "LICENSE.nanoGPT", SOURCES["LICENSE.nanoGPT"][1])
    data = checked_bytes(prepared / "input.txt", SOURCES["input.txt"][1])
    dataset = dataset_info(data)
    parent_bytes = None
    if request.get("parent_checkpoint") is not None:
        parent_bytes = checked_bytes(
            Path(request["parent_checkpoint"]), request["parent_checkpoint_sha256"]
        )
    output = Path(request["output_checkpoint"]) if operation != "test" else None
    if output is not None and output.exists():
        raise ValueError("checkpoint destination already exists")
    evaluator_sha = digest(Path(__file__).read_bytes())
    support_sha = {
        "rsi_core.py": digest((HERE / "rsi_core.py").read_bytes()),
        "dream_replay/common.py": digest(Path(_common.__file__).read_bytes()),
    }
    contract = digest(
        encoded(
            {
                "schema": SCHEMA,
                "config": config,
                "profile": request["profile"],
                "seed": request["seed"],
                "total_steps": request["total_steps"],
                "dataset": dataset,
                "source_sha256": {name: item[1] for name, item in SOURCES.items()},
                "optimizer": {
                    "kind": "AdamW",
                    "betas": [0.9, 0.95],
                    "weight_decay": 0.1,
                    "foreach": False,
                    "clip_grad_norm": 1.0,
                },
                "evaluator_sha256": evaluator_sha,
                "support_sha256": support_sha,
            }
        )
    )
    check_time()

    import torch

    torch.set_num_threads(1)
    if torch.get_num_interop_threads() != 1:
        torch.set_num_interop_threads(1)
    torch.use_deterministic_algorithms(True)
    torch.set_default_dtype(torch.float32)
    torch.manual_seed(request["seed"])
    batch_rng = torch.Generator(device="cpu").manual_seed(request["seed"] + 1)
    runtime = {
        "torch": str(torch.__version__),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "device": "cpu",
        "dtype": "float32",
        "threads": 1,
        "interop_threads": 1,
        "deterministic_algorithms": True,
    }
    spec = importlib.util.spec_from_loader("_dream_rsi_nanogpt", loader=None)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    # Compile precisely the bytes whose upstream digest was checked above.
    exec(compile(model_bytes, str(prepared / "model.py"), "exec"), module.__dict__)
    vocab = dataset["vocabulary"]
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
                dropout=config["dropout"],
                bias=False,
            )
        ).to(device="cpu", dtype=torch.float32)
    optimizer = torch.optim.AdamW(
        model.parameters(), lr=rates[0], betas=(0.9, 0.95), weight_decay=0.1, foreach=False
    )
    start = 0
    if parent_bytes is not None:
        # Never reopen the pathname after verification: deserialize the verified bytes.
        saved = torch.load(io.BytesIO(parent_bytes), map_location="cpu", weights_only=True)
        if saved["contract"] != contract:
            raise ValueError("checkpoint evaluator, data, seed, profile, or horizon mismatch")
        if saved["runtime"] != runtime:
            raise ValueError("checkpoint runtime differs from this worker")
        start = saved["step"]
        if type(start) is not int or not 0 <= start <= request["total_steps"]:
            raise ValueError("checkpoint has an invalid update index")
        model.load_state_dict(saved["model"])
        optimizer.load_state_dict(saved["optimizer"])
        torch.set_rng_state(saved["torch_rng"])
        batch_rng.set_state(saved["batch_rng"])
    end = start + request["updates"]
    if end > request["total_steps"]:
        raise ValueError("attempt exceeds the declared training horizon")

    def batch(tokens, indices):
        context = config["context"]
        return (
            torch.stack([tokens[i : i + context] for i in indices]),
            torch.stack([tokens[i + 1 : i + context + 1] for i in indices]),
        )

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
            for offset in starts:
                check_time()
                _, loss = model(*batch(tokens, [offset]))
                losses.append(loss.item())
        result = sum(losses) / len(losses)
        if not math.isfinite(result):
            raise ValueError(f"nonfinite {split} loss")
        return result

    shared = {
        "schema": SCHEMA,
        "profile": request["profile"],
        "seed": request["seed"],
        "contract_sha256": contract,
        "evaluator_sha256": evaluator_sha,
        "support_sha256": support_sha,
        "schedule_sha256": digest(request["schedule_source"].encode("utf-8")),
        "input_checkpoint_sha256": request.get("parent_checkpoint_sha256"),
        "start": start,
        "end": end,
        "step": end,
        "total_steps": request["total_steps"],
        "runtime": runtime,
        "evaluation_tokens": config["eval_windows"] * config["context"],
    }
    if operation == "test":
        evaluation_started = time.monotonic()
        test_loss = evaluate("test")
        return {
            **shared,
            "kind": "final-test",
            "test_loss": test_loss,
            "training_tokens": 0,
            "checkpoint": request["parent_checkpoint"],
            "checkpoint_sha256": request["parent_checkpoint_sha256"],
            "evaluation_seconds": time.monotonic() - evaluation_started,
            "wall_seconds": time.monotonic() - started,
        }

    training_started = time.monotonic()
    model.train()
    batch_trace = []
    for step in range(start, end):
        check_time()
        indices = torch.randint(
            len(tensors["train"]) - config["context"], (config["batch"],), generator=batch_rng
        ).tolist()
        batch_trace.extend(indices)
        for group in optimizer.param_groups:
            group["lr"] = rates[step]
        optimizer.zero_grad(set_to_none=True)
        _, loss = model(*batch(tensors["train"], indices))
        if not math.isfinite(loss.item()):
            raise ValueError("nonfinite training loss")
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
        optimizer.step()
    training_seconds = time.monotonic() - training_started
    evaluation_started = time.monotonic()
    validation_loss = evaluate("validation")
    evaluation_seconds = time.monotonic() - evaluation_started
    saved = {
        "contract": contract,
        "runtime": runtime,
        "step": end,
        "model": model.state_dict(),
        "optimizer": optimizer.state_dict(),
        "torch_rng": torch.get_rng_state(),
        "batch_rng": batch_rng.get_state(),
        "schedule_sha256": shared["schedule_sha256"],
    }
    checkpoint_started = time.monotonic()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("xb") as handle:
        torch.save(saved, handle)
    output_sha = digest(output.read_bytes())
    return {
        **shared,
        "kind": "initialization" if operation == "initialize" else "training-attempt",
        "validation_loss": validation_loss,
        "score": -validation_loss,
        "training_tokens": (end - start) * config["batch"] * config["context"],
        "checkpoint": str(output),
        "checkpoint_sha256": output_sha,
        "batch_trace_sha256": digest(encoded(batch_trace)),
        "learning_rates_sha256": digest(encoded(rates[start:end])),
        "training_seconds": training_seconds,
        "evaluation_seconds": evaluation_seconds,
        "checkpoint_seconds": time.monotonic() - checkpoint_started,
        "wall_seconds": time.monotonic() - started,
    }


def read_request(path: Path, sha256: str) -> dict:
    def reject_constant(value):
        raise ValueError(f"invalid JSON numeric constant: {value}")

    return json.loads(checked_bytes(path, sha256), parse_constant=reject_constant)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--sha256", required=True)
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument("--initialize", action="store_true")
    operation.add_argument("--test-only", action="store_true")
    args = parser.parse_args()
    try:
        request = read_request(args.request, args.sha256)
        expected = "initialize" if args.initialize else "test" if args.test_only else None
        if expected is not None and request.get("operation") != expected:
            raise ValueError("CLI operation must match the immutable request")
        print(json.dumps(execute(request), sort_keys=True, allow_nan=False))
    except (
        OSError,
        ValueError,
        RuntimeError,
        KeyError,
        TypeError,
        ImportError,
        TimeoutError,
    ) as exc:
        print(f"Dream-RSI training failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
