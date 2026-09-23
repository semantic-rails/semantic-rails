"""``python -m semantic_rails.cli`` entry point.

MCP client configs written by ``semantic-rails mcp setup`` launch servers
this way, so it must keep working after the CLI became a package.
"""

from .app import main

if __name__ == "__main__":
    # ``python -m semantic_rails.cli`` previously exited 0 having done
    # nothing, which reads as a silent success.
    main()
