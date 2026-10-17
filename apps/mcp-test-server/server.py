"""Harmless cluster MCP fixture: no credentials, files, or side effects."""
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

server = MCPServer('cluster-lab')

@server.tool()
def echo(nonce: str) -> dict:
    """Return a caller's nonce to prove a real round trip to the cluster MCP server."""
    return {'nonce': nonce, 'source': 'cluster-mcp-test-server'}

if __name__ == '__main__':
    server.run('streamable-http', host='0.0.0.0', port=8000, stateless_http=True,
               transport_security=TransportSecuritySettings(allowed_hosts=['mcp-test-server:8000', 'mcp-test-server.chat.svc.cluster.local:8000', 'localhost:8000']))
