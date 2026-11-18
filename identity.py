"""Workspace selection for trusted OIDC identities."""
import hashlib
import json
from pathlib import Path


def tenant_workspace(root, claims):
    issuer, subject = claims.get("iss"), claims.get("sub")
    if not isinstance(issuer, str) or not issuer or not isinstance(subject, str) or not subject:
        raise ValueError("身份提供商未返回有效的 iss 和 sub 声明。")
    tenant = hashlib.sha256(json.dumps([issuer, subject]).encode()).hexdigest()
    return Path(root).expanduser().resolve() / "tenants" / tenant
