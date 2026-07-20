"""YAML 1.2 compatible safe loader used for every package YAML read.

Wraps PyYAML's ``CSafeLoader`` / ``SafeLoader`` with the YAML 1.2 boolean
resolution rules so package YAML behaves consistently across hosts that
ship different libyaml builds. Exposes :func:`safe_load` —
:mod:`semantic_rails.config`, :mod:`semantic_rails.config_validation`,
and :mod:`semantic_rails.package_tools` all go through it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import IO, Any

import yaml

_BaseSafeLoader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


class Yaml12SafeLoader(_BaseSafeLoader):  # type: ignore[misc,valid-type]  # dynamic base (CSafeLoader if available, else SafeLoader)
    """PyYAML SafeLoader with YAML 1.2 boolean resolution."""


Yaml12SafeLoader.yaml_implicit_resolvers = {
    key: list(value) for key, value in _BaseSafeLoader.yaml_implicit_resolvers.items()
}

for first_char, resolvers in list(Yaml12SafeLoader.yaml_implicit_resolvers.items()):
    Yaml12SafeLoader.yaml_implicit_resolvers[first_char] = [
        (tag, regexp) for tag, regexp in resolvers if tag != "tag:yaml.org,2002:bool"
    ]

Yaml12SafeLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
    list("tTfF"),
)


def safe_load(stream: str | bytes | IO[str] | IO[bytes]) -> Any:
    return yaml.load(stream, Loader=Yaml12SafeLoader)


def load_yaml_file(path: str | Path) -> Any:
    with open(path, encoding="utf-8") as handle:
        return safe_load(handle)
