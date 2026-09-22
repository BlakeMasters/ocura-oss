# SPDX-License-Identifier: MPL-2.0
"""Policies receive immutable observations and legal actions, never the history."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Observation:
    recipe: str
    quantum: int
    loss: float | None  # A failed attempt closes this branch and consumes its reservation.


def legal_actions(order: tuple[str, ...], horizon: int, seen: tuple[Observation, ...]) -> tuple:
    latest = {o.recipe: o for o in seen}
    opened = tuple(r for r in order if r in latest)
    next_new = next((r for r in order if r not in latest), None)
    available = tuple(
        r for r in opened if latest[r].loss is not None and latest[r].quantum < horizon
    )
    return available + ((next_new,) if next_new is not None else ())


def choose(policy: dict, order: tuple, horizon: int, seen: tuple, legal: tuple) -> str | None:
    if not legal:
        return None
    latest = {o.recipe: o for o in seen}
    counts = {r: latest[r].quantum if r in latest else 0 for r in order}
    mode = policy["mode"]
    if mode == "uniform":
        return min(legal, key=lambda r: (counts[r], order.index(r)))
    if mode == "fixed":
        for target in policy["ranking"]:
            if target not in latest:
                return next(r for r in legal if r not in latest)
            if target in legal:
                return target
        return None
    if mode == "greedy":
        unopened = [r for r in legal if r not in latest]
        if unopened and len(latest) < policy["explore"]:
            return unopened[0]
        continuing = [r for r in legal if r in latest]
        if continuing:
            return min(continuing, key=lambda r: (latest[r].loss, order.index(r)))
        return unopened[0] if unopened else None
    if mode == "halving":
        survivors = list(order)
        rung = 1
        while survivors:
            pending = [r for r in survivors if counts[r] < rung and r in legal]
            if pending:
                return min(pending, key=lambda r: (counts[r], order.index(r)))
            # Initial discovery still opens recipes in the shared order.
            if any(r not in latest for r in survivors):
                return next(r for r in legal if r not in latest)
            survivors = [r for r in survivors if latest[r].loss is not None]
            if not survivors or rung >= horizon:
                return None
            # Rank using only the observations at this rung, never later progress.
            scores = {(o.recipe, o.quantum): o.loss for o in seen}
            survivors.sort(key=lambda r: (scores[(r, rung)], order.index(r)))
            survivors = survivors[: max(1, len(survivors) // 2)]
            rung = min(horizon, rung * 2)
        return None
    raise ValueError(f"unknown policy mode: {mode}")


def candidates(ranking: list[str]) -> list[dict]:
    return [
        {"name": "uniform", "mode": "uniform"},
        {"name": "fixed-development-ranking", "mode": "fixed", "ranking": ranking},
        {"name": "successive-halving", "mode": "halving"},
        *({"name": f"greedy-explore-{n}", "mode": "greedy", "explore": n} for n in (1, 2, 4)),
    ]
