# SPDX-License-Identifier: MPL-2.0
"""Dream-RSI section 3 replay and a restricted, bounded Python policy runner.

Policies see only the revealed tree. An unavailable historical continuation is
not fabricated and does not close a leaf: it consumes a decision round. The
runner restricts generated source and bounds work, but is NOT a security sandbox
for hostile code. Run this example only with reviewed/trusted agent output.
"""

from __future__ import annotations

import ast
import copy
import json
import math
import operator
import subprocess
import sys
from pathlib import Path

SCHEMA = "dream-rsi-v1"
PUBLIC_FIELDS = ("id", "parent", "score", "status", "depth", "candidate", "diagnostic")
MAX_SOURCE = 20_000
MAX_OPERATIONS = 100_000
MAX_ITEMS = 10_000

# A fixed depth-two exploration control. A failed candidate can still be refined.
FIXED_POLICY = """def choose(observation):
    batch = []
    for node in observation["nodes"]:
        if node["id"] in observation["legal"] and node["depth"] < 2:
            batch = batch + [node["id"]]
    batch = batch + ["root"]
    return batch[:observation["workers"]]
"""

GREEDY_POLICY = """def choose(observation):
    leaves = [node for node in observation["nodes"] if node["id"] in observation["legal"]]
    if not leaves:
        return ["root"]
    leaves = sorted(
        leaves,
        key=lambda node: node["score"] if node["status"] == "ok" else observation["root_score"],
        reverse=True,
    )
    batch = [node["id"] for node in leaves[:observation["workers"]]]
    if len(batch) < observation["workers"]:
        batch = batch + ["root"]
    return batch
"""

# Deterministic round-robin across the current legal actions, not a random agent.
UNIFORM_POLICY = """def choose(observation):
    legal = observation["legal"]
    offset = observation["round"] % len(legal)
    ordered = legal[offset:] + legal[:offset]
    return ordered[:observation["workers"]]
"""


class SourceError(ValueError):
    """Source or its output violates this example's restricted-code contract."""


class ExecutionLimitError(SourceError):
    """Generated code exceeded its operation or elapsed-time budget."""


