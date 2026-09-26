"""Assign every support label from each layer's committed artifacts with one rule (shared/rubric.md).

Nobody labels a layer by hand, Semantic Rails included. Each label records the rule that fired
and its evidence, so a reader can check it against the artifacts.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from semantic_rails.config import _merge_package_dir

REPO_ROOT = Path(__file__).resolve().parents[4]
PACK = REPO_ROOT / "comparisons" / "semantic_layers"
RESULTS = PACK / "shared" / "results"
OUTPUT_PATH = RESULTS / "rubric" / "labels.json"
FROZEN_MODEL_PATH = PACK / "shared" / "frozen_model.yml"

# A trivial passthrough of one shared view isn't hand-written logic.
PASSTHROUGH = re.compile(r"^\s*select\s+\*\s+from\s+comparison_\w+\s*;?\s*$", re.IGNORECASE)
RULES = [
    (
        "not_assessed",
        "Frozen-model questions only: the layer isn't listed in frozen_model.yml, because this "
        "pack can't run it on them.",
    ),
    (
        "requires_model_change",
        "Frozen-model questions only: the layer's documented query-time interface can't express "
        "the question with its model unchanged; frozen_model.yml gives the reason and the "
        "documentation.",
    ),
    ("unsupported", "The layer didn't execute the question."),
    (
        "precomputed",
        "The answer reads a cross-row rollup column that the question declares in "
        "bypass_columns, in the executed SQL or in SQL it depends on.",
    ),
    (
        "workaround",
        "The answer depends on SQL written by hand for this pack: a derived table, a "
        "SQL-defined source or cube, or a statement outside the layer's semantic interface.",
    ),
    ("native", "None of the above: the layer's semantic constructs only."),
]


def is_passthrough(sql: str) -> bool:
    body = re.sub(r"\{\{.*?\}\}", "", sql, flags=re.DOTALL)
    body = "\n".join(line for line in body.splitlines() if not line.strip().startswith("--"))
    return PASSTHROUGH.match(body.strip()) is not None


def reads_column(sql: str, column: str) -> bool:
    return re.search(rf"\b{re.escape(column)}\b", sql, flags=re.IGNORECASE) is not None


def decide(
    executed: bool, helpers: list[str], sql_texts: list[str], bypass: list[str]
) -> dict[str, Any]:
    """Apply the rules in order; the first that holds labels the answer."""
    if not executed:
        return {"label": "unsupported", "evidence": []}
    bypassed = [column for column in bypass if any(reads_column(t, column) for t in sql_texts)]
    if bypassed:
        return {"label": "precomputed", "evidence": [f"reads {column}" for column in bypassed]}
    if helpers:
        return {"label": "workaround", "evidence": sorted(helpers)}
    return {"label": "native", "evidence": []}


def _read(relative: str) -> str:
    return (REPO_ROOT / relative).read_text(encoding="utf-8")


def _executed_sql(entry: dict[str, Any]) -> str:
    """Fail closed: an executed answer whose SQL is missing can't be checked for bypass columns."""
    path = entry.get("sql_path")
    text = _read(path) if path and (REPO_ROOT / path).is_file() else ""
    if not text.strip():
        raise SystemExit(f"{entry['question_id']}: found no executed SQL to check")
    return text


def _require(found: list[str], what: str, entry: dict[str, Any]) -> list[str]:
    """Fail closed: a detector that finds nothing to check can't vouch for an answer."""
    if not found:
        raise SystemExit(f"{entry['question_id']}: found no {what} to check")
    return found


SR_PACKAGE = PACK / "semantic_rails" / "package"
SHARED_VIEW = re.compile(r"comparison_\w+")


