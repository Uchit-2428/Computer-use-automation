"""Template rendering for ``{{inputs.x}}``, ``{{secrets.x}}`` and ``{{tenant.x}}``.

Secrets are resolved lazily from references (``env:VAR``) at the moment an action
needs them, so they never sit in the artifact, the plan, the logs or a model prompt.
"""

from __future__ import annotations

import os
import re
from typing import Any, Callable
from urllib.parse import urlparse

from .schema import TEMPLATE_RE, TenantConfig


class SecretError(Exception):
    pass


def resolve_secret_ref(ref: str) -> str:
    if ref.startswith("env:"):
        var = ref[4:]
        v = os.environ.get(var)
        if not v:
            raise SecretError(f"secret env var {var} is not set")
        return v
    raise SecretError(f"unsupported secret reference scheme: {ref.split(':', 1)[0]}")


class Renderer:
    def __init__(self, inputs: dict[str, Any], tenant: TenantConfig, secret_resolver: Callable[[str], str] = resolve_secret_ref):
        self.inputs = {k: str(v) for k, v in inputs.items()}
        self.tenant = tenant
        self._secret_resolver = secret_resolver
        u = urlparse(tenant.base_url)
        self.tenant_vars = {"base_url": tenant.base_url.rstrip("/"), "base_path": u.path.rstrip("/"), "id": tenant.id}

    def _value(self, scope: str, name: str) -> str:
        if scope == "inputs":
            if name not in self.inputs:
                raise KeyError(f"missing input '{name}'")
            return self.inputs[name]
        if scope == "tenant":
            return self.tenant_vars[name]
        if scope == "secrets":
            ref = self.tenant.secrets.get(name)
            if not ref:
                raise SecretError(f"tenant {self.tenant.id} has no secret '{name}'")
            return self._secret_resolver(ref)
        raise KeyError(scope)

    def __call__(self, s: str) -> str:
        return TEMPLATE_RE.sub(lambda m: self._value(m.group(1), m.group(2)), s)

    def regex(self, s: str) -> str:
        """Render a regex template: substituted values are escaped."""
        return TEMPLATE_RE.sub(lambda m: re.escape(self._value(m.group(1), m.group(2))), s)

    def uses_secret(self, s: str) -> bool:
        return any(scope == "secrets" for scope, _ in TEMPLATE_RE.findall(s))


class Templater:
    """Inverse of Renderer, used at *record* time: concrete value -> placeholder."""

    def __init__(self, params: dict[str, str], tenant: TenantConfig):
        self.params = {k: v for k, v in params.items() if v and len(v) >= 2}
        self.base_path = urlparse(tenant.base_url).path.rstrip("/")

    def text(self, s: str) -> str:
        for name, v in sorted(self.params.items(), key=lambda kv: len(kv[1]), reverse=True):
            s = s.replace(v, "{{inputs.%s}}" % name)
        return s

    def is_exact_param(self, s: str) -> bool:
        return any(s.strip() == v for v in self.params.values())

    def path_regex(self, path: str) -> str:
        """Canonicalise a concrete route into a parameterised pattern:
        /t/acme/work/member/100234 -> ^{{tenant.base_path}}/work/member/{{inputs.member_id}}(\\?.*)?$"""
        path = path.split("?", 1)[0]
        prefix = ""
        if self.base_path and path.startswith(self.base_path + "/"):
            prefix = "{{tenant.base_path}}"
            path = path[len(self.base_path):]
        templ = self.text(path)
        out = []
        for piece in re.split(r"(\{\{[^}]+\}\})", templ):
            if piece.startswith("{{"):
                out.append(piece)
            else:
                out.append(re.sub(r"\d+", r"\\d+", re.escape(piece)))
        return "^" + prefix + "".join(out) + r"(\?.*)?$"