def _finite(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number")
    try:
        valid = math.isfinite(value)
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError(f"{label} must be a finite number")
    return value


def _integer(value, label, minimum=0):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")


def validate_world(world):
    """Validate an ordered discovery tree without trusting opaque evidence."""
    if not isinstance(world, dict) or world.get("schema") != SCHEMA:
        raise ValueError("unsupported world schema")
    _integer(world.get("seed"), "seed")
    _finite(world.get("root_score"), "root_score")
    nodes = world.get("nodes")
    if not isinstance(nodes, list):
        raise ValueError("nodes must be a list")
    known = {"root": 0}
    parents = set()
    for node in nodes:
        if not isinstance(node, dict) or not all(key in node for key in PUBLIC_FIELDS):
            raise ValueError("node is missing public fields")
        identity, parent = node["id"], node["parent"]
        if not isinstance(identity, str) or not identity or identity in known:
            raise ValueError("node IDs must be unique nonempty strings other than root")
        if not isinstance(parent, str) or parent not in known:
            raise ValueError("a parent must precede its child")
        if parent != "root" and parent in parents:
            raise ValueError("a non-root parent may have only one recorded child")
        _integer(node["depth"], "depth", 1)
        if node["depth"] != known[parent] + 1:
            raise ValueError("node depth disagrees with its parent")
        if node["status"] == "ok":
            _finite(node["score"], "successful node score")
        elif node["status"] != "failed" or node["score"] is not None:
            raise ValueError("failed nodes must have a null score")
        if not isinstance(node["candidate"], dict) or not isinstance(node["diagnostic"], str):
            raise ValueError("candidate must be an object and diagnostic a string")
        known[identity] = node["depth"]
        parents.add(parent)
    try:
        json.dumps(world, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("world must contain finite JSON data") from exc


def observation(nodes, root_score, workers, round_index, max_rounds, beta_cost, beta_parallel):
    """Construct the shared online/replay input, stripping private evidence."""
    _integer(workers, "workers", 1)
    _integer(round_index, "round_index")
    _integer(max_rounds, "max_rounds")
    if round_index > max_rounds:
        raise ValueError("round_index exceeds max_rounds")
    for value, label in ((beta_cost, "beta_cost"), (beta_parallel, "beta_parallel")):
        if _finite(value, label) < 0:
            raise ValueError(f"{label} must be nonnegative")
    validate_world({"schema": SCHEMA, "seed": 0, "root_score": root_score, "nodes": nodes})
    parents = {node["parent"] for node in nodes}
    return {
        "nodes": [{key: copy.deepcopy(node[key]) for key in PUBLIC_FIELDS} for node in nodes],
        "legal": ["root"] + [node["id"] for node in nodes if node["id"] not in parents],
        "workers": workers,
        "round": round_index,
        "max_rounds": max_rounds,
        "root_score": root_score,
        "beta_cost": beta_cost,
        "beta_parallel": beta_parallel,
    }


def validate_batch(batch, obs):
    if not isinstance(batch, list) or any(not isinstance(item, str) for item in batch):
        raise ValueError("policy batch must be a list of node IDs; [] means STOP")
    if len(batch) > obs["workers"]:
        raise ValueError("policy batch exceeds available workers")
    if len(batch) != len(set(batch)):
        raise ValueError("policy batch contains duplicate node IDs")
    if any(item not in obs["legal"] for item in batch):
        raise ValueError("policy selected an illegal node")


_ALLOWED_AST = {
    ast.Module,
    ast.FunctionDef,
    ast.arguments,
    ast.arg,
    ast.Return,
    ast.Assign,
    ast.AugAssign,
    ast.If,
    ast.For,
    ast.While,
    ast.Break,
    ast.Continue,
    ast.Pass,
    ast.Expr,
    ast.Name,
    ast.Load,
    ast.Store,
    ast.Constant,
    ast.List,
    ast.Tuple,
    ast.Dict,
    ast.Subscript,
    ast.Slice,
    ast.BinOp,
    ast.UnaryOp,
    ast.BoolOp,
    ast.Compare,
    ast.IfExp,
    ast.Call,
    ast.keyword,
    ast.ListComp,
    ast.DictComp,
    ast.GeneratorExp,
    ast.comprehension,
    ast.Lambda,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Pow,
    ast.UAdd,
    ast.USub,
    ast.Not,
    ast.And,
    ast.Or,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.Is,
    ast.IsNot,
    ast.In,
    ast.NotIn,
}


def validate_source(source, function_name):
    """Allow one pure function, with no imports, attributes, or global state."""
    if function_name not in {"choose", "learning_rate"}:
        raise SourceError("only choose and learning_rate functions are supported")
    if not isinstance(source, str) or len(source) > MAX_SOURCE:
        raise SourceError("source must be a string of at most 20000 characters")
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError) as exc:
        raise SourceError(f"invalid Python source: {exc}") from exc
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.FunctionDef):
        raise SourceError("source must contain exactly one function")
    function = tree.body[0]
    expected_args = 1 if function_name == "choose" else 2
    if function.name != function_name or len(function.args.args) != expected_args:
        raise SourceError(f"expected {function_name} with {expected_args} positional arguments")
    if function.decorator_list or function.returns:
        raise SourceError("decorators and annotations are not supported")
    for node in ast.walk(tree):
        if type(node) not in _ALLOWED_AST:
            raise SourceError(f"unsupported Python syntax: {type(node).__name__}")
        if isinstance(node, ast.FunctionDef) and node is not function:
            raise SourceError("nested function definitions are not supported")
        if isinstance(node, ast.arguments) and (
            node.defaults
            or node.kw_defaults
            or node.vararg
            or node.kwarg
            or node.kwonlyargs
            or node.posonlyargs
        ):
            raise SourceError("defaults and variable/keyword-only arguments are not supported")
        if isinstance(node, ast.arg) and (node.annotation or node.arg.startswith("_")):
            raise SourceError("annotations and private names are not supported")
        if isinstance(node, ast.Name) and node.id.startswith("_"):
            raise SourceError("private and dunder names are not supported")
        if isinstance(node, ast.comprehension) and node.is_async:
            raise SourceError("async comprehensions are not supported")
        if isinstance(node, ast.Call) and not isinstance(node.func, ast.Name):
            raise SourceError("calls must use named functions")
        if isinstance(node, ast.keyword) and node.arg is None:
            raise SourceError("expanded keyword arguments are not supported")
        if isinstance(node, ast.Constant):
            value = node.value
            if not isinstance(value, (str, int, float, bool, type(None))):
                raise SourceError("unsupported literal")
            if isinstance(value, str) and len(value) > MAX_ITEMS:
                raise SourceError("literal is too large")
            if isinstance(value, int) and value.bit_length() > 256:
                raise SourceError("integer literal is too large")
            if isinstance(value, float) and not math.isfinite(value):
                raise SourceError("numeric literals must be finite")


def _bounded_range(*args):
    result = range(*args)
    if len(result) > MAX_ITEMS:
        raise ExecutionLimitError("range exceeds item budget")
    return result


