"""A route refusal's options grouped by what they mean, for the answers people and agents read.

An ``AMBIGUOUS_PATH`` refusal (``fanout.route_clarification``) lists one option per route. Inside
the engine that flat list is the pair's candidate routes, and every reader of it (row-filter
coverage, a view's routes, the binding guard, the census walks, the offered rows) keeps reading
it unchanged. Only the edges that show the refusal (the runtime's answer, MCP, ``plan``, the
inspect card and Architect's census) add ``groups``, computed here from those same routes, so
which route a query takes, and every policy result, never depends on how the routes are shown.

Routes are grouped at their first divergence: the hops every route shares are set aside (never
a whole route), and the routes that take the same next hop form a group, named by the entity
that hop reaches (``via``). In a group, routes rank by their one-to-many hops, then their hops,
then their ids, as the route ladder prefers a lookup (``fanout.resolve_path`` rung 5); a group
then shows:

* one route: that option;
* two or more lookups from ``via`` to the target (two roles, such as a billing building and a
  serviced room's building): each lookup as an option, never one of them as the group's meaning;
* else one route ranked first: that option, with the rest counted in ``more_routes``;
* else at most ``EXPAND_MAX`` routes: each as an option;
* else a fork: no option to send, and ``decide_first`` names the part a package author decides.

Grouping chooses nothing: an option is a route with its own row, and the person still says
which one the question means. Groups list options before forks, then by one-to-many hops, hops
and id, and ``ordered`` says that this order is not a preference.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from copy import deepcopy
from typing import Any

from .compiler_parts.indexes import get_package_analysis
from .config_parts.route_rows import walk_entities
from .errors import SemanticLayerError
from .fanout import _entity_keys, _plural, entity_label, route_hops
from .query_call import GROUPS_HINT
from .schema import PackageConfig

# A group with no first route and at most this many routes lists each of them as an option.
EXPAND_MAX = 3
ORDERED = "by one-to-many hops, then hops, then id; not a preference"
# Product choice: a fork is counted and named, never offered at query time: an option is a
# whole route the person can choose, and a fork's routes are a package author's.
FORK_NOTE = "a package author can define which one"


def _many_hops(config: PackageConfig, start: str, path: Sequence[str]) -> int:
    return sum(reach == "many" for _rel, _far, reach in route_hops(config, start, path))


def _rank(config: PackageConfig, start: str, path: Sequence[str]) -> tuple[int, int, list[str]]:
    return _many_hops(config, start, path), len(path), list(path)


def _is_lookup(config: PackageConfig, start: str, path: Sequence[str]) -> bool:
    """Whether every hop of ``path`` from ``start`` reaches one row (rung 5's lookup)."""
    return bool(path) and all(reach != "many" for _r, _f, reach in route_hops(config, start, path))


def _shared_hops(paths: list[list[str]]) -> int:
    """How many leading hops every route shares, leaving at least one hop of each."""
    shared = 0
    while all(len(path) > shared + 1 for path in paths) and len({p[shared] for p in paths}) == 1:
        shared += 1
    return shared


def route_groups(
    config: PackageConfig, start: str, target: str, options: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """``options`` (a route refusal's, one per route) grouped at their first divergence: the
    counts (``options_total``, ``groups_total``, ``listed``: the options the groups list, each
    a route a call can send), the ``ordered`` note and the ``groups``. Each route is in exactly
    one group; a group names its listed options by ``id``."""
    paths = [list(option["relationship_path"]) for option in options]
    ids = {tuple(path): str(option["id"]) for path, option in zip(paths, options, strict=True)}
    relationships = get_package_analysis(config).relationships
    shared = _shared_hops(paths) if len(paths) > 1 else 0
    pivot = walk_entities(relationships, start, paths[0][:shared])[-1] if paths else start
    by_hop: dict[str, list[list[str]]] = {}
    for path in paths:
        by_hop.setdefault(path[shared], []).append(path)
    keys = _entity_keys(config)
    taken = set(ids.values())
    ranked: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    for hop, members in by_hop.items():
        via = walk_entities(relationships, pivot, [hop])[-1]
        members.sort(key=lambda path: _rank(config, start, path))
        first = _rank(config, start, members[0])[:2]
        lookups = [path for path in members if _is_lookup(config, via, path[shared + 1 :])]
        tied = [path for path in members if _rank(config, start, path)[:2] == first]
        if len(members) > 1 and len(lookups) >= 2:
            listed = lookups  # two roles: each is a meaning of its own
        elif len(tied) == 1:
            listed = members[:1]
        elif len(members) <= EXPAND_MAX:
            listed = members
        else:
            listed = []
        group: dict[str, Any] = {"id": ids[tuple(listed[0])] if len(listed) == 1 else ""}
        if not group["id"]:
            base = f"via_{keys.get(via) or 'route'}"
            group["id"], suffix = base, 1
            while group["id"] in taken:
                suffix += 1
                group["id"] = f"{base}_{suffix}"
        taken.add(group["id"])
        group.update(via=via, routes=len(members))
        if listed:
            group.update(
                more_routes=len(members) - len(listed), options=[ids[tuple(p)] for p in listed]
            )
        else:
            group["decide_first"] = {"source_entity": via, "target_entity": target}
        ranked.append(((not listed, *first, hop), group))
    groups = [group for _key, group in sorted(ranked, key=lambda row: row[0])]
    return {
        "options_total": len(paths),
        "groups_total": len(groups),
        "listed": sum(len(group.get("options") or ()) for group in groups),
        "ordered": ORDERED,
        "groups": groups,
    }


def grouped_message(
    config: PackageConfig, start: str, target: str, clarification: Mapping[str, Any]
) -> str:
    """The refusal's message over its groups: the question, then each group's listed meanings
    with the count of its other routes and the entity they pass through, or a fork's count."""
    meanings = {str(option["id"]): option["meaning"] for option in clarification["options"]}
    parts: list[str] = []
    for group in clarification["groups"]:
        through = _plural(entity_label(config, group["via"]))
        if "decide_first" in group:
            parts.append(f"{group['routes']} routes through {through} ({FORK_NOTE})")
            continue
        text = " or ".join(meanings[option_id] for option_id in group["options"])
        more = group["more_routes"]
        if more:
            text += f" (+{more} other route{'s' if more > 1 else ''} through {through})"
        parts.append(text)
    question = clarification["question"]
    return f"Ambiguous path from '{start}' to '{target}'. {question} " + "; ".join(parts)


def grouped_refusal(exc: SemanticLayerError, config: PackageConfig) -> SemanticLayerError:
    """A route refusal (``AMBIGUOUS_PATH``, ``route_decision_required``) as an answer shows it:
    its clarification with ``groups`` beside the flat options, and its message over the groups.
    Any other error, and a refusal already grouped, is returned as it is."""
    details = exc.details or {}
    clarification = details.get("clarification")
    if (
        exc.code != "AMBIGUOUS_PATH"
        or details.get("reason") != "route_decision_required"
        or not isinstance(clarification, dict)
        or "groups" in clarification
        or not clarification.get("options")
    ):
        return exc
    start, target = str(details["start"]), str(details["target"])
    grouped = {**clarification, **route_groups(config, start, target, clarification["options"])}
    hint = f"{details.get('hint') or ''} {GROUPS_HINT}".strip()
    return SemanticLayerError(
        exc.code,
        grouped_message(config, start, target, grouped),
        details={**details, "clarification": grouped, "hint": hint},
    )


# --- The answer's budget: a route question that doesn't fit is shortened, never refused. -----

REST_IN_ARCHITECT = (
    "Architect's project_status lists every route of the pair in its route census "
    "(route_census.undecided)."
)


def _asks(value: Any) -> Iterator[tuple[dict[str, Any], dict[str, Any], str]]:
    """Each route question in ``value`` (an answer): the issue (or plan's ``why``) holding its
    message, the dict holding its options and groups, and the key of each option's call. An
    ``AMBIGUOUS_PATH`` issue's grouped clarification has ``query`` calls, plan's route ``why``
    details ``call``."""
    if isinstance(value, list):
        for item in value:
            yield from _asks(item)
        return
    if not isinstance(value, dict):
        return
    details = value.get("details")
    if value.get("code") == "AMBIGUOUS_PATH" and isinstance(details, dict):
        clarification = details.get("clarification")
        if isinstance(clarification, dict) and "groups" in clarification:
            yield value, clarification, "query"
            return
        if "groups" in details:
            yield value, details, "call"
            return
    for item in value.values():
        yield from _asks(item)


def _listed(ask: dict[str, Any], groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    wanted = {option_id for group in groups for option_id in group.get("options") or ()}
    return [option for option in ask.get("options") or [] if option.get("id") in wanted]


def _as_template(ask: dict[str, Any], key: str) -> bool:
    """``ask``'s options as one ``call`` and each option's ``route`` (the entry to append to
    ``call.routes``), when every option's call is that call with its route added. The route's
    ``via`` is the option's ``relationship_path``, which is then not repeated."""
    options = ask.get("options") or []
    calls = [option.get(key) for option in options]
    if not calls or not all(isinstance(call, dict) and call.get("routes") for call in calls):
        return False

    def rest(call: dict[str, Any]) -> dict[str, Any]:
        return {**call, "routes": call["routes"][:-1]}

    if any(rest(call) != rest(calls[0]) for call in calls):
        return False
    ask["call"] = {k: v for k, v in rest(calls[0]).items() if k != "routes" or v}
    for option, call in zip(options, calls, strict=True):
        option.pop(key)
        option["route"] = call["routes"][-1]
        if option.get("relationship_path") == option["route"].get("via"):
            option.pop("relationship_path")
    return True


def _retext(value: Any, texts: Mapping[str, str]) -> None:
    """Each ``message`` or ``why_invalid`` in ``value`` that is a key of ``texts``, replaced by
    its value: an answer repeats an issue's message (``why_invalid``, the envelope's
    ``error``)."""
    if isinstance(value, list):
        for item in value:
            _retext(item, texts)
    elif isinstance(value, dict):
        for key, item in value.items():
            if key in ("message", "why_invalid") and isinstance(item, str) and item in texts:
                value[key] = texts[item]
            else:
                _retext(item, texts)


def _short_message(holder: dict[str, Any], ask: dict[str, Any], shown: int) -> str:
    """The question alone, and how many of its groups are listed (``shown``, when any)."""
    details = holder.get("details") or {}
    lead = (
        f"Ambiguous path from '{details['start']}' to '{details['target']}'. "
        if "start" in details and "target" in details
        else ""
    )
    question = ask.get("question") or (ask.get("clarification") or {}).get("question") or ""
    listed = f" {shown} of its {ask.get('groups_total')} groups are listed." if shown else ""
    return f"{lead}{question}{listed}"


def shorter_route_asks(payload: Mapping[str, Any]) -> Iterator[dict[str, Any]]:
    """``payload`` with its route questions shorter, step by step, for an answer over its
    budget: only the options the groups list; then one ``call`` and a ``route`` per option to
    append to its ``routes``; then the first groups, fewest last, under a message that only
    asks the question; then the question and its counts alone. Each step's ``note`` says what
    it left out. Nothing is yielded when ``payload`` has no grouped route question."""
    if not any(True for _ in _asks(payload)):
        return
    note = "Only the options the groups list are shown. " + REST_IN_ARCHITECT
    step = deepcopy(dict(payload))
    for _holder, ask, _key in _asks(step):
        ask["options"], ask["note"] = _listed(ask, ask["groups"]), note
    yield deepcopy(step)
    for _holder, ask, key in _asks(step):
        if _as_template(ask, key):
            ask["note"] = (
                f"{note} Send call with the option's route appended to call.routes; the route's "
                "via is the option's relationship_path."
            )
    yield deepcopy(step)
    widest = max(len(ask["groups"]) for _holder, ask, _key in _asks(step))
    for count in range(widest, 0, -1):
        cut, texts = deepcopy(step), {}
        for holder, ask, _key in _asks(cut):
            shown = min(count, len(ask["groups"]))
            ask["groups"] = ask["groups"][:shown]
            ask["options"] = _listed(ask, ask["groups"])
            texts[str(holder.get("message"))] = _short_message(holder, ask, shown)
            if shown < int(ask.get("groups_total") or 0):
                ask["note"] = f"The first {shown} groups are shown. {REST_IN_ARCHITECT}"
        _retext(cut, texts)
        yield cut
    texts = {}
    for holder, ask, _key in _asks(step):
        for field in ("groups", "options", "call"):
            ask.pop(field, None)
        texts[str(holder.get("message"))] = _short_message(holder, ask, 0)
        holder["details"].pop("hint", None)
        ask["note"] = f"No group fits the answer. {REST_IN_ARCHITECT}"
    _retext(step, texts)
    yield step
