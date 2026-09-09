# SPDX-License-Identifier: MPL-2.0

"""Tiny finite-context autoregressive character model; optional CPU backends."""

from __future__ import annotations

import argparse
import json
import math
import platform
import random
import sys
from collections.abc import Callable, Sequence

CONTEXT = 8
HIDDEN = 32
TRAIN_LINES = (
    "red fox runs.\n",
    "red bird sings.\n",
    "red cat rests.\n",
    "blue fox sings.\n",
    "blue bird rests.\n",
    "blue cat runs.\n",
    "green fox rests.\n",
    "green bird runs.\n",
    "green cat sings.\n",
)
VALIDATION_LINES = ("red fox rests.\n", "blue bird runs.\n", "green cat runs.\n")
VOCAB = "\n .abcdefghijklmnopqrstuvwxyz"


def encode_context(text: str) -> list[float]:
    """Only preceding characters enter the next-character prediction."""
    context = ("\n" * CONTEXT + text)[-CONTEXT:]
    return [float(char == token) for char in context for token in VOCAB]


def examples(lines: Sequence[str]) -> tuple[list[list[float]], list[int]]:
    inputs, targets = [], []
    for line in lines:
        for position, char in enumerate(line):
            inputs.append(encode_context(line[:position]))
            targets.append(VOCAB.index(char))
    return inputs, targets


def initial_weights(seed: int) -> list:
    """Both backends start with the same small weights and full-batch SGD."""
    rng = random.Random(seed)
    width = CONTEXT * len(VOCAB)
    return [
        [[rng.gauss(0, 1 / math.sqrt(width)) for _ in range(HIDDEN)] for _ in range(width)],
        [0.0] * HIDDEN,
        [[rng.gauss(0, 1 / math.sqrt(HIDDEN)) for _ in VOCAB] for _ in range(HIDDEN)],
        [0.0] * len(VOCAB),
    ]


def generate(predict: Callable[[list[float]], list[float]]) -> str:
    """Greedy decoding feeds each predicted character back into the context."""
    text = "red "
    for _ in range(40):
        logits = predict(encode_context(text))
        token = VOCAB[max(range(len(VOCAB)), key=logits.__getitem__)]
        text += token
        if token == "\n":
            break
    return text


def train_pytorch(steps: int, learning_rate: float, seed: int) -> dict:
    import torch
    import torch.nn.functional as functional

    torch.set_num_threads(1)
    torch.use_deterministic_algorithms(True)
    x, y = examples(TRAIN_LINES)
    vx, vy = examples(VALIDATION_LINES)
    x, vx = (torch.tensor(data, dtype=torch.float32, device="cpu") for data in (x, vx))
    y, vy = (torch.tensor(data, dtype=torch.long, device="cpu") for data in (y, vy))
    weights = [
        torch.tensor(value, dtype=torch.float32, device="cpu", requires_grad=True)
        for value in initial_weights(seed)
    ]

    def logits(inputs):
        w1, b1, w2, b2 = weights
        return torch.tanh(inputs @ w1 + b1) @ w2 + b2

    initial = float(functional.cross_entropy(logits(x), y).detach())
    for _ in range(steps):
        loss = functional.cross_entropy(logits(x), y)
        loss.backward()
        with torch.no_grad():
            for weight in weights:
                weight -= learning_rate * weight.grad
                weight.grad = None
    with torch.no_grad():
        final = float(functional.cross_entropy(logits(x), y))
        validation = float(functional.cross_entropy(logits(vx), vy))
        sample = generate(lambda row: logits(torch.tensor([row], dtype=torch.float32))[0].tolist())
    return {
        "framework_version": torch.__version__,
        "initial_train_loss": initial,
        "train_loss": final,
        "validation_loss": validation,
        "generated_text": sample,
    }


def train_jax(steps: int, learning_rate: float, seed: int) -> dict:
    import jax
    import jax.numpy as jnp

    # Keep the example small and CPU-only even on a machine with accelerators.
    with jax.default_device(jax.devices("cpu")[0]):
        x, y = examples(TRAIN_LINES)
        vx, vy = examples(VALIDATION_LINES)
        x, vx = (jnp.asarray(data, dtype=jnp.float32) for data in (x, vx))
        y, vy = (jnp.asarray(data, dtype=jnp.int32) for data in (y, vy))
        weights = [jnp.asarray(value, dtype=jnp.float32) for value in initial_weights(seed)]

        def logits(params, inputs):
            w1, b1, w2, b2 = params
            return jnp.tanh(inputs @ w1 + b1) @ w2 + b2

        def loss(params, inputs, targets):
            log_probs = jax.nn.log_softmax(logits(params, inputs), axis=-1)
            return -jnp.mean(log_probs[jnp.arange(targets.shape[0]), targets])

        @jax.jit
        def update(params):
            _, grads = jax.value_and_grad(loss)(params, x, y)
            return [value - learning_rate * grad for value, grad in zip(params, grads, strict=True)]

        initial = float(loss(weights, x, y))
        for _ in range(steps):
            weights = update(weights)
        final = float(loss(weights, x, y))
        validation = float(loss(weights, vx, vy))
        sample = generate(
            lambda row: logits(weights, jnp.asarray([row], dtype=jnp.float32))[0].tolist()
        )
    return {
        "framework_version": jax.__version__,
        "initial_train_loss": initial,
        "train_loss": final,
        "validation_loss": validation,
        "generated_text": sample,
    }


def train(backend: str, steps: int, learning_rate: float, seed: int) -> dict:
    if steps <= 0 or not math.isfinite(learning_rate) or learning_rate <= 0:
        raise ValueError("steps and learning rate must be positive and finite")
    if backend not in ("pytorch", "jax"):
        raise ValueError(f"unknown backend: {backend}")
    result = (train_pytorch if backend == "pytorch" else train_jax)(steps, learning_rate, seed)
    if not all(math.isfinite(result[key]) for key in ("train_loss", "validation_loss")):
        raise ValueError("training produced a nonfinite loss")
    return {
        "example": "autoregressive-character-v1",
        "backend": backend,
        "device": "cpu",
        "python_version": platform.python_version(),
        "steps": steps,
        "learning_rate": learning_rate,
        "seed": seed,
        "context_length": CONTEXT,
        "hidden_size": HIDDEN,
        "train_tokens": sum(map(len, TRAIN_LINES)),
        "validation_tokens": sum(map(len, VALIDATION_LINES)),
        **result,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", required=True, choices=("pytorch", "jax"))
    parser.add_argument("--steps", type=int, default=40)
    parser.add_argument("--learning-rate", type=float, default=0.5)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args(argv)
    try:
        result = train(args.backend, args.steps, args.learning_rate, args.seed)
        print(json.dumps({**result, "executor": "local"}, allow_nan=False))
    except (ImportError, ValueError) as exc:
        print(f"error: {exc}; install the chosen example backend (see README.md)", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
