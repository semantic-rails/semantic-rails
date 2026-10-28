"""Say which groups a count or sum reads 0 in for want of rows of its own.

``ZERO_FILLED_GROUPS`` (a note). Two selected measures that the call's ``group_by`` joins answer
a row for every group either one's rows hold, so a group can hold rows of one and none of the
other: grouped by customer, signups beside orders answer a customer who never ordered, whose
order count reads 0 (data of nothing). The statement marks such groups itself, in hidden
columns (``guard_empty_groups``' ``absent``, named in the compiled ``absent_rows``), so this
reads the returned rows and runs no other query.
"""

from __future__ import annotations

import json
from collections.abc import Collection
from typing import Any

from ..diagnostics import semantic_issue
from ..query_call import TOTAL_OVER
from ..schema import PackageConfig


def zero_filled_groups_notes(
    config: PackageConfig,
    compiled: dict[str, Any],
    rows: list[dict[str, Any]],
    hidden: dict[str, list[Any]],
    *,
    excluded_outputs: Collection[str] = (),
) -> list[dict[str, Any]]:
    """ZERO_FILLED_GROUPS: the call's groups whose count or sum reads 0 because none of its
    measure's own rows reach them, while another measure's rows hold the group (two leaves
    joined on the call's keys, ``absent_rows``). A count of the returned rows' hidden marks
    (``guard_empty_groups``): nothing else is read. A group that read rows, even ones summing
    to 0, and one that reads NULL (no data) are not counted, nor are total rows. A truncated
    answer counts the rows shown, as a lower bound. Outputs 0 in the same groups share one
    sentence, and its ``having`` condition names the first of them."""
    marked = {
        output: names
        for output, names in dict(compiled.get("absent_rows") or {}).items()
        if output not in excluded_outputs and all(name in hidden for name in names)
    }
    own = [index for index, row in enumerate(rows) if row.get(TOTAL_OVER) is None]
    if not marked or not own:
        return []
    measures = {item["output"]: item["measures"] for item in compiled.get("zero_outputs") or []}
    # The entity a measure counts rows of, as the caller sees it (else the measure itself).
    visible = {row.id for row in config.entities}
    entity_of = {row.id: row.entity if row.entity in visible else row.id for row in config.measures}
    # Each output's zero-filled rows, and the outputs that read 0 in the same ones.
    alike: dict[tuple[int, ...], list[str]] = {}
    for output, names in marked.items():
        filled = tuple(
            index
            for index in own
            if rows[index].get(output) is not None
            and all(_marks(hidden[name][index]) for name in names)
        )
        if filled:
            alike.setdefault(filled, []).append(output)
    if not alike:
        return []
    truncated = bool(getattr(rows, "truncated", False))
    outputs: list[dict[str, Any]] = []
    sentences: list[str] = []
    for filled, names in alike.items():
        ids = sorted({measure for output in names for measure in measures.get(output, [])})
        entities = sorted({entity_of[measure] for measure in ids if measure in entity_of})
        one, count = len(filled) == 1, len(filled)
        counted = (
            f"at least {count} group{'' if one else 's'} ({count} of the {len(own)} rows shown)"
            if truncated
            else f"{count} of {len(own)} groups"
        )
        sentences.append(
            f"{counted} {'has' if one else 'have'} no rows of {' or '.join(entities)} in this "
            f"call, so "
            f"{'its' if one else 'their'} {_listed(names)} {'is' if len(names) == 1 else 'are'}"
            f" 0 (to leave {'it' if one else 'them'} out, add "
            f"{json.dumps({'field': names[0], 'op': 'not', 'value': 0})} to having)"
        )
        outputs.extend(
            {
                "output": output,
                "zero_filled": count,
                "entities": sorted(
                    {entity_of[m] for m in measures.get(output, []) if m in entity_of}
                ),
                "having": {"field": output, "op": "not", "value": 0},
            }
            for output in names
        )
    message = "; ".join(sentences)
    return [
        semantic_issue(
            code="ZERO_FILLED_GROUPS",
            message=f"{message[0].upper()}{message[1:]}. A 0 there is data of nothing, not "
            "missing data.",
            severity="info",
            stage="execution",
            details={
                "outputs": outputs,
                "groups": len(own),
                "truncated": truncated,
                "agent_action": "mention_when_interpreting_results",
            },
            object_ids=sorted(
                {measure for item in outputs for measure in measures.get(item["output"], [])}
            ),
        )
    ]


def _listed(names: list[str]) -> str:
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def _marks(value: Any) -> bool:
    """Whether a hidden mark reads 1, as any warehouse returns it (an int, a decimal, text)."""
    try:
        return value is not None and int(value) == 1
    except (TypeError, ValueError):
        return False
