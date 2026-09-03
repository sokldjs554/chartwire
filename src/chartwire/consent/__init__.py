"""Consent lifecycle: scopes and deterministic gates (§8.3)."""

from chartwire.consent.gates import ConsentScopeMissing, active_scopes, require_scope
from chartwire.consent.scopes import SCOPES, Scope

__all__ = ["SCOPES", "ConsentScopeMissing", "Scope", "active_scopes", "require_scope"]
