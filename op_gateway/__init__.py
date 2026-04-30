"""op MCP gateway — single MCP server that proxies to many.

The SDK sees one tool: `op`. Calls take the shape
`{operation: "<namespace>.<tool>", args: {...}}` and are dispatched to
the right backend MCP server.

See README.md and design.md for the full architecture.
"""

__version__ = "0.0.1"
