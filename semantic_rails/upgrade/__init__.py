"""Pure package upgrade planning; rules describe YAML paths and never write files."""

from .model import Edit, Finding, Option, PackageFiles, Plan, Rule, plan

__all__ = ["Edit", "Finding", "Option", "PackageFiles", "Plan", "Rule", "plan"]