def _bounded_value(value):
    if isinstance(value, int) and value.bit_length() > 1024:
        raise ExecutionLimitError("integer exceeds size budget")
    if isinstance(value, (str, list, tuple, dict)) and len(value) > MAX_ITEMS:
        raise ExecutionLimitError("collection exceeds item budget")
    return value


_OPERATORS = {
    "Add": operator.add,
    "Sub": operator.sub,
    "Mult": operator.mul,
    "Div": operator.truediv,
    "FloorDiv": operator.floordiv,
    "Mod": operator.mod,
    "Pow": operator.pow,
}


def _binary(kind, left, right):
    if kind == "Pow":
        if not isinstance(right, (int, float)) or not math.isfinite(right) or abs(right) > 1024:
            raise ExecutionLimitError("power exceeds exponent budget")
        if (
            isinstance(left, int)
            and isinstance(right, int)
            and right > 0
            and left.bit_length() * right > 1024
        ):
            raise ExecutionLimitError("power exceeds integer budget")
    if kind == "Mult":
        for value, count in ((left, right), (right, left)):
            if (
                isinstance(value, (str, list, tuple))
                and isinstance(count, int)
                and len(value) * count > MAX_ITEMS
            ):
                raise ExecutionLimitError("repetition exceeds item budget")
    if kind == "Mod" and isinstance(left, str):
        raise SourceError("string formatting is not supported")
    return _bounded_value(_OPERATORS[kind](left, right))


class _BoundArithmetic(ast.NodeTransformer):
    def visit_BinOp(self, node):
        self.generic_visit(node)
        return ast.copy_location(
            ast.Call(
                func=ast.Name(id="_binary", ctx=ast.Load()),
                args=[ast.Constant(type(node.op).__name__), node.left, node.right],
                keywords=[],
            ),
            node,
        )

    def visit_AugAssign(self, node):
        # Lower x += y so arithmetic receives the same bounds as x = x + y.
        target = copy.deepcopy(node.target)
        target.ctx = ast.Load()
        replacement = ast.Assign(
            targets=[node.target], value=ast.BinOp(target, node.op, node.value)
        )
        return self.visit(ast.copy_location(replacement, node))


def compile_function(source, function_name, *, max_operations=MAX_OPERATIONS):
    """Compile once; each invocation resets its opcode budget and copies inputs.

    This efficient helper belongs inside an already time-bounded evaluator.
    Use invoke_source/choose_policy for an additional subprocess timeout.
    """
    validate_source(source, function_name)
    _integer(max_operations, "max_operations", 1)
    tree = ast.fix_missing_locations(_BoundArithmetic().visit(ast.parse(source)))
    builtins = {
        "abs": abs,
        "min": min,
        "max": max,
        "sum": sum,
        "len": len,
        "range": _bounded_range,
        "round": round,
        "int": int,
        "float": float,
        "bool": bool,
        "list": list,
        "tuple": tuple,
        "dict": dict,
        "sorted": sorted,
        "enumerate": enumerate,
        "zip": zip,
        "all": all,
        "any": any,
        "pow": lambda left, right: _binary("Pow", left, right),
    }
    namespace = {"__builtins__": builtins, "_binary": _binary}
    filename = "<dream-rsi-generated>"
    exec(compile(tree, filename, "exec"), namespace)
    function = namespace[function_name]

    def call(*args):
        remaining = max_operations

        def trace(frame, event, arg):
            nonlocal remaining
            if frame.f_code.co_filename == filename:
                frame.f_trace_opcodes = True
                if event in {"opcode", "line", "call"}:
                    remaining -= 1
                    if remaining < 0:
                        raise ExecutionLimitError("generated function exceeded operation budget")
                return trace
            return None

        previous_trace = sys.gettrace()
        sys.settrace(trace)
        try:
            result = function(*copy.deepcopy(args))
            _bounded_value(result)
            json.dumps(result, allow_nan=False)
            return result
        finally:
            sys.settrace(previous_trace)

    return call


