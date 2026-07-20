from pathlib import Path


def main() -> None:
    pack_dir = Path(__file__).resolve().parents[1]
    yaml_path = pack_dir / "jaffle_semantic_view.yaml"
    output_path = pack_dir / "generated" / "create_semantic_view.sql"

    yaml_body = yaml_path.read_text()
    sql = f"""-- Generated from jaffle_semantic_view.yaml.
-- Run this after the base COMPARISON_* tables exist in ANALYTICS.SEMANTIC_COMPARISON.

USE ROLE ACCOUNTADMIN;
USE WAREHOUSE SEMANTIC_WH;
USE DATABASE ANALYTICS;
USE SCHEMA SEMANTIC_COMPARISON;

-- Verify the YAML first.
CALL SYSTEM$CREATE_SEMANTIC_VIEW_FROM_YAML(
  'ANALYTICS.SEMANTIC_COMPARISON',
  $${yaml_body}$$,
  TRUE
);

-- Create or replace the semantic view.
CALL SYSTEM$CREATE_SEMANTIC_VIEW_FROM_YAML(
  'ANALYTICS.SEMANTIC_COMPARISON',
  $${yaml_body}$$
);

SHOW SEMANTIC VIEWS IN SCHEMA ANALYTICS.SEMANTIC_COMPARISON;
DESCRIBE SEMANTIC VIEW ANALYTICS.SEMANTIC_COMPARISON.JAFFLE_SEMANTIC_COMPARISON;
"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(sql)


if __name__ == "__main__":
    main()