def semantic_rails(entry: dict[str, Any]) -> tuple[list[str], list[str]]:
    # The rubric doesn't resolve which models a metric reads, so any derived relation in the
    # package counts against every answer: a relation pipeline, an aggregate relation, or a
    # model over anything but one shared view. The package is read the way the engine reads
    # it, so a model declared anywhere the engine accepts one is checked.
    package = _merge_package_dir(str(SR_PACKAGE))
    helpers = []
    if package.get("relations") or package.get("aggregate_relations"):
        helpers.append("relation pipelines")
    models = _require(sorted(dict(package.get("models") or {}).items()), "models", entry)
    for model_id, model in models:
        relation = model.get("relation")
        if (
            {"relation_ref", "variants"} & set(model)
            or not isinstance(relation, str)
            or not SHARED_VIEW.fullmatch(relation)
        ):
            helpers.append(f"derived model {model_id}")
    return helpers, [_executed_sql(entry)]


MF_MODELS = PACK / "metricflow" / "models"


def metricflow(entry: dict[str, Any]) -> tuple[list[str], list[str]]:
    executed = _executed_sql(entry).split("SQL (remove --explain", 1)[-1]
    config = yaml.safe_load((MF_MODELS / "_models.yml").read_text(encoding="utf-8"))
    # MetricFlow requires a time spine model: it's configuration, not logic for this pack.
    time_spines = {model["name"] for model in config.get("models", []) if "time_spine" in model}
    models = {path.stem: path for path in MF_MODELS.rglob("*.sql")}
    helpers, texts = [], [executed]
    relations = sorted(set(re.findall(r'"main"\."(\w+)"', executed)))
    for relation in _require(relations, "relations in the executed SQL", entry):
        if relation not in models or relation in time_spines:
            continue
        body = models[relation].read_text(encoding="utf-8")
        if not is_passthrough(body):
            helpers.append(f"dbt model {relation}")
            texts.append(body)
    return helpers, texts


# SQL an SQL API query writes around Cube's members, beyond selecting and aggregating them.
SQL_AROUND_MEMBERS = {
    "SQL API query over a derived table": r"\(\s*select\b",
    "SQL API window function": r"\bover\s*\(",
    "SQL API CASE expression": r"\bcase\b",
    "SQL API HAVING, FILTER or UNION": r"\bhaving\b|\bfilter\s*\(|\bunion\b",
}


def cube(entry: dict[str, Any]) -> tuple[list[str], list[str]]:
    cubes, helpers = PACK / "cube" / "model" / "cubes", []
    sql_api = entry["query_path"].endswith(".sql")
    if sql_api:
        # An SQL API query. Anything beyond selecting and aggregating members (and filtering on
        # them) is SQL written by hand, whether Cube post-processes it or pushes it down.
        sql = _read(entry["query_path"])
        names = re.findall(r"\b(?:from|join)\s+([a-z_]\w*)", sql, flags=re.IGNORECASE)
        names = [name for name in names if (cubes / f"{name}.yml").is_file()]
        helpers += [
            what for what, found in SQL_AROUND_MEMBERS.items() if re.search(found, sql, re.I)
        ]
    else:
        query = json.loads(_read(entry["query_path"]))
        members = [*query.get("measures", []), *query.get("dimensions", [])]
        members += [item["dimension"] for item in query.get("timeDimensions", [])]
        members += [item["member"] for item in query.get("filters", []) if "member" in item]
        names = [member.split(".")[0] for member in members]
    for name in _require(sorted(set(names)), "cubes", entry):
        spec = yaml.safe_load((cubes / f"{name}.yml").read_text(encoding="utf-8"))["cubes"][0]
        if "sql" in spec and not is_passthrough(spec["sql"]):
            helpers.append(f"cube {name}")
    generated = json.loads(_executed_sql(entry))["sql"]
    # Cube has no single SQL for an SQL API query it post-processes; its own statement is what ran.
    executed = generated["sql"][0] if "sql" in generated or not sql_api else sql
    return helpers, [executed]


# `name is jaffle.sql("""...""")`: a SQL source, or a join declared inline.
MALLOY_SQL = re.compile(r"(\w+)\s+is\s+\w+\.sql\(\s*(\"\"\"|\"|')(.*?)\2\s*\)", re.DOTALL)