def _run_worker(request, timeout):
    if _finite(timeout, "timeout") <= 0:
        raise ValueError("timeout must be positive")
    try:
        completed = subprocess.run(
            [sys.executable, "-I", str(Path(__file__).resolve()), "--worker"],
            input=json.dumps(request, allow_nan=False),
            text=True,
            capture_output=True,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        raise ExecutionLimitError("generated code exceeded subprocess timeout") from exc
    try:
        response = json.loads(completed.stdout)
    except ValueError as exc:
        raise SourceError(f"generated-code worker failed (exit {completed.returncode})") from exc
    if not response.get("ok"):
        error_type = (
            ExecutionLimitError if response.get("type") == "ExecutionLimitError" else SourceError
        )
        raise error_type(response.get("error", "generated-code worker failed"))
    if completed.returncode != 0:
        raise SourceError(f"generated-code worker failed (exit {completed.returncode})")
    return response["result"]


def invoke_source(source, function_name, *args, timeout=2.0):
    validate_source(source, function_name)
    return _run_worker(
        {"operation": "invoke", "source": source, "function": function_name, "args": args}, timeout
    )


def choose_policy(source, obs, timeout=2.0):
    batch = invoke_source(source, "choose", obs, timeout=timeout)
    validate_batch(batch, obs)
    return batch


def _replay(world, source, workers, max_rounds, beta_cost, beta_parallel):
    validate_world(world)
    choose = compile_function(source, "choose")
    observed, seen, trace = [], set(), []
    rounds = 0
    # Check all configuration fields even when the world is empty.
    observation([], world["root_score"], workers, 0, max_rounds, beta_cost, beta_parallel)
    stop_reason = "round_limit"
    while rounds < max_rounds:
        if len(seen) == len(world["nodes"]):
            stop_reason = "exhausted"
            break
        obs = observation(
            observed, world["root_score"], workers, rounds, max_rounds, beta_cost, beta_parallel
        )
        batch = choose(obs)
        validate_batch(batch, obs)
        if not batch:
            stop_reason = "policy_stop"
            break
        revealed = []
        for parent in batch:
            child = next(
                (
                    node
                    for node in world["nodes"]
                    if node["parent"] == parent and node["id"] not in seen
                ),
                None,
            )
            if child is not None:
                seen.add(child["id"])
                observed.append(child)
                revealed.append({key: copy.deepcopy(child[key]) for key in PUBLIC_FIELDS})
        rounds += 1
        trace.append({"round": rounds, "batch": batch, "revealed": revealed})
        if len(seen) == len(world["nodes"]):
            stop_reason = "exhausted"
            break
    if not world["nodes"]:
        stop_reason = "exhausted"
    best = max(
        [world["root_score"]] + [node["score"] for node in observed if node["status"] == "ok"]
    )
    attempts = len(observed)
    score = best - beta_cost * attempts + beta_parallel * attempts / max(1, rounds)
    _finite(score, "replay score")
    return {
        "seed": world["seed"],
        "score": score,
        "best_score": best,
        "attempts": attempts,
        "rounds": rounds,
        "trace": trace,
        "stop_reason": stop_reason,
    }


def replay(
    world, source, workers=1, max_rounds=24, beta_cost=0.0, beta_parallel=0.0, *, timeout=5.0
):
    """Run one entire replay in a bounded subprocess; never call an evaluator."""
    validate_world(world)
    validate_source(source, "choose")
    return _run_worker(
        {
            "operation": "replay",
            "world": world,
            "source": source,
            "options": {
                "workers": workers,
                "max_rounds": max_rounds,
                "beta_cost": beta_cost,
                "beta_parallel": beta_parallel,
            },
        },
        timeout,
    )


def evaluate_policy(
    worlds, source, workers=1, max_rounds=24, beta_cost=0.0, beta_parallel=0.0, *, timeout=10.0
):
    """Evaluate a policy against a fixed, nonempty pool, resetting per world."""
    if not isinstance(worlds, list) or not worlds:
        raise ValueError("policy evaluation needs a nonempty list of worlds")
    for world in worlds:
        validate_world(world)
    validate_source(source, "choose")
    return _run_worker(
        {
            "operation": "evaluate",
            "worlds": worlds,
            "source": source,
            "options": {
                "workers": workers,
                "max_rounds": max_rounds,
                "beta_cost": beta_cost,
                "beta_parallel": beta_parallel,
            },
        },
        timeout,
    )


def _worker():
    try:
        request = json.load(sys.stdin)
        if request["operation"] == "invoke":
            result = compile_function(request["source"], request["function"])(*request["args"])
        elif request["operation"] == "replay":
            result = _replay(request["world"], request["source"], **request["options"])
        elif request["operation"] == "evaluate":
            reports = [
                _replay(world, request["source"], **request["options"])
                for world in request["worlds"]
            ]
            result = {
                "mean_score": math.fsum(report["score"] / len(reports) for report in reports),
                "worlds": reports,
            }
        else:
            raise ValueError("unknown worker operation")
        response = {"ok": True, "result": result}
    except Exception as exc:
        response = {"ok": False, "type": type(exc).__name__, "error": str(exc)}
    print(json.dumps(response, allow_nan=False))


if __name__ == "__main__":
    if sys.argv[1:] != ["--worker"]:
        raise SystemExit("This module is used by the Dream-RSI example controller.")
    _worker()
