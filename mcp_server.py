"""Composition root for wyckoff-mcp and python mcp_server.py."""


def build_backend():
    from agents.public_mcp_backend import DomainBackend

    return DomainBackend()


def main() -> None:
    from integrations.public_mcp.runtime import Runtime
    from integrations.public_mcp.server import main as serve

    serve(Runtime(backend_factory=build_backend))


if __name__ == "__main__":
    main()