def _squash(sql: str) -> str:
    """Lowercase, single spaces, and no space just inside parentheses."""
    return re.sub(r"\(\s+|\s+\)", lambda m: m.group().strip(), " ".join(sql.split()).lower())


def malloy(entry: dict[str, Any]) -> tuple[list[str], list[str]]:
    model = (PACK / "malloy" / "models" / "jaffle.malloy").read_text(encoding="utf-8")
    # A frozen-model question's query is its own file, which imports the model.
    query_file = PACK / "malloy" / "queries" / f"{entry['question_id']}.malloy"
    if query_file.is_file():
        text = query_file.read_text(encoding="utf-8")
        # Only the model's import and the question's own query: another import, a source or a
        # run here would be model authoring outside the pinned model.
        imports = re.findall(r"^\s*import\b.*$", text, flags=re.MULTILINE)
        declared = re.findall(r"^\s*(source|query|run)\s*:", text, flags=re.MULTILINE)
        if imports != ['import "../models/jaffle.malloy"'] or declared != ["query"]:
            raise SystemExit(f"{query_file.name} must hold one import of the model and one query")
        model += "\n" + text
    query = rf"^query:\s+{re.escape(entry['question_id'])}\s+is\b"
    _require(re.findall(query, model, flags=re.MULTILINE), "named query", entry)
    executed = _executed_sql(entry)
    # Malloy compiles a SQL block into the query verbatim, as a parenthesized derived table,
    # wherever the query reads it: as its source, through a join or alias, or under an `extend`.
    # Matching the whole parenthesized body keeps a block that is only part of another's SQL
    # from counting.
    helpers = {
        f"SQL source {name}"
        for name, _, body in MALLOY_SQL.findall(model)
        if not is_passthrough(body) and f"({_squash(body)})" in _squash(executed)
    }
    # A raw SQL expression (sql_number, sql_string, ...) in a variant's query is hand-written.
    if query_file.is_file() and re.search(r"\bsql_\w+\s*\(", query_file.read_text("utf-8")):
        helpers.add("raw SQL expression in the query")
    return sorted(helpers), [executed]


def ktx(entry: dict[str, Any]) -> tuple[list[str], list[str]]:
    payload = json.loads(_read(entry["query_path"]))
    fields = [item for item in payload.get("measures", []) if isinstance(item, str)]
    # An inline measure expression reads `source.column` references.
    fields += [
        ref
        for item in payload.get("measures", [])
        if isinstance(item, dict)
        for ref in re.findall(r"\b[A-Za-z_]\w*\.\w+", item["expr"])
    ]
    fields += [
        item if isinstance(item, str) else item["field"] for item in payload.get("dimensions", [])
    ]
    helpers = [
        f"SQL subquery in the inline measure {item['name']}"
        for item in payload.get("measures", [])
        if isinstance(item, dict) and re.search(r"\(\s*select\b", item["expr"], re.IGNORECASE)
    ]
    fields += [re.split(r"\s", item)[0] for item in payload.get("filters", [])]
    for name in _require(sorted({field.split(".")[0] for field in fields}), "sources", entry):
        spec = yaml.safe_load(
            (PACK / "ktx" / "sources" / f"{name}.yaml").read_text(encoding="utf-8")
        )
        if "sql" in spec and not is_passthrough(spec["sql"]):
            helpers.append(f"SQL source {name}")
    return helpers, [_executed_sql(entry)]


def snowflake(entry: dict[str, Any]) -> tuple[list[str], list[str]]:
    executed = _executed_sql(entry)  # the statement the capture ran
    helpers = [] if "SEMANTIC_VIEW(" in executed else ["SQL outside SEMANTIC_VIEW(...)"]
    return helpers, [executed]


DETECTORS = {
    "semantic_rails": ("semantic_rails", semantic_rails),
    "metricflow": ("metricflow", metricflow),
    "cube": ("cube", cube),
    "malloy": ("malloy", malloy),
    "snowflake_semantic_views": ("snowflake_semantic_views", snowflake),
    "ktx": ("ktx", ktx),
}


