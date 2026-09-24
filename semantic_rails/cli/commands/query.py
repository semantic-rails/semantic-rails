"""JSON runtime commands: catalog, discover, inspect, validate, compile,
query, segments, valid-values, build-options, plan, packages and serve.
"""

from __future__ import annotations

import argparse

from ...api import serve
from ...catalog_service import resolve_catalog
from ...config import list_package_ids
from ...metadata import (
    build_options_payload,
    discover_payload,
    inspect_payload,
    valid_values_payload,
)
from ...planner import plan_payload
from ..common import (
    _package_ref_from_args,
    _parse_json,
    _policy_context_from_args,
    _print,
    _query_payload_from_args,
    _query_with_policy_context,
    _runtime_from_package_or_path,
)


def cmd_packages(_: argparse.Namespace) -> None:
    _print({"packages": list_package_ids()})


def cmd_catalog(args: argparse.Namespace) -> None:
    runtime = _runtime_from_package_or_path(args)
    try:
        _print(
            {
                "catalog": resolve_catalog(
                    runtime,
                    view=args.view,
                    verbosity=args.verbosity,
                    kind=args.kind,
                    search=args.search,
                    entity=args.entity,
                    policy_context=_policy_context_from_args(args),
                )
            }
        )
    finally:
        runtime.close()


def cmd_discover(args: argparse.Namespace) -> None:
    runtime = _runtime_from_package_or_path(args)
    try:
        query = _query_with_policy_context(
            _parse_json(args.query_json) if args.query_json else None, args
        )
        _print(
            discover_payload(
                runtime,
                terms=args.terms,
                kinds=[part for part in args.kinds.split(",") if part],
                partial_query=query,
                stage=args.stage,
                verbosity=args.verbosity,
                limit=args.limit,
                enforce_scope=True,
            )
        )
    finally:
        runtime.close()


def cmd_inspect(args: argparse.Namespace) -> None:
    runtime = _runtime_from_package_or_path(args)
    try:
        query = _query_with_policy_context(
            _parse_json(args.query_json) if args.query_json else None, args
        )
        _print(
            inspect_payload(
                runtime, object_id=args.object_id, partial_query=query, verbosity=args.verbosity
            )
        )
    finally:
        runtime.close()


def cmd_validate(args: argparse.Namespace) -> None:
    runtime = _runtime_from_package_or_path(args)
    try:
        _print(runtime.validate(_query_payload_from_args(args)))
    finally:
        runtime.close()


def cmd_compile(args: argparse.Namespace) -> None:
    runtime = _runtime_from_package_or_path(args)
    try:
        _print(runtime.compile(_query_payload_from_args(args)))
    finally:
        runtime.close()


def cmd_query(args: argparse.Namespace) -> None:
    runtime = _runtime_from_package_or_path(args)
    try:
        _print(runtime.query(_query_payload_from_args(args)))
    finally:
        runtime.close()


def cmd_segment_validate(args: argparse.Namespace) -> None:
    runtime = _runtime_from_package_or_path(args)
    try:
        _print(
            runtime.segment_validate(
                args.segment_id,
                policy_context=_policy_context_from_args(args),
            )
        )
    finally:
        runtime.close()


def cmd_segment_explain(args: argparse.Namespace) -> None:
    runtime = _runtime_from_package_or_path(args)
    try:
        _print(
            runtime.segment_explain(
                args.segment_id,
                policy_context=_policy_context_from_args(args),
            )
        )
    finally:
        runtime.close()


def cmd_segment_preview(args: argparse.Namespace) -> None:
    runtime = _runtime_from_package_or_path(args)
    try:
        _print(
            runtime.segment_preview(
                args.segment_id,
                limit=args.limit,
                policy_context=_policy_context_from_args(args),
            )
        )
    finally:
        runtime.close()


def cmd_serve(args: argparse.Namespace) -> None:
    ref = _package_ref_from_args(args)
    serve(
        ref.package_id,
        host=args.host,
        port=args.port,
        path=ref.source_path if not ref.package_id else "",
    )


def cmd_valid_values(args: argparse.Namespace) -> None:
    runtime = _runtime_from_package_or_path(args)
    try:
        query = _query_with_policy_context(
            _parse_json(args.query_json) if args.query_json else None, args
        )
        _print(
            valid_values_payload(
                runtime,
                dimension_id=args.dimension,
                query=query,
                search=args.search,
                limit=args.limit,
                offset=args.offset,
                include_counts=args.include_counts,
            )
        )
    finally:
        runtime.close()


def cmd_build_options(args: argparse.Namespace) -> None:
    runtime = _runtime_from_package_or_path(args)
    try:
        query = _query_with_policy_context(_parse_json(args.query_json), args) or {}
        _print(
            build_options_payload(
                runtime,
                partial_query=query,
                focus_terms=args.focus_terms,
                focus_object_id=args.focus_object_id,
                step=args.step,
                stage=args.stage,
                verbosity=args.verbosity,
                include_blocked=args.include_blocked,
                limit=args.limit,
            )
        )
    finally:
        runtime.close()


def cmd_plan(args: argparse.Namespace) -> None:
    runtime = _runtime_from_package_or_path(args)
    try:
        query = _query_with_policy_context(
            _parse_json(args.query_json) if args.query_json else None, args
        )
        _print(
            plan_payload(
                runtime,
                intent=args.intent,
                partial_query=query,
                limit=args.limit,
                detail=args.detail,
            )
        )
    finally:
        runtime.close()
