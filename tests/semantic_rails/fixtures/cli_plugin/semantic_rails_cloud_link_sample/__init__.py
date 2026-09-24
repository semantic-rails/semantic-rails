"""A sample ``semantic-rails`` CLI plugin, shipped as a separate distribution.

It proves the command-registration hook end to end and is the template for
extension authors. A distribution exposes ``register`` in the
``semantic_rails.cli`` entry-point group::

    [project.entry-points."semantic_rails.cli"]
    cloud-link-sample = "semantic_rails_cloud_link_sample:register"

It uses the three kinds of registration an extension needs:

- a new command group and command: ``semantic-rails cloud link``;
- an extension of an existing command: ``semantic-rails mcp setup --sample-client``;
- a new import source: ``semantic-rails import --from sample-json``.

It is a sample only. Nothing here talks to a service or writes a file: each
command prints the JSON it would act on. ``register`` itself only records
operations, with no printing or I/O, because every CLI run imports it.
"""

from __future__ import annotations

import argparse
from typing import Any

from semantic_rails.cli.registry import (
    CommandRegistry,
    add_package_arguments,
    package_ref_from_args,
    print_json,
)


def register(registry: CommandRegistry) -> None:
    registry.add_group(
        ("cloud",),
        help="Sample plugin: connect a package to a hosted workspace",
        description="Commands added by the semantic-rails-cloud-link-sample distribution.",
    )
    registry.add_command(
        ("cloud", "link"),
        _link,
        help="Show what linking this package would send (sample: sends nothing)",
        configure=_configure_link,
    )
    registry.extend_command(("mcp", "setup"), configure=_configure_setup, wrap=_wrap_setup)
    registry.add_import_source(
        "sample-json",
        _import_sample_json,
        help="A JSON file of metric definitions (sample: prints what it would import)",
    )


def _configure_link(parser: argparse.ArgumentParser) -> None:
    add_package_arguments(parser)
    parser.add_argument("--workspace", required=True, help="Hosted workspace to link to.")


def _link(args: argparse.Namespace) -> None:
    ref = package_ref_from_args(args)
    print_json(
        {
            "ok": True,
            "sample": True,
            "linked": False,
            "workspace": args.workspace,
            "package": {"id": ref.package_id, "source_path": ref.source_path},
            "message": "Sample plugin: nothing was sent.",
        }
    )


def _configure_setup(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--sample-client",
        default="",
        help="Sample plugin: preview MCP config for another client (writes nothing).",
    )


def _wrap_setup(handler: Any) -> Any:
    def setup(args: argparse.Namespace) -> None:
        if not args.sample_client:
            handler(args)  # the built-in `mcp setup`, unchanged
            return
        print_json(
            {
                "ok": True,
                "sample": True,
                "client": args.sample_client,
                "command": ["semantic-rails", "mcp", "stdio"],
                "message": "Sample plugin: this is the config it would write.",
            }
        )

    return setup


def _import_sample_json(args: argparse.Namespace) -> None:
    print_json(
        {
            "ok": True,
            "sample": True,
            "source": args.source,
            "output": args.output,
            "package_id": args.package_id,
            "message": "Sample plugin: nothing was imported.",
        }
    )
