"""API compatibility checks.

Detects the changes that break a caller: an HTTP route that no longer exists, a
public function that was removed or had a required parameter added, a response
field that disappeared, a schema field that became mandatory.

Everything is derived by comparing the removed side of the diff against the added
side, so a rename shows up as one removal plus one addition and is reported as a
removal — which is exactly what it is from a caller's point of view.

The output is deliberately conservative about wording: this reports "no longer
present in the diff", not "deleted from the codebase", because a symbol moved to
another file that the pull request did not touch cannot be distinguished from a
deletion by reading a diff. Every finding says which it might be.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

from ..common.globs import matches
from .base import Finding, as_bool, as_patterns, block, result, verdict
from .changes import FileChange, Line

# -- HTTP routes -------------------------------------------------------------
#: Each pattern must expose a ``method`` and/or ``path`` group.
ROUTE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # Method-bearing decorators first: @router.get carries its method in the name,
    # and a generic pattern matching it would report every endpoint as ANY.
    ("python-decorator", re.compile(
        r"""@(?:\w+\.)?(?P<method>get|post|put|patch|delete|head|options)\s*\(\s*"""
        r"""['"](?P<path>[^'"]+)['"]""", re.IGNORECASE)),
    ("flask", re.compile(
        r"""@(?:\w+\.)?route\s*\(\s*['"](?P<path>[^'"]+)['"]"""
        r"""(?:[^)]*methods\s*=\s*\[(?P<method>[^\]]*)\])?""")),
    ("django", re.compile(
        r"""(?:path|re_path|url)\s*\(\s*r?['"](?P<path>[^'"]*)['"]""")),
    ("express", re.compile(
        r"""(?:app|router|api)\.(?P<method>get|post|put|patch|delete|all)\s*\(\s*"""
        r"""['"`](?P<path>/[^'"`]*)['"`]""", re.IGNORECASE)),
    ("nestjs", re.compile(
        r"""@(?P<method>Get|Post|Put|Patch|Delete)\s*\(\s*['"](?P<path>[^'"]*)['"]""")),
    ("laravel", re.compile(
        r"""Route::(?P<method>get|post|put|patch|delete|any|match)\s*\(\s*"""
        r"""['"](?P<path>[^'"]+)['"]""", re.IGNORECASE)),
    ("spring", re.compile(
        r"""@(?P<method>Get|Post|Put|Patch|Delete|Request)Mapping\s*\(\s*"""
        r"""(?:value\s*=\s*)?['"](?P<path>[^'"]*)['"]""")),
    ("go-mux", re.compile(
        r"""(?:HandleFunc|Handle)\s*\(\s*['"](?P<path>/[^'"]*)['"]""")),
    ("go-chi-gin", re.compile(
        r"""\.(?P<method>GET|POST|PUT|PATCH|DELETE)\s*\(\s*['"](?P<path>/[^'"]*)['"]""")),
    ("rails", re.compile(
        r"""^\s*(?P<method>get|post|put|patch|delete)\s+['"](?P<path>[^'"]+)['"]""")),
    ("openapi", re.compile(
        r"""^\s{2,}(?P<path>/[A-Za-z0-9{}/_.-]*)\s*:\s*$""")),
)

# -- public symbols ----------------------------------------------------------
#: language -> (pattern with a ``name`` group and optional ``params`` group)
SYMBOL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("python-def", re.compile(
        r"""^(?P<indent>\s*)(?:async\s+)?def\s+(?P<name>[A-Za-z_]\w*)\s*\((?P<params>[^)]*)""")),
    ("python-class", re.compile(r"""^(?P<indent>\s*)class\s+(?P<name>[A-Za-z_]\w*)""")),
    ("js-export-fn", re.compile(
        r"""^\s*export\s+(?:default\s+)?(?:async\s+)?function\s+(?P<name>\w+)\s*"""
        r"""\((?P<params>[^)]*)""")),
    ("js-export-const", re.compile(
        r"""^\s*export\s+(?:const|let|var|class)\s+(?P<name>\w+)""")),
    ("js-export-list", re.compile(r"""^\s*export\s*\{(?P<name>[^}]*)\}""")),
    ("ts-interface", re.compile(
        r"""^\s*export\s+(?:interface|type|enum)\s+(?P<name>\w+)""")),
    ("php-public", re.compile(
        r"""^\s*(?:public\s+|abstract\s+public\s+)(?:static\s+)?function\s+(?P<name>\w+)\s*"""
        r"""\((?P<params>[^)]*)""")),
    ("php-class", re.compile(r"""^\s*(?:final\s+|abstract\s+)?class\s+(?P<name>\w+)""")),
    ("go-func", re.compile(
        r"""^func\s+(?:\([^)]*\)\s*)?(?P<name>[A-Z]\w*)\s*\((?P<params>[^)]*)""")),
    ("java-public", re.compile(
        r"""^\s*public\s+(?:static\s+|final\s+|synchronized\s+)*[\w<>\[\],.\s]+\s+"""
        r"""(?P<name>\w+)\s*\((?P<params>[^)]*)""")),
    ("ruby-def", re.compile(r"""^\s*def\s+(?P<name>[a-z_]\w*[?!]?)""")),
)

#: A leading underscore, or a directory that is not part of any public surface.
_PRIVATE_NAME = re.compile(r"\A_(?!_init__)")

DEFAULT_INTERNAL_PATHS = (
    "tests/**", "test/**", "**/tests/**", "**/test/**", "**/__tests__/**",
    "**/*_test.*", "**/test_*.*", "**/*.test.*", "**/*.spec.*",
    "**/migrations/**", "**/node_modules/**", "vendor/**", "**/vendor/**",
    "**/internal/**", "**/private/**", "docs/**", "**/*.md",
)

#: Schema and contract files whose change is a contract change by definition.
CONTRACT_PATTERNS = (
    "**/openapi*.y*ml", "**/openapi*.json", "**/swagger*.y*ml", "**/swagger*.json",
    "**/*.proto", "**/*.graphql", "**/*.gql", "**/schema.graphql",
    "**/asyncapi*.y*ml", "**/*.avsc", "**/*.thrift", "**/wsdl/**",
)

# -- schema field shapes -----------------------------------------------------
_PYDANTIC_FIELD = re.compile(
    r"""^\s{2,}(?P<name>[a-z_]\w*)\s*:\s*(?P<type>[^=\n]+?)(?P<default>\s*=\s*.+)?$""",
    re.IGNORECASE,
)
_TS_FIELD = re.compile(r"""^\s+(?P<name>\w+)(?P<optional>\?)?\s*:\s*(?P<type>[^;,\n]+)""")
_SERIALIZER_FIELD = re.compile(
    r"""^\s+(?P<name>\w+)\s*=\s*serializers\.\w+\((?P<args>[^)]*)\)""")
_GRAPHQL_FIELD = re.compile(r"""^\s+(?P<name>\w+)\s*:\s*(?P<type>[\w\[\]]+)(?P<required>!)?""")
_PROTO_FIELD = re.compile(
    r"""^\s+(?:optional\s+|repeated\s+|required\s+)?[\w.]+\s+(?P<name>\w+)\s*=\s*(?P<tag>\d+)""")

DEFAULTS = {
    "enabled": True,
    "fail_on": "",              # advisory by default
    "check_routes": True,
    "check_symbols": True,
    "check_schemas": True,
    "max_reported": 80,
}


def _normalise_route(path: str) -> str:
    """Collapse path parameters so ``/users/{id}`` and ``/users/:id`` compare equal."""
    text = (path or "").strip()
    text = re.sub(r"\{[^}]*\}", "{param}", text)
    text = re.sub(r":[A-Za-z_]\w*", "{param}", text)
    text = re.sub(r"<[^>]*>", "{param}", text)
    text = re.sub(r"\(\?P<[^>]*>[^)]*\)", "{param}", text)
    text = re.sub(r"/+", "/", text)
    return text.rstrip("/") or "/"


def _methods(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    found = re.findall(r"[A-Za-z]+", raw)
    return tuple(sorted({m.upper() for m in found if m.upper() in
                         {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "ANY",
                          "ALL", "REQUEST"}}))


def extract_routes(lines: Iterable[Line]) -> dict[str, dict[str, Any]]:
    """``{route_key: detail}`` for every route declaration in the given lines."""
    routes: dict[str, dict[str, Any]] = {}
    for line in lines:
        for framework, pattern in ROUTE_PATTERNS:
            match = pattern.search(line.text)
            if not match:
                continue
            groups = match.groupdict()
            raw_path = (groups.get("path") or "").strip()
            if not raw_path or not raw_path.startswith(("/", "^", "")):
                continue
            if framework == "openapi" and not raw_path.startswith("/"):
                continue
            normalised = _normalise_route(raw_path)
            if normalised in ("/", "") and framework != "openapi":
                # A bare mount point is not a distinct endpoint worth reporting.
                continue
            methods = _methods(groups.get("method"))
            key = f"{'|'.join(methods) or 'ANY'} {normalised}"
            routes.setdefault(key, {
                "framework": framework, "methods": list(methods) or ["ANY"],
                "path": normalised, "raw_path": raw_path,
                "file": line.path, "line": line.line,
            })
            break
    return routes


def extract_symbols(lines: Iterable[Line]) -> dict[str, dict[str, Any]]:
    """``{symbol_key: detail}`` for every public symbol declaration."""
    symbols: dict[str, dict[str, Any]] = {}
    for line in lines:
        for language, pattern in SYMBOL_PATTERNS:
            match = pattern.match(line.text)
            if not match:
                continue
            groups = match.groupdict()
            raw_name = (groups.get("name") or "").strip()
            if not raw_name:
                continue
            names = (
                [n.strip().split(" as ")[0].strip() for n in raw_name.split(",")]
                if language == "js-export-list" else [raw_name]
            )
            for name in names:
                if not name or _PRIVATE_NAME.match(name):
                    continue
                # A nested definition is an implementation detail, not a public symbol.
                if groups.get("indent") and len(groups["indent"]) > 4:
                    continue
                params = _parameters(groups.get("params"))
                symbols[f"{line.path}::{name}"] = {
                    "name": name, "language": language, "file": line.path,
                    "line": line.line, "params": params,
                    "required_params": [p for p in params if "=" not in p],
                }
            break
    return symbols


def _parameters(raw: str | None) -> list[str]:
    """Split a parameter list, ignoring the receiver and type annotations."""
    if not raw:
        return []
    parts = [p.strip() for p in re.split(r",(?![^\[\](){}<>]*[\]\)}>])", raw) if p.strip()]
    return [p for p in parts if p not in ("self", "cls", "*", "/", "&$this")]


def _route_findings(removed: Mapping[str, Any], added: Mapping[str, Any]) -> list[Finding]:
    findings: list[Finding] = []
    gone = [key for key in removed if key not in added]
    # Compare on path alone too: a method-only change is its own kind of break.
    added_paths = {detail["path"] for detail in added.values()}
    for key in sorted(gone):
        detail = removed[key]
        path_survives = detail["path"] in added_paths
        findings.append(Finding(
            "medium" if path_survives else "high",
            "api-route-removed" if not path_survives else "api-route-method-changed",
            (f"Route `{key}` no longer declared"
             if not path_survives else
             f"Route `{detail['path']}` still exists but its methods changed"),
            (f"Detected via the {detail['framework']} pattern in `{detail['file']}`. "
             + ("The path is still declared elsewhere in the diff, so this is likely a "
                "method or handler change rather than a removal. "
                if path_survives else
                "If the endpoint moved to a file this pull request does not touch, this is a "
                "false positive. If it was genuinely removed, every existing caller breaks: "
                "deprecate it for a release first, or version the endpoint. ")
             + "Check for external consumers before merging."),
            path=detail["file"], line=detail["line"],
        ))
    new = [key for key in added if key not in removed]
    if new:
        findings.append(Finding(
            "info", "api-route-added",
            f"{len(new)} new route(s)",
            ", ".join(f"`{key}`" for key in sorted(new)[:15])
            + ". Additions are backwards compatible; confirm authentication and input "
            "validation are in place.",
        ))
    return findings


def _symbol_findings(removed: Mapping[str, Any], added: Mapping[str, Any]) -> list[Finding]:
    findings: list[Finding] = []
    added_names = {detail["name"] for detail in added.values()}

    for key, detail in sorted(removed.items()):
        if key in added:
            continue
        if detail["name"] in added_names:
            # Same name, different file: a move, which callers survive if the
            # package re-exports it.
            findings.append(Finding(
                "low", "api-symbol-moved",
                f"`{detail['name']}` moved out of `{detail['file']}`",
                "The same name is declared elsewhere in this change. Confirm the old import "
                "path still resolves, or that every caller was updated.",
                path=detail["file"], line=detail["line"],
            ))
            continue
        findings.append(Finding(
            "medium", "api-symbol-removed",
            f"Public {_kind(detail['language'])} `{detail['name']}` no longer declared",
            f"Removed from `{detail['file']}`. If it was re-exported or moved to a file this "
            "pull request does not touch, this is a false positive; otherwise every external "
            "caller breaks. Deprecate before removing where the symbol is part of a published "
            "interface.",
            path=detail["file"], line=detail["line"],
        ))

    # Signature changes: same file, same name, different required parameters.
    for key, new_detail in sorted(added.items()):
        old_detail = removed.get(key)
        if not old_detail:
            continue
        old_required = old_detail["required_params"]
        new_required = new_detail["required_params"]
        if old_required == new_required:
            continue
        gained = [p for p in new_required if p not in old_required]
        lost = [p for p in old_required if p not in new_required]
        if len(new_required) > len(old_required) and gained:
            findings.append(Finding(
                "high", "api-required-param-added",
                f"`{new_detail['name']}` gained required parameter(s): "
                f"{', '.join(gained)}",
                "Every existing call site breaks. Give the parameter a default, or add an "
                "overload, unless every caller is inside this change.",
                path=new_detail["file"], line=new_detail["line"],
            ))
        elif lost:
            findings.append(Finding(
                "medium", "api-param-removed",
                f"`{new_detail['name']}` dropped parameter(s): {', '.join(lost)}",
                "A caller passing the old argument now fails. Accept and ignore it for one "
                "release if the function is part of a published interface.",
                path=new_detail["file"], line=new_detail["line"],
            ))
    return findings


def _kind(language: str) -> str:
    if "class" in language:
        return "class"
    if "interface" in language:
        return "type"
    return "function"


def _schema_findings(changes: Mapping[str, FileChange]) -> list[Finding]:
    """Field-level changes in models, serialisers and schema files."""
    findings: list[Finding] = []
    for path, change in sorted(changes.items()):
        if change.is_binary:
            continue
        is_contract = any(matches(pattern, path) for pattern in CONTRACT_PATTERNS)
        added = _fields(change.added)
        removed = _fields(change.removed)

        gone = sorted(set(removed) - set(added))
        if gone:
            findings.append(Finding(
                "high" if is_contract else "medium",
                "api-field-removed",
                f"{len(gone)} field(s) removed from `{path}`",
                ", ".join(f"`{name}`" for name in gone[:15])
                + ". A removed response field breaks any consumer reading it; a removed request "
                "field breaks any consumer sending it. Keep it and ignore it for one release "
                "where the schema is published.",
                path=path,
            ))

        for name, detail in sorted(added.items()):
            old = removed.get(name)
            if old is None:
                if detail["required"] and not change.is_new:
                    findings.append(Finding(
                        "high", "api-required-field-added",
                        f"Required field `{name}` added to `{path}`",
                        "An existing client that does not send this field now gets a validation "
                        "error. Add it as optional with a default, then tighten it in a later "
                        "release.",
                        path=path,
                    ))
                continue
            if detail["required"] and not old["required"]:
                findings.append(Finding(
                    "high", "api-field-now-required",
                    f"Field `{name}` in `{path}` became required",
                    "Previously optional. Every client omitting it starts failing at the "
                    "moment this deploys.",
                    path=path,
                ))
            elif old["type"] and detail["type"] and old["type"] != detail["type"]:
                findings.append(Finding(
                    "medium", "api-field-type-changed",
                    f"Field `{name}` in `{path}` changed type: "
                    f"{old['type']} → {detail['type']}",
                    "A type change is breaking unless the new type is a superset of the old "
                    "one. Confirm every consumer parses both.",
                    path=path,
                ))

        # A contract file is worth naming whenever it changes at all, whether or
        # not individual fields could be parsed out of it: the file itself is the
        # published interface.
        if is_contract and (change.added or change.removed):
            findings.append(Finding(
                "info", "api-contract-file-changed",
                f"Published contract file changed: `{path}`",
                "This file is the interface other teams build against. Regenerate any client "
                "SDKs and bump the contract version.",
                path=path,
            ))
    return findings


def _fields(lines: Iterable[Line]) -> dict[str, dict[str, Any]]:
    """Best-effort field extraction across the common schema syntaxes."""
    fields: dict[str, dict[str, Any]] = {}
    for line in lines:
        text = line.text
        match = _GRAPHQL_FIELD.match(text)
        if match and line.path.endswith((".graphql", ".gql")):
            fields[match.group("name")] = {
                "type": match.group("type"), "required": bool(match.group("required")),
                "line": line.line,
            }
            continue
        match = _PROTO_FIELD.match(text)
        if match and line.path.endswith(".proto"):
            fields[match.group("name")] = {
                "type": "", "required": False, "line": line.line, "tag": match.group("tag"),
            }
            continue
        match = _SERIALIZER_FIELD.match(text)
        if match:
            args = match.group("args") or ""
            fields[match.group("name")] = {
                "type": "", "line": line.line,
                "required": "required=False" not in args and "default=" not in args,
            }
            continue
        match = _TS_FIELD.match(text)
        if match and line.path.endswith((".ts", ".tsx", ".d.ts")):
            fields[match.group("name")] = {
                "type": match.group("type").strip(),
                "required": not match.group("optional"), "line": line.line,
            }
            continue
        match = _PYDANTIC_FIELD.match(text)
        if match and line.path.endswith(".py"):
            name = match.group("name")
            if name in ("self", "cls", "class", "def", "return", "import"):
                continue
            type_text = match.group("type").strip()
            default = match.group("default") or ""
            optional = (
                bool(default)
                and "Field(..." not in default
                and "= Field(...)" not in default
            ) or "Optional[" in type_text or type_text.endswith("| None")
            fields[name] = {"type": type_text, "required": not optional, "line": line.line}
    return fields


def evaluate(
    changes: Mapping[str, FileChange],
    context: Mapping[str, Any] | None = None,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Compare the two sides of the diff for breaking interface changes."""
    policy = {**DEFAULTS, **block(config, "api_policy")}
    if not as_bool(policy.get("enabled"), True):
        return result("not_run", "API compatibility checks are disabled for this repository.")

    internal = list(DEFAULT_INTERNAL_PATHS) + as_patterns(policy.get("internal_paths"))
    public_only = as_patterns(policy.get("public_paths"))
    relevant = {
        path: change for path, change in changes.items()
        if not change.is_binary
        and not any(matches(pattern, path) for pattern in internal)
        and (not public_only or any(matches(pattern, path) for pattern in public_only))
    }
    if not relevant:
        return result("pass", "No files in the public surface changed.", (),
                      routes={"added": [], "removed": []}, symbols={"removed": []})

    added_lines = [line for change in relevant.values() for line in change.added]
    removed_lines = [line for change in relevant.values() for line in change.removed]

    findings: list[Finding] = []
    routes_added: dict[str, Any] = {}
    routes_removed: dict[str, Any] = {}
    symbols_added: dict[str, Any] = {}
    symbols_removed: dict[str, Any] = {}

    if as_bool(policy.get("check_routes"), True):
        routes_added = extract_routes(added_lines)
        routes_removed = extract_routes(removed_lines)
        findings.extend(_route_findings(routes_removed, routes_added))

    if as_bool(policy.get("check_symbols"), True):
        symbols_added = extract_symbols(added_lines)
        symbols_removed = extract_symbols(removed_lines)
        findings.extend(_symbol_findings(symbols_removed, symbols_added))

    if as_bool(policy.get("check_schemas"), True):
        findings.extend(_schema_findings(relevant))

    breaking = [f for f in findings if f.severity in ("critical", "high")]
    fail_on = str(policy.get("fail_on") or "").strip().lower()
    status = verdict(findings, fail_at=fail_on, warn_at="medium")
    max_reported = int(policy.get("max_reported") or DEFAULTS["max_reported"])

    return result(
        status,
        (f"{len(breaking)} likely breaking change(s), {len(findings) - len(breaking)} other "
         f"interface change(s)" if findings else "No interface changes detected"),
        findings[:max_reported],
        breaking_change=bool(breaking),
        routes={
            "added": sorted(routes_added),
            "removed": sorted(key for key in routes_removed if key not in routes_added),
        },
        symbols={
            "removed": sorted(
                detail["name"] for key, detail in symbols_removed.items()
                if key not in symbols_added
            )[:50],
        },
        contract_files=sorted(
            path for path in relevant if any(matches(p, path) for p in CONTRACT_PATTERNS)
        ),
        note=(
            "Derived from the diff only. A symbol that moved to an untouched file cannot be "
            "distinguished from a deletion, so confirm before acting on a removal finding."
        ),
    )
