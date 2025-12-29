# -*- coding: utf-8 -*-
"""
In-memory test for DergiPark MCP Server
FastMCP Client ile test
"""

import asyncio
from fastmcp import Client
from mcp_server import mcp


async def test_search():
    """Test search_articles tool"""
    print("=== In-Memory MCP Test ===\n")

    async with Client(mcp) as client:
        # List tools
        tools = await client.list_tools()
        print(f"Available tools: {[t.name for t in tools]}\n")

        # Test search_articles
        print("Testing search_articles...")
        result = await client.call_tool("search_articles", {
            "query": "yapay zeka",
            "page": 1
        })

        print(f"Result type: {type(result)}")
        print(f"Result: {result}")


if __name__ == "__main__":
    asyncio.run(test_search())
