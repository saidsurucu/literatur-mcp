"""
ASGI application for DergiPark MCP Server

Run with:
    uvicorn app:app --host 0.0.0.0 --port 8000

MCP endpoint: http://localhost:8000/mcp/
"""

from starlette.responses import JSONResponse
from mcp_server import mcp

@mcp.custom_route("/health", methods=["GET"])
async def health_check(request):
    """Health check endpoint"""
    return JSONResponse({
        "status": "healthy",
        "service": "DergiPark MCP Server",
        "version": "0.1.0"
    })

app = mcp.http_app()
