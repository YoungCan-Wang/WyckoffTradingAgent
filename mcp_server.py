"""Compatibility entrypoint for wyckoff-mcp and python mcp_server.py."""

from integrations.public_mcp.server import main

if __name__ == "__main__":
    main()
