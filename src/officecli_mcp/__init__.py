"""officecli-mcp: MCP server wrapping OfficeCLI for OpenWebUI."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("officecli-mcp")
except PackageNotFoundError:  # not installed (e.g. running from source without install)
    __version__ = "0.0.0+dev"