def model_digest(paths: list[str]) -> str:
    """Identify a layer's model: every file under its model paths, by path and content."""
    files = []
    for relative in paths:
        root = PACK / relative
        found = [root] if root.is_file() else [p for p in root.rglob("*") if p.is_file()]
        # A dotfile (.DS_Store, an editor's swap file) isn't part of the model.
        files += [
            p for p in found if not any(part.startswith(".") for part in p.relative_to(PACK).parts)
        ]
    digest = hashlib.sha256()
    for path in sorted(files):
        data = path.read_bytes().replace(b"\r\n", b"\n")  # the same text on a CRLF checkout
        digest.update(f"{path.relative_to(PACK).as_posix()}\0{len(data)}\0".encode())
        digest.update(data)
    return digest.hexdigest()


def load_frozen_models() -> dict[str, Any]:
    """Fail closed: a frozen-model answer can't be vouched for once its model has changed."""
    frozen = yaml.safe_load(FROZEN_MODEL_PATH.read_text(encoding="utf-8"))
    for layer, spec in frozen.items():
        if model_digest(spec["model"]) != spec["sha256"]:
            raise SystemExit(
                f"{layer}: its model ({', '.join(spec['model'])}) changed since the frozen-model "
                "questions were answered; answer them again before re-pinning frozen_model.yml"
            )
    return frozen


def frozen_label(spec: dict[str, Any] | None, qid: str, executed: bool) -> dict[str, Any] | None:
    """The labels only a frozen-model question can get, checked before the others."""
    if spec is None:
        return {"label": "not_assessed", "evidence": []}
    declared = spec["requires_model_change"].get(qid)
    if declared is None:
        return None
    if executed:
        raise SystemExit(f"{qid} is declared requires_model_change but the layer executed it")
    return {"label": "requires_model_change", "evidence": [declared["reason"], declared["doc"]]}


def explain_workaround(spec: dict[str, Any], qid: str, label: dict[str, Any]) -> None:
    """A frozen-model answer labeled workaround says why, with the documentation behind it."""
    if label["label"] != "workaround":
        return
    why = spec.get("workaround", {}).get(qid)
    if why is None:
        raise SystemExit(f"{qid} is a workaround, but frozen_model.yml doesn't say why")
    label["evidence"] += [why["reason"], why["doc"]]


def build_labels() -> dict[str, Any]:
    questions = yaml.safe_load((PACK / "shared" / "questions.yml").read_text(encoding="utf-8"))
    frozen = load_frozen_models()
    labels: dict[str, dict[str, Any]] = {}
    for layer, (results_dir, detect) in DETECTORS.items():
        directory = RESULTS / results_dir
        summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
        entries = {entry["question_id"]: entry for entry in summary["questions"]}
        failed_path = directory / "unsupported.json"
        failed = json.loads(failed_path.read_text(encoding="utf-8")) if failed_path.exists() else {}
        labels[layer] = {}
        for question in questions["questions"]:
            # A question the layer has no executed result for is unsupported, whatever the reason.
            entry = entries.get(question["id"])
            executed = (
                entry is not None
                and entry["status"] != "unsupported"
                and question["id"] not in failed
            )
            if question.get("scope_level") == "variant":
                label = frozen_label(frozen.get(layer), question["id"], executed)
                if label is not None:
                    labels[layer][question["id"]] = label
                    continue
            helpers, texts = detect(entry) if executed else ([], [])
            labels[layer][question["id"]] = decide(
                executed, helpers, texts, list(question.get("bypass_columns", []))
            )
            if question.get("scope_level") == "variant":
                explain_workaround(frozen[layer], question["id"], labels[layer][question["id"]])
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "rules": [{"label": label, "rule": rule} for label, rule in RULES],
        "labels": labels,
    }


def main() -> None:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(build_labels(), indent=2, sort_keys=True), encoding="utf-8")
    print(f"Wrote rubric labels to {OUTPUT_PATH.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
