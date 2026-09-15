"""Authentication and authorization for Epicor MCP Server.

Handles Azure AD OAuth 2.1 token validation, session management,
and Epicor credential/API-key selection per department.
"""

from epicor_mcp.auth.credentials import CredentialManager
from epicor_mcp.auth.oauth import AzureADTokenValidator
from epicor_mcp.auth.session import MCPSession, SessionStore

__all__ = [
    "AzureADTokenValidator",
    "CredentialManager",
    "MCPSession",
    "SessionStore",
]
