# SPDX-License-Identifier: MPL-2.0

"""Execute the same training workload as one local Ray Core task."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", required=True, choices=("pytorch", "jax"))
    parser.add_argument("--steps", type=int, default=40)
    parser.add_argument("--learning-rate", type=float, default=0.5)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args(argv)
    try:
        import ray
        from train import train
    except ImportError as exc:
        print(f"error: {exc}; install Ray and the chosen backend (see README.md)", file=sys.stderr)
        return 2
    # A fresh, explicitly local runtime; never attach to RAY_ADDRESS or an existing cluster.
    # Only the driver writes Ocura state. The worker returns its metrics to this command.
    try:
        ray.init(address="local", num_cpus=1, include_dashboard=False, log_to_driver=False)
        task = ray.remote(num_cpus=1, max_retries=0)(train)
        result = ray.get(
            task.remote(args.backend, args.steps, args.learning_rate, args.seed), timeout=120
        )
        print(
            json.dumps(
                {**result, "executor": "ray", "ray_version": ray.__version__}, allow_nan=False
            )
        )
    finally:
        ray.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
