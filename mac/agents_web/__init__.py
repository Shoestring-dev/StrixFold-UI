"""Agents' web tools for phase 1a: the egress guard, the URL and query guards,
web_search over a sandboxed SearXNG, web_read through a sandboxed parser, and the files tool; Tools is the interface
the research harness calls. Standard library only in the app process; SearXNG and the parser run as sandboxed
children."""
from .adapter import Failed, Tools, query_reasons, shutdown, status, url_reasons
from .decision import (ALLOW, Allow, Ask, Deny, Denied, FetchFailed, FilesDenied, NeedsApproval, ParserUnavailable,
                       SearchUnavailable, ToolError)
from .exfil import check_query, check_url
from .files import Workspace
from .guard import EgressGuard, check_ip, check_static, clean_url
from .read import Parser, chunk_markdown, est_tokens, instruction_flags, web_read, wrap_untrusted
from .search import SearxService, web_search

__all__ = ["Failed", "Tools", "query_reasons", "shutdown", "status", "url_reasons", "ALLOW", "Allow", "Ask", "Deny", "Denied",
           "FetchFailed", "FilesDenied", "NeedsApproval", "ParserUnavailable", "SearchUnavailable", "ToolError",
           "check_query", "check_url", "Workspace", "EgressGuard", "check_ip", "check_static", "clean_url", "Parser",
           "chunk_markdown", "est_tokens", "instruction_flags", "web_read", "wrap_untrusted", "SearxService",
           "web_search", "untrusted"]


def untrusted(source, text):
    """Tool output as the model sees it: inside delimiters a page cannot close (wrap_untrusted), its source cut to 300."""
    return wrap_untrusted(str(source)[:300], text)
