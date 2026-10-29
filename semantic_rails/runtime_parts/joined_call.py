"""A joined call answered: its parts bound and compiled as requests, one statement run once.

Each part (``query_call.read_joined_call``) is bound as the request it would be alone, on the
caller's view, under the request's identity, row filters and policies, and lowered as a part
(``compiler_parts.part_domain``); ``compiler_parts.joined_parts`` checks the join and writes
the one statement. The rows it returns are split back into each part's rows, and each part is
answered from its own rows exactly as the part alone is answered (``Runtime._answer``): its
warnings, assumptions, window, routes and settled values. The joined answer carries them all,
each tagged with its part, and ``PARTS_JOINED`` says how the parts were joined and how many
rows of each found no row in the others, read from the statement's own rows only.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, replace
from typing import Any

from ..answer import (
    _policy_context,
    _time_zone,
    _withheld_columns,
    answered,
    finish,
    minimal_route,
)
from ..ast import PERIOD_PARTIAL, TIME_AXIS
from ..compiler import _prepare, compile_query
from ..compiler_parts.joined_parts import (
    IN,
    KEPT_ROWS,
    NULL_KEYS,
    ROWS,
    UNMATCHED,
    JoinKey,
    JoinShape,
    PartStatement,
    hidden_name,
    join_keys,
    joined_statement,
    presence_order,
    refuse_shared_outputs,
)
from ..compiler_parts.part_domain import PRESENT, reading_part_domain
from ..db_parts.base import QueryRows
from ..errors import SemanticLayerError
from ..policies import enforce_query_policies
from ..query_call import in_part, refusal
from ..request_context import policy_identity
from ..top_n import limit_order
from .assumptions import hidden_columns, refuse_hidden_governors, split_hidden
from .responses import (
    _ANSWER_KEYS,
    apply_response_verbosity,
    output_columns,
    resolve_sql_profile,
    resolve_verbosity,
)

# The options of the request every part is bound with.
_OPTIONS = ("sql_profile", "verbosity", "request_id", "debug")
# The response keys of the first part's answer the joined answer states once.
_SHARED_METADATA = (
    "semantic_fingerprint",
    "source_fingerprint",
    "sql_profile",
    "warehouse",
    "dialect",
    "warehouse_capabilities",
)
# What each part's block states beside its answer keys, when present.
_PART_FIELDS = (
    "policy_effects",
    "provenance_summary",
    "freshness_as_of",
    "methodology_hints",
    "data_diagnostics",
)
_AGGREGATED = (
    "Each part was aggregated to its own groups before the join, so no row counts twice within "
    "a part."
)
# Said instead when a part's groups overlap, naming those parts.
_AGGREGATED_OVERLAPPING = (
    "Each part was aggregated to its own groups before the join, so no row counts twice within "
    "a group of a part; a row of {parts} counts in every group it has a row in, as its "
    "REWRITE_APPLIED note says, so those groups overlap."
)
_THE_CALLERS = (
    "It does not check that the joined columns mean the same thing in both models; the join is "
    "the caller's."
)


@dataclass(frozen=True)
class _Part:
    index: int
    payload: dict[str, Any]
    policy_effects: list[dict[str, Any]]
    compiled: dict[str, Any]
    statement: PartStatement


@dataclass(frozen=True)
class _Joined:
    parts: list[_Part]
    shape: JoinShape
    keep: list[int] | None
    compiled: dict[str, Any]
    zone: str

    @property
    def keys(self) -> list[JoinKey]:
        return self.shape.keys


def joined_check(
    runtime: Any, payload: dict[str, Any], *, kind: str, started: float | None = None
) -> dict[str, Any]:
    """``validate`` (``kind``) or ``compile`` a joined call: every part bound and compiled, the
    join checked and the statement written, nothing run."""
    from .answer_warnings import _compiled_warnings

    verbosity = resolve_verbosity(payload)
    joined = _compile(runtime, payload)
    policy_context = _policy_context(payload)
    blocks: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    assumptions: list[dict[str, Any]] = []
    for part in joined.parts:
        block = runtime._answer_block(
            part.compiled, part.payload, part.policy_effects, policy_context
        )
        warnings.extend(
            _tagged(row, part.index)
            for row in _compiled_warnings(runtime._config, part.compiled, part.payload)
        )
        assumptions.extend(_tagged_assumption(row, part.index) for row in block["assumptions"])
        blocks.append(_part_block(part, block, verbosity))
    out = answered(
        {
            "warnings": [_parts_joined(runtime._config, joined, blocks, counts=None), *warnings],
            "assumptions": assumptions,
            "parts": blocks,
        }
    )
    if kind == "compile":
        out["rendered_sql"] = joined.compiled["sql"]
    out["explain"] = _explain(joined)
    if started is not None:
        out["timing_ms"] = round((time.perf_counter() - started) * 1000, 3)
    return apply_response_verbosity(
        _finished(out, payload),
        verbosity=verbosity,
        sql_profile=resolve_sql_profile(payload),
        kind=kind,
    )


def joined_output_columns(
    runtime: Any, payload: dict[str, Any]
) -> tuple[list[dict[str, Any]], str]:
    """A joined call's output columns as ``output_columns`` states a call's (each part's, once
    per field) and its clock's zone: what a package snapshot test encodes its rows by."""
    joined = _compile(runtime, payload)
    columns: dict[str, dict[str, Any]] = {}
    for part in joined.parts:
        for column in output_columns(runtime._config, part.compiled):
            columns.setdefault(column["field"], column)
    return list(columns.values()), joined.zone


