# SPDX-License-Identifier: MPL-2.0

"""Argparse wiring, exit-code mapping, concise text views, and JSON summaries."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from ocura_oss import branching, demo, model, runner, store
from ocura_oss.model import ModelError
from ocura_oss.store import StoreError

EXIT_OK = 0
EXIT_COMMAND_FAILED = 1
EXIT_INVALID = 2
EXIT_LAUNCH_FAILED = 3


def main(argv: Sequence[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    parser = _build_parser()
    command_tail: list[str] = []
    if raw and raw[0] == "run":
        wants_help = "-h" in raw or "--help" in raw
        if "--" not in raw and not wants_help:
            print(
                "error: put the command after -- so its flags are not"
                " interpreted by ocura-oss; for example:",
                file=sys.stderr,
            )
            print("  ocura-oss run -- python script.py", file=sys.stderr)
            return EXIT_INVALID
        separator = raw.index("--") if "--" in raw else len(raw)
        head = raw[:separator]
        command_tail = raw[separator + 1 :]
        args = parser.parse_args(head)
    else:
        args = parser.parse_args(raw)
    try:
        return _dispatch(args, command_tail)
    except (StoreError, ModelError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_INVALID
    except RecursionError:
        print(
            "error: state lineage is too deeply nested to verify;"
            " the state directory may be corrupt",
            file=sys.stderr,
        )
        return EXIT_INVALID
    except demo.DemoError as exc:
        print(f"error: demo step '{exc.step}' failed: {exc.message}", file=sys.stderr)
        print("the demo directory was retained", file=sys.stderr)
        return EXIT_INVALID
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_INVALID


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ocura-oss",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Ocura OSS records a research workflow: run, evidence, chokepoint, "
            "branch, rerun, compare. An early concept for trusted same-owner "
            "local work; commands are not sandboxed."
        ),
    )
    subparsers = parser.add_subparsers(dest="command_name", required=True)

    init_parser = subparsers.add_parser(
        "init",
        help="create a den and a default pathway",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Creates one den and one default pathway under ROOT/.ocura-oss/."
            " Returns exit code 2 if state already exists."
        ),
    )
    init_parser.add_argument(
        "--root",
        default=None,
        metavar="PATH",
        help="project root directory (default: current directory)",
    )
    init_parser.add_argument(
        "--name",
        default="ocura-oss",
        metavar="NAME",
        help="name recorded in the den (default: ocura-oss)",
    )

    run_parser = subparsers.add_parser(
        "run",
        help="run one command and record evidence",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Runs COMMAND directly with shell=False, the project root as working"
            " directory, and captured stdout/stderr logs. Everything after -- is"
            " the command; put run's own options before it.\n"
            "Example: ocura-oss run --pathway ID --param batch=2 -- python script.py\n"
            "While the command runs, its output streams to your terminal and is"
            " recorded under .ocura-oss/logs/. If you press Ctrl+C, the partial"
            " attempt is still recorded as interrupted evidence; a second"
            " Ctrl+C exits immediately instead.\n"
            "Exit codes: 0 passed, 1 command failed or was interrupted,"
            " 3 could not launch; every attempt produces a terminal chokepoint."
        ),
    )
    run_parser.add_argument(
        "--root",
        default=None,
        metavar="PATH",
        help="project root directory (default: current directory)",
    )
    run_parser.add_argument(
        "--pathway",
        default=None,
        metavar="ID",
        help="pathway to attach evidence to (default: the den's default pathway)",
    )
    run_parser.add_argument(
        "--param",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="declared parameter stored on this atom only; repeatable",
    )
    run_parser.add_argument(
        "--quiet",
        action="store_true",
        help="do not stream the command's output to the terminal",
    )
    run_parser.add_argument(
        "command",
        nargs="*",
        metavar="COMMAND",
        help="command tokens after -- (required); executed verbatim",
    )

    pathways_parser = subparsers.add_parser(
        "pathways",
        help="list pathways oldest first",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Lists each pathway's lineage: parent, source chokepoint, creation"
            " time, reason, declared parameters, and whether terminal evidence"
            " is attached."
        ),
    )
    pathways_parser.add_argument(
        "--root",
        default=None,
        metavar="PATH",
        help="project root directory (default: current directory)",
    )
    pathways_parser.add_argument(
        "--json", action="store_true", help="print machine-readable JSON instead of text"
    )

    chokepoints_parser = subparsers.add_parser(
        "chokepoints",
        help="list terminal chokepoints newest first",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Lists terminal evidence boundaries. Never includes commands or log contents.",
    )
    chokepoints_parser.add_argument(
        "--root",
        default=None,
        metavar="PATH",
        help="project root directory (default: current directory)",
    )
    chokepoints_parser.add_argument(
        "--json", action="store_true", help="print machine-readable JSON instead of text"
    )

    branch_parser = subparsers.add_parser(
        "branch",
        help="branch lineage metadata from a chokepoint",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Records a child pathway whose effective parameters are the parent's"
            " plus your overrides. Metadata only: no workspace, artifact, or"
            " process is copied or rewound. Returns exit code 2 for missing,"
            " checksum-mismatched, nonterminal, nonbranchable, or unverifiable sources."
        ),
    )
    branch_parser.add_argument(
        "--from",
        dest="source",
        required=True,
        metavar="CHOKEPOINT_ID",
        help="source chokepoint id (see chokepoints)",
    )
    branch_parser.add_argument(
        "--reason", required=True, metavar="TEXT", help="why this branch exists (nonblank)"
    )
    branch_parser.add_argument(
        "--param",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="parameter override added to the parent's effective parameters; repeatable",
    )
    branch_parser.add_argument(
        "--root",
        default=None,
        metavar="PATH",
        help="project root containing the source chokepoint (default: current directory)",
    )
    branch_parser.add_argument(
        "--json", action="store_true", help="print machine-readable JSON instead of text"
    )

    compare_parser = subparsers.add_parser(
        "compare",
        help="compare a source run with its child runs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Compares a chokepoint's source atom with every child pathway"
            " created from it. State is ready when every child has terminal"
            " evidence, partial when at least one child lacks evidence, and"
            " no_branch when there are no children. Returns exit code 2 if"
            " required records or logs fail verification."
        ),
    )
    compare_parser.add_argument(
        "--root",
        default=None,
        metavar="PATH",
        help="project root directory (default: current directory)",
    )
    compare_parser.add_argument(
        "--from",
        dest="source",
        default=None,
        metavar="CHOKEPOINT_ID",
        help="source chokepoint id (default: the newest branchable chokepoint)",
    )
    compare_parser.add_argument(
        "--json", action="store_true", help="print machine-readable JSON instead of text"
    )

    verify_parser = subparsers.add_parser(
        "verify",
        help="re-check every state record and every log referenced by a recorded run",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Validates record envelopes, checksums, references, and semantic"
            " consistency; verifies each referenced log's size and SHA-256;"
            " reports orphaned files under .ocura-oss/logs/. Exits 0 when intact"
            " and 2 with an explicit problem list otherwise."
        ),
    )
    verify_parser.add_argument(
        "--root",
        default=None,
        metavar="PATH",
        help="project root directory (default: current directory)",
    )
    verify_parser.add_argument(
        "--json", action="store_true", help="print a JSON report including the problems list"
    )

    demo_parser = subparsers.add_parser(
        "demo",
        help="run the complete workflow in a new retained directory",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Performs init, a baseline run, a named chokepoint, an explained"
            " branch, an explicit rerun, full verification, and a ready"
            " comparison. Creates and retains DESTINATION, which must not exist;"
            " on any failure the directory is kept and the failing step is named."
        ),
    )
    demo_parser.add_argument(
        "--root",
        required=True,
        metavar="PATH",
        help="destination directory, which must not exist",
    )
    demo_parser.add_argument(
        "--json", action="store_true", help="print a JSON report of steps and comparison"
    )

    return parser


def _dispatch(args: argparse.Namespace, command_tail: list[str]) -> int:
    name = args.command_name
    if name == "init":
        return _init(args)
    if name == "run":
        return _run(args, command_tail)
    if name == "pathways":
        return _pathways(args)
    if name == "chokepoints":
        return _chokepoints(args)
    if name == "branch":
        return _branch(args)
    if name == "compare":
        return _compare(args)
    if name == "verify":
        return _verify(args)
    if name == "demo":
        return _demo(args)
    raise StoreError(f"unknown command: {name}")


def _open_store(root_value: str | None, *, require_state: bool = True) -> store.Store:
    root = store.resolve_root(root_value)
    state = store.Store(root)
    if require_state:
        state.require()
    return state


def _collect_parameters(tokens: Sequence[str]) -> dict:
    parameters: dict[str, str] = {}
    for token in tokens:
        key, value = model.parse_parameter(token)
        if key in parameters:
            raise model.ValidationError(f"duplicate parameter declaration: {key}")
        parameters[key] = value
    return parameters


def _emit_json(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _init(args: argparse.Namespace) -> int:
    state = _open_store(args.root, require_state=False)
    den, pathway = state.initialize_state(name=args.name)
    print(f"den: {den.id}")
    print(f"default pathway: {pathway.id}")
    print(f"state: {state.state_dir}")
    return EXIT_OK


def _run(args: argparse.Namespace, command_tail: Sequence[str]) -> int:
    if not command_tail:
        raise model.ValidationError("run requires at least one command token after --")
    if args.command:
        raise model.ValidationError("unexpected tokens before --; put the command after --")
    state = _open_store(args.root)
    declared = _collect_parameters(args.param)
    pathway_id = args.pathway or state.load_den().default_pathway_id
    state.load_pathway(pathway_id)
    mirror = not args.quiet
    print(f"recording under {state.state_dir}")
    execution = runner.run_command(
        state,
        pathway_id=pathway_id,
        argv=list(command_tail),
        declared_parameters=declared,
        mirror=mirror,
    )
    atom = execution.atom
    print(f"atom: {atom.id}")
    print(f"chokepoint: {execution.chokepoint.id}")
    print(f"pathway: {atom.pathway_id}")
    print(f"outcome: {atom.outcome.value}")
    print(f"duration: {atom.duration_seconds:.6f}s")
    print(f"stdout log: {atom.stdout_log}")
    print(f"stderr log: {atom.stderr_log}")
    if atom.return_code is not None:
        print(f"return code: {atom.return_code}")
    if atom.launch_error_category is not None:
        if atom.launch_error_category == "interrupted":
            print("interrupted: partial output retained in the logs above")
        else:
            print(f"launch error: {atom.launch_error_category}")
    if atom.outcome is model.Outcome.LAUNCH_FAILED:
        return EXIT_LAUNCH_FAILED
    if atom.outcome in (model.Outcome.FAILED, model.Outcome.INTERRUPTED):
        return EXIT_COMMAND_FAILED
    return EXIT_OK


def _pathway_summaries(state: store.Store) -> list[model.PathwaySummary]:
    summaries = []
    for pathway in state.list_pathways():
        summaries.append(
            model.PathwaySummary(
                id=pathway.id,
                parent_pathway_id=pathway.parent_pathway_id,
                source_chokepoint_id=pathway.source_chokepoint_id,
                created_at=pathway.created_at,
                reason=pathway.reason,
                parameters=pathway.parameters,
                has_terminal_evidence=state.has_terminal_evidence(pathway.id),
            )
        )
    return summaries


def _pathways(args: argparse.Namespace) -> int:
    state = _open_store(args.root)
    summaries = _pathway_summaries(state)
    if args.json:
        _emit_json({"pathways": [item.to_dict() for item in summaries]})
        return EXIT_OK
    for item in summaries:
        parent = item.parent_pathway_id or "-"
        source = item.source_chokepoint_id or "-"
        evidence = "yes" if item.has_terminal_evidence else "no"
        parameter_text = ", ".join(
            f"{key}={value}" for key, value in sorted(item.parameters.items())
        )
        print(f"pathway {item.id}")
        print(f"  parent: {parent}")
        print(f"  source: {source}")
        print(f"  created: {item.created_at}")
        print(f"  reason: {item.reason}")
        print(f"  parameters: {parameter_text or '(none)'}")
        print(f"  terminal evidence: {evidence}")
    return EXIT_OK


def _chokepoints(args: argparse.Namespace) -> int:
    state = _open_store(args.root)
    summaries = [
        model.ChokepointSummary(
            id=item.id,
            pathway_id=item.pathway_id,
            atom_id=item.atom_id,
            outcome=item.outcome,
            created_at=item.created_at,
            branchable=item.branchable,
        )
        for item in state.list_chokepoints()
    ]
    if args.json:
        _emit_json({"chokepoints": [item.to_dict() for item in summaries]})
        return EXIT_OK
    for item in summaries:
        branchable = "yes" if item.branchable else "no"
        print(
            f"{item.created_at}  {item.id}  pathway={item.pathway_id}"
            f"  atom={item.atom_id}  outcome={item.outcome.value}"
            f"  branchable={branchable}"
        )
    return EXIT_OK


def _branch(args: argparse.Namespace) -> int:
    overrides = _collect_parameters(args.param)
    state = _open_store(args.root)
    chokepoint, _atom, pathway = branching.load_verified_source(state, args.source)
    child = branching.create_child_pathway(
        state,
        source_chokepoint=chokepoint,
        source_pathway=pathway,
        reason=args.reason,
        overrides=overrides,
    )
    summary = model.PathwaySummary(
        id=child.id,
        parent_pathway_id=child.parent_pathway_id,
        source_chokepoint_id=child.source_chokepoint_id,
        created_at=child.created_at,
        reason=child.reason,
        parameters=child.parameters,
        has_terminal_evidence=False,
    )
    if args.json:
        _emit_json(summary.to_dict())
        return EXIT_OK
    print(f"pathway: {child.id}")
    print(f"parent: {child.parent_pathway_id}")
    print(f"source chokepoint: {child.source_chokepoint_id}")
    return EXIT_OK


def _compare(args: argparse.Namespace) -> int:
    state = _open_store(args.root)
    result = branching.compare(state, args.source)
    if args.json:
        _emit_json(result.to_dict())
        return EXIT_OK
    print(f"comparison: {result.state.value}")
    print(f"source chokepoint: {result.source_chokepoint_id}")
    print(f"source pathway: {result.source_pathway_id}")
    print(f"source run: {_run_phrase(result.source_run)}")
    for child in result.children:
        print(f"child pathway: {child.pathway_id}")
        print(f"  reason: {child.reason}")
        print(f"  parameters: {child.parameters.phrase()}")
        print(f"  source run: {_run_phrase(child.source_run)}")
        print(f"  child run: {_run_phrase(child.child_run)}")
        if child.run_parameters is not None:
            print(f"  run parameters: {child.run_parameters.phrase()}")
    return EXIT_OK


def _run_phrase(summary: model.RunSummary | None) -> str:
    if summary is None:
        return "missing evidence"
    return f"{summary.outcome.value} {summary.duration_seconds:.6f}s"


def _verify(args: argparse.Namespace) -> int:
    state = _open_store(args.root)
    report = state.verify_state()
    den = state.load_den()
    status = "ok" if report.ok else "failed"
    if args.json:
        _emit_json(
            {
                "den": den.id,
                "status": status,
                "counts": {
                    "pathways": report.pathways,
                    "atoms": report.atoms,
                    "chokepoints": report.chokepoints,
                    "logs_checked": report.logs_checked,
                },
                "problems": [
                    {"record": record, "problem": problem} for record, problem in report.problems
                ],
            }
        )
        return EXIT_OK if report.ok else EXIT_INVALID
    print(f"den: {den.id}")
    print(
        f"verified: {report.pathways} pathways, {report.atoms} atoms,"
        f" {report.chokepoints} chokepoints, {report.logs_checked} logs"
    )
    for record, problem in report.problems:
        print(f"problem: {record}: {problem}")
    print(f"integrity: {status}")
    return EXIT_OK if report.ok else EXIT_INVALID


def _demo(args: argparse.Namespace) -> int:
    report = demo.run_demo(Path(args.root))
    if args.json:
        _emit_json(report.to_dict())
        return EXIT_OK
    print(f"demo completed: {report.root}")
    for step in report.steps:
        print(f"  {step.step}: {step.detail}")
    print(f"comparison: {report.comparison.state.value}")
    print("retained state under .ocura-oss/")
    return EXIT_OK
