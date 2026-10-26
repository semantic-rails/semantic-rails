"""A large synthetic package for route-census scale tests: a campus warehouse.

74 entities in 17 subject areas, 123 many-to-one relationships walked both ways, and the
default four-hop ceiling: the shape of a real institution's warehouse once every foreign key is
related. Each entity has one model with a name, a row count and a metric; the examples are the
questions an analyst would author. ``copies=2`` writes a second campus beside the first, both
belonging to one ``system``, for a 149-entity package.

``python -m tests.semantic_rails.campus_package DIR [COPIES]`` writes it to ``DIR``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

AREAS = {
    "facilities": "campus building floor room room_use space_unit building_history",
    "organization": "school department org_unit cost_center",
    "people": "person employee job_title appointment student",
    "academics": "term subject course class_section enrollment grade_scale evaluation",
    "finance": "fiscal_year fiscal_period account_type account fund gl_transaction budget_line "
    "vendor purchase_order po_line invoice",
    "research": "sponsor proposal award expenditure protocol",
    "library": "library_branch title item loan hold_request",
    "housing": "residence residence_room housing_assignment",
    "workshops": "workshop workshop_session workshop_registration",
    "it": "asset_type asset ticket",
    "admissions": "applicant program application admission_decision",
    "parking": "lot permit citation",
    "events": "venue event event_attendance",
    "payroll": "payroll_run paycheck benefits_plan benefit_enrollment",
    "advancement": "donor gift campaign",
    "maintenance": "work_order work_order_task",
    "utilities": "meter meter_reading",
}
ENTITIES = [name for names in AREAS.values() for name in names.split()]

# source target [role]: a many-to-one key; a role names a second key to the same target.
RELATIONSHIPS = """
building campus; floor building; room floor; room building; room room_use; space_unit room;
space_unit building; building_history building; room org_unit; space_unit org_unit;
department school; org_unit department; cost_center org_unit;
employee person; employee department; appointment employee; appointment job_title;
appointment org_unit; student person; student department;
subject department; course subject; class_section course; class_section term;
class_section room; class_section employee; enrollment student; enrollment class_section;
enrollment term; enrollment grade_scale; evaluation class_section; evaluation student;
fiscal_period fiscal_year; account account_type; gl_transaction account; gl_transaction fund;
gl_transaction cost_center; gl_transaction fiscal_period posting;
gl_transaction fiscal_period entry; budget_line account; budget_line cost_center;
budget_line fiscal_year; purchase_order vendor; purchase_order cost_center;
purchase_order employee requester; purchase_order employee approver; po_line purchase_order;
po_line account; invoice vendor; invoice purchase_order; invoice fiscal_period;
proposal sponsor; proposal department; proposal employee; award proposal; award sponsor;
award department; award employee; expenditure award; expenditure account;
expenditure fiscal_period; protocol award;
library_branch building; item title; item library_branch; loan item; loan person;
loan library_branch; hold_request title; hold_request person; hold_request library_branch;
residence building; residence_room residence; residence_room room;
housing_assignment student; housing_assignment residence_room; housing_assignment term;
workshop department; workshop_session workshop; workshop_session room;
workshop_registration workshop_session; workshop_registration person;
asset asset_type; asset room; asset org_unit; ticket asset; ticket person requester;
ticket employee assignee;
applicant person; program department; application applicant; application program;
application term; admission_decision application;
lot campus; permit person; permit lot; citation permit; citation lot;
venue building; event venue; event department; event_attendance event; event_attendance person;
paycheck employee; paycheck payroll_run; paycheck cost_center; benefit_enrollment employee;
benefit_enrollment benefits_plan;
donor person; gift donor; gift fund; gift department; gift campaign;
work_order room; work_order building; work_order employee; work_order cost_center;
work_order_task work_order; work_order_task employee technician;
meter building; meter_reading meter; meter_reading fiscal_period
"""

# Questions an analyst authors as examples: (count of, by the name of).
EXAMPLES = [
    ("enrollment", "department"),
    ("paycheck", "department"),
    ("work_order", "building"),
    ("loan", "campus"),
    ("gift", "school"),
    ("citation", "lot"),
    ("ticket", "department"),
    ("award", "school"),
    ("housing_assignment", "building"),
    ("event_attendance", "school"),
    ("space_unit", "department"),
    ("meter_reading", "fiscal_year"),
]


def _label(name: str) -> str:
    return name.replace("_", " ").capitalize()


def write_campus_package(root: Path, *, copies: int = 1) -> Path:
    """Write the package under ``root`` (``copies`` campuses) and return its directory."""
    package = root / ("campus" if copies == 1 else f"campus_x{copies}")
    (package / "models").mkdir(parents=True)
    (package / "metrics").mkdir()
    (package / "examples").mkdir()
    suffixes = [""] + [f"_{copy}" for copy in range(2, copies + 1)]
    entities: dict[str, dict] = {}
    relationships: dict[str, dict] = {}
    metrics: dict[str, dict] = {}
    meta = {"owner_team": "analytics", "review_priority": "low", "change_risk": "low"}
    names = [name + suffix for suffix in suffixes for name in ENTITIES]
    for suffix in suffixes:
        for item in RELATIONSHIPS.replace("\n", " ").split(";"):
            if parts := item.split():
                source, target, *role = parts
                column = f"{role[0] + '_' if role else ''}{target}_id"
                relationships[f"{source}{suffix}_{role[0] if role else target}"] = {
                    "entities": [source + suffix, target + suffix],
                    "cardinality": "many_to_one",
                    "via": [column],
                    "target": [f"{target}_id"],
                }
        if copies > 1:
            relationships[f"campus{suffix}_system"] = {
                "entities": [f"campus{suffix}", "system"],
                "cardinality": "many_to_one",
                "via": ["system_id"],
                "target": ["system_id"],
            }
    if copies > 1:
        names.append("system")
    for name in names:
        label = _label(name)
        entities[name] = {"label": label, "key": [f"{name}_id"], "model": name}
        model = {
            "id": name,
            "label": label,
            "relation": name,
            "entities": {name: {}},
            "dimensions": {"name": {"label": f"{label} name", "kind": "categorical"}},
            "measures": {
                f"{name}_count": {
                    "label": f"{label} count",
                    "kind": "entity_count",
                    "entity_key": f"{name}_id",
                    "meta": meta,
                }
            },
        }
        (package / "models" / f"{name}.yml").write_text(yaml.safe_dump({"model": model}))
        metrics[f"{name}_count"] = {
            "label": f"{label} count",
            "kind": "aggregate",
            "measure": f"{name}_count",
            "value_type": "count",
            "meta": meta,
        }
    examples = {
        f"{fact}_by_{dimension}": {
            "question": f"{_label(fact)} count by {dimension.replace('_', ' ')}",
            "query": {
                "select": [f"measure.campus.{fact}_count"],
                "group_by": [f"dimension.campus_{dimension}_name"],
            },
        }
        for fact, dimension in EXAMPLES
    }
    documents = {
        "package.yml": {
            "schema_version": 1,
            "package": {
                "id": package.name,
                "namespace": "campus",
                "warehouse": "duckdb",
                "environments": ["development"],
                "default_db": "campus.duckdb",
                "seed": {"kind": "external"},
            },
        },
        "graph.yml": {"graph": {"entities": entities, "relationships": relationships}},
        "metrics/counts.yml": {"metrics": metrics},
        "examples/core.yml": {"examples": examples},
    }
    for name, document in documents.items():
        (package / name).write_text(yaml.safe_dump(document, sort_keys=False))
    return package


if __name__ == "__main__":
    print(write_campus_package(Path(sys.argv[1]), copies=int(sys.argv[2:3] and sys.argv[2] or 1)))
