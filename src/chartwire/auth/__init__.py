"""Authentication and authorization: HS256 JWT, RBAC matrix, scrypt passwords (§8.1)."""

from chartwire.auth.jwt import Principal, TokenError, issue, verify

__all__ = ["Principal", "TokenError", "issue", "verify"]
