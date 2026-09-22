# SPDX-License-Identifier: MPL-2.0
"""Small, standard-library-only contracts shared by the example commands."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCHEMA = "dream-replay-v1"
NANOGPT = "3adf61e154c3fe3fca428ad6bc3818b27a3b8291"
CHAR_RNN = "6f9487a6fe5b420b7ca9afb0d7c078e37c1d1b4e"
SOURCES = {
    "model.py": (
        f"https://raw.githubusercontent.com/karpathy/nanoGPT/{NANOGPT}/model.py",
        "7c01703240dbec5d554527dc666e35b3df8391d0b117fddc07afcf325a21d11c",
    ),
    "LICENSE.nanoGPT": (
        f"https://raw.githubusercontent.com/karpathy/nanoGPT/{NANOGPT}/LICENSE",
        "a59ec5cdb1c1e447e5266a014b3e7ded511f8d6e5a08931e931c87490ff821fe",
    ),
    "input.txt": (
        f"https://raw.githubusercontent.com/karpathy/char-rnn/{CHAR_RNN}"
        "/data/tinyshakespeare/input.txt",
        "86c4e6aa9db7c042ec79f339dcb96d42b0075e16b8fc2e86bf0ca57e2dc565ed",
    ),
}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def encoded(value) -> bytes:
    return json.dumps(value, sort_keys=True, allow_nan=False, separators=(",", ":")).encode()


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value) -> None:
    with path.open("xb") as handle:
        handle.write(encoded(value))


def adapter_digest() -> str:
    return digest(b"".join(p.name.encode() + p.read_bytes() for p in sorted(HERE.glob("*.py"))))


def checked_bytes(path: Path, expected: str) -> bytes:
    data = path.read_bytes()
    if digest(data) != expected:
        raise ValueError(f"digest mismatch: {path.name}")
    return data


def artifact_path(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to((root / "checkpoints").resolve()):
        raise ValueError("checkpoint must be inside the experiment's checkpoints directory")
    return path


def splits(data: bytes) -> dict[str, str]:
    text = data.decode("utf-8")
    a, b = int(len(text) * 0.8), int(len(text) * 0.9)
    return {"train": text[:a], "validation": text[a:b], "test": text[b:]}


def dataset_info(data: bytes) -> dict:
    parts = splits(data)
    return {
        "sha256": digest(data),
        "split_sha256": {k: digest(v.encode()) for k, v in parts.items()},
        "split_characters": {k: len(v) for k, v in parts.items()},
        "vocabulary": "".join(sorted(set(parts["train"]))),
        "unknown_id": 0,
    }


def configuration(profile: str) -> dict:
    return read_json(HERE / "recipes.json")[profile]


def validate_manifest(manifest: dict) -> None:
    if manifest.get("schema") != SCHEMA or manifest.get("kind") != "manifest":
        raise ValueError("unsupported example manifest")
    config = manifest["config"]
    for name in (
        "layers",
        "heads",
        "width",
        "context",
        "batch",
        "quantum",
        "horizon",
        "eval_windows",
    ):
        if type(config[name]) is not int or config[name] <= 0:
            raise ValueError(f"invalid configuration: {name}")
    if config["width"] % config["heads"] or config["threads"] != 1:
        raise ValueError("expected divisible attention heads and one CPU thread")
    if manifest.get("purpose") not in ("collection", "evaluation"):
        raise ValueError("invalid experiment purpose")
    recipes = config["recipes"]
    names = [r["name"] for r in recipes]
    if not names or len(set(names)) != len(names) or set(names) != set(manifest["order"]):
        raise ValueError("invalid recipe catalogue/order")
    if len(names) != len(manifest["order"]):
        raise ValueError("duplicate recipe order")
    if type(manifest["seed"]) is not int or not 0 <= manifest["seed"] < 2**32:
        raise ValueError("seed must be in [0, 2**32)")
    for recipe in recipes:
        if (
            recipe["schedule"] not in ("constant", "cosine")
            or not 0 <= recipe["dropout"] < 1
            or not math.isfinite(recipe["lr"])
            or recipe["lr"] <= 0
            or type(recipe["warmup"]) is not int
            or recipe["warmup"] < 0
        ):
            raise ValueError("invalid recipe")


def recipe_for(manifest: dict, name: str) -> dict:
    return next(r for r in manifest["config"]["recipes"] if r["name"] == name)


def quantum_tokens(manifest: dict) -> int:
    c = manifest["config"]
    return c["quantum"] * c["batch"] * c["context"]


def learning_rate(recipe: dict, step: int, horizon: int) -> float:
    if step < recipe["warmup"]:
        return recipe["lr"] * (step + 1) / recipe["warmup"]
    if recipe["schedule"] == "constant":
        return recipe["lr"]
    fraction = (step - recipe["warmup"]) / max(1, horizon - recipe["warmup"] - 1)
    return recipe["lr"] * (0.1 + 0.9 * (1 + math.cos(math.pi * fraction)) / 2)
