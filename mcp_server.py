# -*- coding: utf-8 -*-
"""
DergiPark MCP Server

MCP server for searching and analyzing Turkish academic journal articles
using FastMCP v2.14.1+.

Usage:
    python mcp_server.py          # Direct run
    fastmcp dev mcp_server.py     # Development mode
    fastmcp run mcp_server.py     # Production mode
"""

import sys
from typing import Optional, Literal, Annotated
from contextlib import asynccontextmanager
from fastmcp import FastMCP, Context
from pydantic import Field

from core import (
    search_articles_core,
    pdf_to_html_core,
    get_article_references_core,
    browser_pool_manager,
)


# --- Lifespan Context Manager ---
@asynccontextmanager
async def lifespan(server: FastMCP):
    """Manage browser pool lifecycle."""
    print("=== MCP SERVER STARTUP ===", file=sys.stderr)
    await browser_pool_manager.initialize()
    print("=== BROWSER POOL READY ===", file=sys.stderr)
    try:
        yield {}
    finally:
        print("=== MCP SERVER SHUTDOWN ===", file=sys.stderr)
        await browser_pool_manager.cleanup()
        print("=== CLEANUP COMPLETE ===", file=sys.stderr)


# --- FastMCP Server Initialization ---
mcp = FastMCP(
    name="DergiPark MCP",
    instructions="""
    DergiPark Academic Article Search and Analysis MCP Server.

    This server is used for searching Turkish academic journals, converting PDFs
    to readable format, and extracting article metadata.

    Features:
    - Article search (with year, type, sorting filters)
    - PDF to HTML conversion
    - Article metadata and index information extraction

    Note: Automatically handles CAPTCHA protection.
    """,
    lifespan=lifespan,
)


# --- MCP Tools ---

@mcp.tool
async def search_articles(
    query: Annotated[str, Field(description="Search query (e.g., 'artificial intelligence', 'sociology'). Leave empty to search all articles.")] = "",
    dergipark_page: Annotated[int, Field(ge=1, description="DergiPark page number (default: 1)")] = 1,
    page: Annotated[int, Field(ge=1, description="API pagination (24 articles per page, default: 1)")] = 1,
    sort: Annotated[Optional[Literal["newest", "oldest"]], Field(description="Sort order: 'newest' or 'oldest'")] = None,
    article_type: Annotated[Optional[str], Field(description="Article type filter (e.g., '54' = Research Article)")] = None,
    year: Annotated[Optional[str], Field(description="Publication year filter (e.g., '2024', '2023')")] = None,
    index_filter: Annotated[Optional[Literal["tr_dizin_icerenler", "bos_olmayanlar", "hepsi"]], Field(description="Index filter: 'tr_dizin_icerenler' (TR Index only), 'bos_olmayanlar' (has any index), 'hepsi' (all)")] = "hepsi",
    ctx: Context = None,
) -> dict:
    """
    Search Turkish academic journals on DergiPark.

    Returns paginated results with 24 articles per page. Each article includes
    title, authors, abstract, keywords, DOI, indexes, and PDF link.

    Examples:
    - General search: search_articles(query="machine learning")
    - Year filtered: search_articles(query="education", year="2024")
    - Latest first: search_articles(query="health", sort="newest")
    - TR Index only: search_articles(query="economics", index_filter="tr_dizin_icerenler")
    """
    if ctx:
        await ctx.info(f"Searching DergiPark: '{query or '*'}'")

    try:
        result = await search_articles_core(
            q=query or None,
            dergipark_page=dergipark_page,
            api_page=page,
            sort_by=sort,
            article_type=article_type,
            publication_year=year,
            index_filter=index_filter,
        )

        if ctx:
            article_count = len(result.get("articles", []))
            total = result.get("pagination", {}).get("total_items_on_dergipark_page", 0)
            await ctx.info(f"Found: {article_count} articles (total: {total})")

        return result

    except Exception as e:
        error_msg = f"Search error: {str(e)}"
        if ctx:
            await ctx.error(error_msg)
        return {"error": error_msg, "pagination": None, "articles": []}


@mcp.tool
async def pdf_to_html(
    pdf_id: Annotated[str, Field(description="DergiPark article file ID (e.g., '118146')")],
    ctx: Context = None,
) -> str:
    """
    Convert a DergiPark PDF to readable HTML format.

    Only the numeric file ID is required (e.g., '118146').
    URL is automatically constructed: dergipark.org.tr/tr/download/article-file/{id}
    """
    pdf_url = f"https://dergipark.org.tr/tr/download/article-file/{pdf_id}"

    if ctx:
        await ctx.info(f"Converting PDF: {pdf_id}")

    try:
        result = await pdf_to_html_core(pdf_url)

        if ctx:
            await ctx.info("PDF converted successfully")

        return result

    except Exception as e:
        error_msg = f"PDF conversion error: {str(e)}"
        if ctx:
            await ctx.error(error_msg)
        return f"<html><body><h1>Error</h1><p>{error_msg}</p></body></html>"


@mcp.tool
async def get_article_references(
    article_url: Annotated[str, Field(description="DergiPark article URL (e.g., 'https://dergipark.org.tr/en/pub/eskiyeni/article/434507')")],
    ctx: Context = None,
) -> dict:
    """
    Get references list for a DergiPark article.

    Returns the full list of references cited in the article.
    Use this after search_articles to get detailed reference information.
    """
    if ctx:
        await ctx.info(f"Fetching references: {article_url[:50]}...")

    try:
        result = await get_article_references_core(article_url)

        if ctx:
            count = result.get('reference_count', 0)
            await ctx.info(f"Found {count} references")

        return result

    except Exception as e:
        error_msg = f"References fetch error: {str(e)}"
        if ctx:
            await ctx.error(error_msg)
        return {"error": error_msg, "references": []}


@mcp.tool
async def summarize_article(
    pdf_id: Annotated[str, Field(description="DergiPark article file ID (e.g., '118146')")],
    ctx: Context = None,
) -> str:
    """
    Summarize a DergiPark article using LLM.

    Converts the PDF to text and generates a Turkish summary.
    Requires client to support LLM sampling.
    """
    pdf_url = f"https://dergipark.org.tr/tr/download/article-file/{pdf_id}"

    if ctx:
        await ctx.info(f"Converting PDF: {pdf_id}")

    try:
        html_content = await pdf_to_html_core(pdf_url)

        if ctx:
            await ctx.info("Summarizing with LLM...")

        result = await ctx.sample(
            messages=f"Bu akademik makaleyi Türkçe özetle:\n\n{html_content}",
            system_prompt="Sen akademik makale özetleme uzmanısın. Kısa ve öz özetler yaz.",
            max_tokens=1000,
        )

        return result.text or ""

    except Exception as e:
        error_msg = f"Summarization error: {str(e)}"
        if ctx:
            await ctx.error(error_msg)
        return f"Error: {error_msg}"


# --- Entry Point ---

def main():
    """Entry point for the MCP server."""
    mcp.run()


if __name__ == "__main__":
    main()
