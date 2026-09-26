"""Apache Ossie interop: export a package as an Ossie 0.1.1 document plus a sidecar."""

from .export import OSSIE_VERSION, export_ossie, write_ossie_export

__all__ = ["OSSIE_VERSION", "export_ossie", "write_ossie_export"]
