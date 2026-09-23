"""Assign every support label from each layer's committed artifacts with one rule (shared/rubric.md).

Nobody labels a layer by hand, Semantic Rails included. Each label records the rule that fired
and its evidence, so a reader can check it against the artifacts.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[4]
PACK = REPO_ROOT / "comparisons" / "semantic_layers"
RESULTS = PACK / "shared" / "results"
OUTPUT_PATH = RESULTS / "rubric" / "labels.json"

# A trivial passthrough of one shared view isn't hand-written logic.
PASSTHROUGH = re.compile(r"^\s*select\s+\*\s+from\s+comparison_\w+\s*;?\s*$", re.IGNORECASE)
RULES = [
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


SR_PACKAGE = PACK / "semantic_rails" / "package"
SHARED_VIEW = re.compile(r"comparison_\w+")


def semantic_rails(entry: dict[str, Any]) -> tuple[list[str], list[str]]:
    # The rubric doesn't resolve which models a metric reads, so any derived relation in the
    # package counts against every answer: a relation pipeline, or a model over anything but
    # one shared view.
    helpers = []
    package = yaml.safe_load((SR_PACKAGE / "package.yml").read_text(encoding="utf-8"))
    if package.get("relations") or any(
        (SR_PACKAGE / name).exists() for name in ("relations.yml", "relations")
    ):
        helpers.append("relation pipelines")
    for path in sorted((SR_PACKAGE / "models").rglob("*.yml")):
        model = yaml.safe_load(path.read_text(encoding="utf-8"))["model"]
        relation = model.get("relation")
        if (
            {"relation_ref", "variants"} & set(model)
            or not isinstance(relation, str)
            or not SHARED_VIEW.fullmatch(relation)
        ):
            helpers.append(f"derived model {model['id']}")
    return helpers, [_read(entry["sql_path"])] if entry.get("sql_path") else []


MF_MODELS = PACK / "metricflow" / "models"


def metricflow(entry: dict[str, Any]) -> tuple[list[str], list[str]]:
    executed = _read(entry["sql_path"]).split("SQL (remove --explain", 1)[-1]
    config = yaml.safe_load((MF_MODELS / "_models.yml").read_text(encoding="utf-8"))
    # MetricFlow requires a time spine model: it's configuration, not logic for this pack.
    time_spines = {model["name"] for model in config.get("models", []) if "time_spine" in model}
    models = {path.stem: path for path in MF_MODELS.rglob("*.sql")}
    helpers, texts = [], [executed]
    for relation in sorted(set(re.findall(r'"main"\."(\w+)"', executed))):
        if relation not in models or relation in time_spines:
            continue
        body = models[relation].read_text(encoding="utf-8")
        if not is_passthrough(body):
            helpers.append(f"dbt model {relation}")
            texts.append(body)
    return helpers, texts


def cube(entry: dict[str, Any]) -> tuple[list[str], list[str]]:
    query = json.loads(_read(entry["query_path"]))
    members = [*query.get("measures", []), *query.get("dimensions", [])]
    members += [item["dimension"] for item in query.get("timeDimensions", [])]
    members += [item["member"] for item in query.get("filters", []) if "member" in item]
    helpers = []
    for name in sorted({member.split(".")[0] for member in members}):
        model = PACK / "cube" / "model" / "cubes" / f"{name}.yml"
        spec = yaml.safe_load(model.read_text(encoding="utf-8"))["cubes"][0]
        if "sql" in spec and not is_passthrough(spec["sql"]):
            helpers.append(f"cube {name}")
    executed = json.loads(_read(entry["sql_path"]))["sql"]["sql"][0]
    return helpers, [executed]


MALLOY_SOURCE = re.compile(r"^source: (\w+) is jaffle\.(table|sql)\(", re.MULTILINE)


def malloy(entry: dict[str, Any]) -> tuple[list[str], list[str]]:
    model = (PACK / "malloy" / "models" / "jaffle.malloy").read_text(encoding="utf-8")
    kinds = dict(MALLOY_SOURCE.findall(model))
    blocks = dict(
        re.findall(r"^source: (\w+) is (.*?)(?=^source: |^query: |\Z)", model, re.M | re.S)
    )
    question_id = entry["question_id"]
    pending = [re.search(rf"^query: {question_id} is (\w+)", model, flags=re.MULTILINE).group(1)]
    used: set[str] = set()
    while pending:  # the query's source and every source it joins
        source = pending.pop()
        if source not in used:
            used.add(source)
            pending += re.findall(r"join_(?:one|many|cross): (\w+)", blocks.get(source, ""))
    helpers = [f"SQL source {source}" for source in sorted(used) if kinds.get(source) == "sql"]
    return helpers, [_read(entry["sql_path"])]


def ktx(entry: dict[str, Any]) -> tuple[list[str], list[str]]:
    payload = json.loads(_read(entry["query_path"]))
    fields = [*payload.get("measures", [])]
    fields += [
        item if isinstance(item, str) else item["field"] for item in payload.get("dimensions", [])
    ]
    fields += [re.split(r"\s", item)[0] for item in payload.get("filters", [])]
    helpers = []
    for name in sorted({field.split(".")[0] for field in fields}):
        spec = yaml.safe_load(
            (PACK / "ktx" / "sources" / f"{name}.yaml").read_text(encoding="utf-8")
        )
        if "sql" in spec and not is_passthrough(spec["sql"]):
            helpers.append(f"SQL source {name}")
    return helpers, [_read(entry["sql_path"])]


def snowflake(entry: dict[str, Any]) -> tuple[list[str], list[str]]:
    examples = (PACK / "snowflake_semantic_views" / "query_examples.sql").read_text(
        encoding="utf-8"
    )
    section = examples.split(f"-- {entry['question_id']}\n", 1)[1].split("\n-- q", 1)[0]
    helpers = [] if "SEMANTIC_VIEW(" in section else ["SQL outside SEMANTIC_VIEW(...)"]
    return helpers, [section]


DETECTORS = {
    "semantic_rails": ("semantic_rails", semantic_rails),
    "metricflow": ("metricflow", metricflow),
    "cube": ("cube_sql_replay", cube),
    "malloy": ("malloy", malloy),
    "snowflake_semantic_views": ("snowflake_semantic_views", snowflake),
    "ktx": ("ktx", ktx),
}


def build_labels() -> dict[str, Any]:
    questions = yaml.safe_load((PACK / "shared" / "questions.yml").read_text(encoding="utf-8"))
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
            helpers, texts = detect(entry) if executed else ([], [])
            labels[layer][question["id"]] = decide(
                executed, helpers, texts, list(question.get("bypass_columns", []))
            )
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