def joined_query(runtime: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """Answer a joined call: the statement run once, each part answered from its own rows."""
    verbosity = resolve_verbosity(payload)
    policy_context = _policy_context(payload)
    joined = _compile(runtime, payload)
    limits = runtime._request_limits(payload, joined.zone)
    rows, _, limits_warnings, tie_warnings = runtime._run_statement(
        joined.compiled, payload, policy_context, limits, totaled=False
    )
    truncated = bool(getattr(rows, "truncated", False))
    counts = _counts(joined, rows)
    returned = {key.casefold(): key for key in rows[0]} if rows else {}

    def column(name: str) -> str:
        return returned.get(name.casefold(), name)

    answers: list[dict[str, Any]] = []
    where: list[list[int]] = []  # each part's row index in each joined row, or -1
    for part in joined.parts:
        own: list[dict[str, Any]] = []
        at: list[int] = []
        seen: dict[tuple[Any, ...], int] = {}
        present = column(IN.format(part=part.index))
        for row in rows:
            if row.get(present) is None:
                at.append(-1)  # a row this part has none of (a key it lacks, a NULL-key row)
                continue
            part_row = _part_row(row, part, joined.keys, column)
            # A coarser part's row repeats on each finer row of its key, an untimed part's on
            # each period's: the part answers from each of its rows once.
            identity = tuple(part_row.get(name) for name in part.statement.keys)
            if identity not in seen:
                seen[identity] = len(own)
                own.append(part_row)
            at.append(seen[identity])
        part_rows, hidden = split_hidden(QueryRows(own, truncated=truncated), part.compiled)
        answers.append(
            runtime._answer(
                part.compiled,
                part.payload,
                part_rows,
                hidden,
                policy_effects=part.policy_effects,
                policy_context=policy_context,
                limits=limits,
                call_warnings=[],
                verbosity=verbosity,
            )
        )
        where.append(at)
    out_rows, column_types = _joined_rows(joined, answers, where, len(rows))
    blocks = [
        _part_block(part, answer, verbosity)
        for part, answer in zip(joined.parts, answers, strict=True)
    ]
    out = answered(
        {
            "read_from": _read_from(answers),
            "rows": out_rows,
            "column_types": column_types,
            "row_count": len(out_rows),
            "truncated": truncated,
            "rendered_sql": joined.compiled["sql"],
            "warnings": [
                _parts_joined(runtime._config, joined, blocks, counts=counts, ran=True),
                *(
                    _tagged(row, part.index)
                    for part, answer in zip(joined.parts, answers, strict=True)
                    for row in answer["warnings"]
                ),
                *limits_warnings,
                *tie_warnings,
                *runtime._seed_warnings,
            ],
            "assumptions": [
                _tagged_assumption(row, part.index)
                for part, answer in zip(joined.parts, answers, strict=True)
                for row in answer.get("assumptions") or []
            ],
            "parts": blocks,
            "request_context": answers[0].get("request_context"),
            "explain": _explain(joined),
        }
    )
    withheld = sorted({name for answer in answers for name in answer.get("withheld") or []})
    if withheld:
        out["withheld"] = withheld
    if any("output_columns" in answer for answer in answers):
        out["output_columns"] = _output_columns(joined, answers)
    out.update(
        {key: answers[0][key] for key in _SHARED_METADATA if answers[0].get(key) is not None}
    )
    return apply_response_verbosity(
        _finished(out, payload),
        verbosity=verbosity,
        sql_profile=resolve_sql_profile(payload),
        kind="execute",
    )


def _compile(runtime: Any, payload: dict[str, Any]) -> _Joined:
    """Bind, check and compile every part, in order (the first refusal is the answer), then
    check the join and write the one statement."""
    config = runtime._config
    policy_context = _policy_context(payload)
    if policy_context.get("metric_allowlist") is not None:
        raise SemanticLayerError(
            "RESOURCE_ACCESS_DENIED", "A metric grant answers a call of its own, not parts."
        )
    options = {key: payload[key] for key in _OPTIONS if key in payload}
    notes = list(payload.get("_call_notes") or [])
    parts: list[_Part] = []
    for index, call in enumerate(payload["parts"]):
        own_notes = [
            {key: value for key, value in note.items() if key != "part"}
            for note in notes
            if note.get("part") == index
        ]
        part_payload = {
            **call,
            "policy_context": policy_context,
            **options,
            **({"_call_notes": own_notes} if own_notes else {}),
        }
        try:
            parts.append(_compile_part(runtime, index, part_payload, policy_context))
        except SemanticLayerError as exc:
            refused = runtime._refusal(exc, part_payload, policy_context)
            raise in_part(refused, index) from exc
    zones = {
        _time_zone(config, part.compiled) for part in parts if part.compiled["logical_plan"].time
    }
    if len(zones) > 1:
        raise refusal(
            "parts",
            f"the parts read clocks in different time zones ({', '.join(sorted(zones))}): one "
            "statement buckets every part in one zone",
            fix="Ask for each part in a call of its own.",
            reason="parts_time_zones_differ",
        )
    time_key = TIME_AXIS if payload.get("timed") else ""
    shape = join_keys(
        config,
        payload["join"]["on"],
        [list(part.payload.get("group_by") or []) for part in parts],
        [list(part.statement.keys) for part in parts],
        time_key,
    )
    refuse_shared_outputs(shape, [part.statement.outputs for part in parts])
    _check_order(payload.get("order_by") or [], shape, parts)
    keep = payload["join"]["keep"]
    keep = None if keep == "all" else list(keep)
    statement = joined_statement(
        [part.statement for part in parts],
        shape,
        keep=keep,
        order_by=payload.get("order_by") or [],
        limit=payload.get("limit"),
    )
    # A requested order under a limit is broken by the keys (the finer part's further keys
    # too), and a tie at the limit is stated, as a call's is; the parts' order breaks what is
    # left.
    own, order_keys = (
        limit_order(statement, [*(key.name for key in shape.keys), *shape.extra])
        if payload.get("order_by")
        else (statement, ())
    )
    own = replace(own, order_by=[*own.order_by, *presence_order(len(parts))])
    visibility = _visibility(parts)
    sql_profile = str(payload.get("sql_profile", "audit") or "audit")
    prepared = _prepare(own, config, sql_profile, visibility)
    probe = None
    if order_keys and own.limit is not None and own.limit > 0:
        probe = _prepare(replace(own, limit=own.limit + 1), config, sql_profile, visibility)
    compiled = {
        "sql_ast": own,
        "sql": prepared.sql,
        "prepared_query": prepared,
        "limit_probe": probe,
        "limit_order_keys": order_keys,
    }
    zone = next(iter(zones), "") or _time_zone(config, parts[0].compiled)
    return _Joined(parts=parts, shape=shape, keep=keep, compiled=compiled, zone=zone)


def _compile_part(
    runtime: Any, index: int, payload: dict[str, Any], policy_context: dict[str, Any]
) -> _Part:
    """One part, as ``Runtime._enforced_compile`` compiles a call (bound on the caller's view,
    the policy gate, then the compile), lowered as a part, never from the compile cache: the
    cache keys on the call alone, and a part's statement is another one."""
    from .answer_warnings import refuse_closing_day_zone

    config = runtime._config
    with reading_part_domain() as domain:
        binding = runtime._bind(payload, policy_context)
    policy_effects = enforce_query_policies(
        runtime.package_config,
        binding.object_ids,
        **policy_identity(policy_context),
        query=payload,
        binding=binding,
    )
    refuse_hidden_governors(config, binding.plan.query)
    compiled = compile_query(config, runtime.registry, payload, binding=binding)
    refuse_closing_day_zone(config, compiled)
    if not domain.taken:
        raise SemanticLayerError(
            "REWRITE_NOT_SUPPORTED",
            "this call can't be a part of a joined call yet",
            details={"reason": "part_not_joinable"},
        )
    keys = tuple(domain.keys)
    hidden = tuple(hidden_columns(compiled))
    outputs = tuple(
        field.alias
        for field in compiled["sql_ast"].select
        if field.alias not in keys and field.alias not in hidden and field.alias != PRESENT
    )
    return _Part(
        index=index,
        payload=payload,
        policy_effects=policy_effects,
        compiled=compiled,
        statement=PartStatement(compiled["sql_ast"], keys, outputs, hidden),
    )


def _visibility(parts: list[_Part]) -> Any:
    """The row filters the joined statement is held to: the request's, which every part was
    bound under, with every part's load logs and relation pipelines (named as the statement
    names its CTEs)."""
    seen = [part.compiled.get("row_filter_visibility") for part in parts]
    present = [row for row in seen if row is not None]
    if not present:
        return None
    if len(present) != len(seen) or any(row.filters != present[0].filters for row in present):
        raise SemanticLayerError(
            "REWRITE_NOT_SUPPORTED",
            "the parts of this call were bound under different row filters",
            details={"reason": "part_not_joinable"},
        )
    return replace(
        present[0],
        load_logs=frozenset().union(*(row.load_logs for row in present)),
        pipelines=frozenset(
            f"joined_part_{index}__{name}"
            for index, row in enumerate(present)
            for name in row.pipelines
        ),
    )


def _check_order(order_by: list[dict[str, Any]], shape: JoinShape, parts: list[_Part]) -> None:
    names = [
        *(key.name for key in shape.keys),
        *shape.extra,
        *(name for part in parts for name in part.statement.outputs),
    ]
    for index, item in enumerate(order_by):
        if item["field"] not in names:
            raise refusal(
                f"order_by[{index}].field",
                f"names '{item['field']}', which is no column of the joined answer",
                fix=f"Name one of {names}.",
                code="INVALID_ORDER_BY",
            )


def _part_row(row: dict[str, Any], part: _Part, keys: list[JoinKey], column: Any) -> dict[str, Any]:
    """A part's row of a joined row, under the part's own column names, in its own order."""
    by_column = {key.columns[part.index]: key.name for key in keys if key.columns[part.index]}
    out: dict[str, Any] = {}
    for field in part.statement.sql_ast.select:
        name = field.alias
        if name == PRESENT:
            continue
        if name in by_column:
            out[name] = row.get(column(by_column[name]))
        elif name in part.statement.hidden:
            out[name] = row.get(column(hidden_name(part.index, name)))
        else:
            out[name] = row.get(column(name))
    return out


def _joined_rows(
    joined: _Joined, answers: list[dict[str, Any]], where: list[list[int]], count: int
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """The answer's rows, rebuilt from each part's answered rows: each key column (from the
    first part with a row there that holds it), the finer part's further keys, then each
    part's outputs (NULL where it has none)."""
    rows: list[dict[str, Any]] = []
    finer = joined.shape.finer
    # Whether the period comes with its partial flag: then a row no timed part has (an untimed
    # part's key no timed part has) reads it NULL beside its NULL period.
    partial = any(PERIOD_PARTIAL in (answer.get("column_types") or {}) for answer in answers)
    for position in range(count):
        out: dict[str, Any] = {}
        for key in joined.keys:
            holding = [
                index
                for index in range(len(joined.parts))
                if key.columns[index] and where[index][position] >= 0
            ]
            source = answers[holding[0]]["rows"][where[holding[0]][position]] if holding else {}
            out[key.name] = source.get(key.columns[holding[0]]) if holding else None
            if key.name == TIME_AXIS and (PERIOD_PARTIAL in source or partial):
                out[PERIOD_PARTIAL] = source.get(PERIOD_PARTIAL)
        if finer is not None:
            at = where[finer][position]
            source = answers[finer]["rows"][at] if at >= 0 else {}
            out.update({name: source.get(name) for name in joined.shape.extra})
        for part, answer, at in zip(joined.parts, answers, where, strict=True):
            row = answer["rows"][at[position]] if at[position] >= 0 else {}
            for name in _visible_outputs(part):
                out[name] = row.get(name)
        rows.append(out)
    types: dict[str, Any] = {}
    for key in joined.keys:
        for part, answer in zip(joined.parts, answers, strict=True):
            if not key.columns[part.index]:
                continue
            kind = (answer.get("column_types") or {}).get(key.columns[part.index])
            if kind is not None:
                types[key.name] = kind
                break
        if key.name == TIME_AXIS:
            kind = next(
                (
                    answer["column_types"][PERIOD_PARTIAL]
                    for answer in answers
                    if PERIOD_PARTIAL in (answer.get("column_types") or {})
                ),
                None,
            )
            if kind is not None:
                types[PERIOD_PARTIAL] = kind
    if finer is not None:
        for name in joined.shape.extra:
            kind = (answers[finer].get("column_types") or {}).get(name)
            if kind is not None:
                types[name] = kind
    for part, answer in zip(joined.parts, answers, strict=True):
        for name in _visible_outputs(part):
            kind = (answer.get("column_types") or {}).get(name)
            if kind is not None:
                types[name] = kind
    return rows, types


def _visible_outputs(part: _Part) -> list[str]:
    withheld = _withheld_columns(part.policy_effects)
    return [name for name in part.statement.outputs if name not in withheld]


def _counts(joined: _Joined, rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The whole result's counts, read from the statement's own columns (every row carries
    them); None when no row came back."""
    if not rows:
        return None
    first = {key.casefold(): value for key, value in rows[0].items()}

    def read(name: str) -> int:
        return int(first.get(name.casefold()) or 0)

    parts = []
    for part in joined.parts:
        others = [other.index for other in joined.parts if other.index != part.index]
        parts.append(
            {
                "part": part.index,
                "rows": read(ROWS.format(part=part.index)),
                "null_keys": read(NULL_KEYS.format(part=part.index)),
                "unmatched": {
                    str(other): read(UNMATCHED.format(part=part.index, other=other))
                    for other in others
                },
            }
        )
    return {"parts": parts, "kept": read(KEPT_ROWS)}


def _overlapping(part: _Part) -> set[str]:
    """The measures whose groups overlap in ``part``: a row counts, whole, in every group it
    has a row in (``overlapping_groups``, the fact the part's own REWRITE_APPLIED states)."""
    return {
        str(step.measure_id)
        for step in part.compiled["logical_plan"].rewrite_steps
        if (getattr(step, "details", None) or {}).get("overlapping_groups")
    }


def _adds_up(config: Any, part: _Part) -> dict[str, bool]:
    """Per output of a part (at the join key), whether it adds up down the table: a count, or a
    sum of an additive measure, read as itself, in groups that don't overlap. A distinct count,
    an average, a ratio, a metric or a measure whose groups overlap never does."""
    measures = {row.id: row for row in config.measures}
    overlapping = _overlapping(part)
    out: dict[str, bool] = {}
    for item in part.compiled["logical_plan"].query.get("select") or []:
        alias = str(item.get("as") or "")
        expression = dict(item.get("expression") or {})
        measure = measures.get(str(expression.get("measure") or ""))
        aggregation = str(
            expression.get("aggregation") or getattr(measure, "default_aggregation", "")
        ).lower()
        out[alias] = bool(
            expression.get("kind") == "measure"
            and measure is not None
            and getattr(measure, "additive", False)
            and getattr(measure, "measure_class", "") == "additive"
            and aggregation in {"count", "sum"}
            and measure.id not in overlapping
        )
    return out


def _parts_joined(
    config: Any,
    joined: _Joined,
    blocks: list[dict[str, Any]],
    *,
    counts: dict[str, Any] | None,
    ran: bool = False,
) -> dict[str, Any]:
    """``PARTS_JOINED``: the key columns, what each is in each part and whether the package
    says they hold the same values (``declared``; else the join is the caller's choice),
    ``keep``, each part's window, the whole result's counts (once it ran) and which columns
    add up."""
    from ..diagnostics import semantic_issue

    keys = [
        {"column": key.name, "parts": list(key.columns), "declared": not key.chosen}
        for key in joined.keys
    ]
    parts = [
        {"part": part.index, **({"window": block["window"]} if block.get("window") else {})}
        for part, block in zip(joined.parts, blocks, strict=True)
    ]
    details: dict[str, Any] = {
        "keys": keys,
        "keep": "all" if joined.keep is None else list(joined.keep),
        "parts": parts,
    }
    on = ", ".join(
        key.name
        if len(set(key.columns)) == 1
        else f"{key.name} ({', '.join(f'parts[{i}]: {c}' for i, c in enumerate(key.columns))})"
        for key in joined.keys
    )
    if len(joined.parts) == 1:
        sentences = [f"The answer is its one part, keyed by {on}."]
    else:
        kept = (
            "keeping every key of every part"
            if joined.keep is None
            else "keeping the keys "
            + _listed([f"parts[{index}]" for index in joined.keep])
            + (" has" if len(joined.keep) == 1 else " all have")
        )
        sentences = [f"The answer joins {len(joined.parts)} parts on {on}, {kept}."]
        sentences.extend(
            f"No relationship in the package pairs {key.dimensions[0]} with "
            f"{_listed(list(key.chosen))}: joining on them is the caller's choice."
            for key in joined.keys
            if key.chosen
        )
    if counts is not None:
        details["kept_rows"] = counts["kept"]
        for row, stated in zip(counts["parts"], parts, strict=True):
            stated.update({key: value for key, value in row.items() if key != "part"})
            clauses = [
                f"{value} with no row in parts[{other}]"
                for other, value in row["unmatched"].items()
            ]
            clauses.append(
                f"{row['null_keys']} with a NULL key"
                + (" (its own row, NULL in every other part)" if len(parts) > 1 else "")
            )
            sentences.append(f"parts[{row['part']}] has {row['rows']} rows: {_listed(clauses)}.")
    elif ran:
        sentences.append("No row came back, so no counts are stated.")
    columns: list[dict[str, Any]] = []
    for part in joined.parts:
        adds = _adds_up(config, part)
        columns.extend(
            {"column": name, "part": part.index, "adds_up": adds.get(name, False)}
            for name in _visible_outputs(part)
        )
    details["columns"] = columns
    summed = [row["column"] for row in columns if row["adds_up"]]
    others = [row["column"] for row in columns if not row["adds_up"]]
    if summed:
        sentences.append(
            f"Summed down the rows, {_listed(summed)} add{'s' if len(summed) == 1 else ''} up."
        )
    overlap = [f"parts[{part.index}]" for part in joined.parts if _overlapping(part)]
    if others:
        sentences.append(
            f"{_listed(others)} {'is' if len(others) == 1 else 'are'} not stated to add up: a "
            + (
                "distinct count, an average, a ratio or a part's overlapping groups"
                if overlap
                else "distinct count, an average or a ratio"
            )
            + " can count a member of two rows twice."
        )
    if len(joined.parts) > 1:
        sentences.append(
            _AGGREGATED_OVERLAPPING.format(parts=_listed(overlap)) if overlap else _AGGREGATED
        )
        sentences.append(_THE_CALLERS)
    return semantic_issue(
        code="PARTS_JOINED",
        message=" ".join(sentences),
        severity="info",
        stage="compile",
        details=details,
    )


def _listed(items: list[str]) -> str:
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


# The having a ZERO_FILLED_GROUPS note offers: a part has no having (``read_joined_call``).
_HAVING_HINT = re.compile(r" \(to leave (?:it|them) out, add \{[^}]*\} to having\)")


def _tagged(row: dict[str, Any], index: int) -> dict[str, Any]:
    """A part's warning in the joined answer: said of its part (``details.part``). A zero-filled
    group's ``having`` is no fix a part takes, so it goes."""
    out = dict(row)
    details = dict(row.get("details") or {})
    message = str(row.get("message", ""))
    if row.get("code") == "ZERO_FILLED_GROUPS":
        message = _HAVING_HINT.sub("", message)
        details["outputs"] = [
            {key: value for key, value in item.items() if key != "having"}
            for item in details.get("outputs") or []
        ]
    out["message"] = f"parts[{index}]: {message}"
    out["details"] = {**details, "part": index}
    if isinstance(out.get("path"), str) and out["path"] and not out["path"].startswith("time"):
        out["path"] = f"parts[{index}].{out['path']}"
    return out


def _tagged_assumption(row: dict[str, Any], index: int) -> dict[str, Any]:
    return {**row, "message": f"parts[{index}]: {row.get('message', '')}", "part": index}


def _part_block(part: _Part, answer: dict[str, Any], verbosity: str) -> dict[str, Any]:
    """What a part's answer means, beside the joined rows: its call, window, clock, routes and
    models (every verbosity), then its policy effects and provenance."""
    from ..answer import _call

    block: dict[str, Any] = {"part": part.index, "query": _call(part.payload)}
    if isinstance(block["query"], dict):
        block["query"].pop("time", None)  # the joined call's one time block
    keys = [key for key in _ANSWER_KEYS if key != "assumptions"]
    if verbosity != "minimal":
        keys.extend(_PART_FIELDS)
    for key in keys:
        value = answer.get(key)
        if value in (None, "", [], {}):
            continue
        if key == "routes" and verbosity == "minimal":
            value = [minimal_route(route) for route in value]
        block[key] = value
    return block


def _read_from(answers: list[dict[str, Any]]) -> dict[str, list[str]]:
    out: dict[str, dict[str, None]] = {}
    for answer in answers:
        for entity, ids in (answer.get("read_from") or {}).items():
            out.setdefault(entity, {}).update(dict.fromkeys(ids))
    return {entity: list(ids) for entity, ids in out.items()}


def _output_columns(joined: _Joined, answers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The answer's columns: the key columns as the first part with each names them, then each
    part's outputs (``part``)."""
    columns: list[dict[str, Any]] = []
    by_field = [
        {row["field"]: row for row in answer.get("output_columns") or []} for answer in answers
    ]
    for key in joined.keys:
        for part in joined.parts:
            row = by_field[part.index].get(key.columns[part.index])
            if key.columns[part.index] and row is not None:
                columns.append({**row, "field": key.name, "sql_alias": key.name})
                break
        if key.name == TIME_AXIS:
            row = next(
                (fields[PERIOD_PARTIAL] for fields in by_field if PERIOD_PARTIAL in fields), None
            )
            if row is not None:
                columns.append(row)
    if joined.shape.finer is not None:
        for name in joined.shape.extra:
            row = by_field[joined.shape.finer].get(name)
            if row is not None:
                columns.append({**row, "part": joined.shape.finer})
    for part in joined.parts:
        for name in _visible_outputs(part):
            row = by_field[part.index].get(name)
            if row is not None:
                columns.append({**row, "part": part.index})
    return columns


def _explain(joined: _Joined) -> dict[str, Any]:
    return {
        "parts": [
            {"part": part.index, "chosen_paths": part.compiled["explain"].chosen_paths}
            for part in joined.parts
        ],
        "join": {
            "on": [list(key.columns) for key in joined.keys],
            "keep": "all" if joined.keep is None else list(joined.keep),
        },
    }


def _finished(out: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    return finish(out, payload=payload)
