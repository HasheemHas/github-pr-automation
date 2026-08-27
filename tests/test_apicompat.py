"""API compatibility: routes, public symbols, signatures and schema fields."""
from __future__ import annotations

from scripts.insights.apicompat import evaluate, extract_routes, extract_symbols
from scripts.insights.changes import Line, parse_changes


def _diff(path, removed=(), added=()):
    body = "".join(f"-{line}\n" for line in removed) + "".join(f"+{line}\n" for line in added)
    return (
        f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
        f"@@ -1,{len(removed) or 1} +1,{len(added) or 1} @@\n{body}"
    )


def _evaluate(diff, config=None):
    changes = parse_changes(diff)
    return evaluate(changes, {"files": sorted(changes)}, config or {})


def _rules(result):
    return {f["rule"] for f in result["findings"]}


def _lines(path, texts):
    return [Line(path=path, line=n, text=text) for n, text in enumerate(texts, start=1)]


# -- route extraction ------------------------------------------------------

def test_routes_are_extracted_from_the_common_frameworks():
    routes = extract_routes(_lines("api.py", [
        '@app.route("/users/<int:user_id>", methods=["GET", "POST"])',
        '@router.get("/invoices/{invoice_id}")',
    ]))
    paths = {detail["path"] for detail in routes.values()}
    assert "/users/{param}" in paths
    assert "/invoices/{param}" in paths


def test_path_parameters_are_normalised_across_syntaxes():
    express = extract_routes(_lines("api.js", ["app.get('/users/:id', handler)"]))
    fastapi = extract_routes(_lines("api.py", ['@router.get("/users/{id}")']))
    assert {d["path"] for d in express.values()} == {d["path"] for d in fastapi.values()}


def test_a_removed_route_is_high_severity():
    result = _evaluate(_diff(
        "api.py",
        removed=['@router.get("/invoices/{invoice_id}")', "def get_invoice(): ..."],
        added=["# endpoint retired"],
    ))
    finding = next(f for f in result["findings"] if f["rule"] == "api-route-removed")
    assert finding["severity"] == "high"
    assert result["breaking_change"] is True


def test_an_added_route_is_informational_only():
    result = _evaluate(_diff("api.py", added=['@router.post("/invoices")']))
    assert "api-route-added" in _rules(result)
    assert result["breaking_change"] is False
    assert result["status"] in ("pass", "warn")


def test_a_route_kept_with_different_methods_is_reported_as_a_method_change():
    result = _evaluate(_diff(
        "api.py",
        removed=['@app.route("/invoices", methods=["GET", "POST"])'],
        added=['@app.route("/invoices", methods=["GET"])'],
    ))
    assert "api-route-method-changed" in _rules(result)


def test_an_unchanged_route_produces_nothing():
    result = _evaluate(_diff(
        "api.py",
        removed=['@router.get("/invoices")', "    return old()"],
        added=['@router.get("/invoices")', "    return new()"],
    ))
    assert not _rules(result) & {"api-route-removed", "api-route-method-changed"}


# -- symbol extraction -----------------------------------------------------

def test_public_symbols_are_extracted_and_private_ones_are_not():
    symbols = extract_symbols(_lines("mod.py", [
        "def public_thing(a, b=1):",
        "def _private_thing():",
        "class PublicClass:",
    ]))
    names = {detail["name"] for detail in symbols.values()}
    assert names == {"public_thing", "PublicClass"}


def test_a_deeply_nested_definition_is_not_a_public_symbol():
    symbols = extract_symbols(_lines("mod.py", ["            def inner():"]))
    assert symbols == {}


def test_a_removed_public_function_is_reported():
    result = _evaluate(_diff("mod.py", removed=["def calculate_total(items):"]))
    finding = next(f for f in result["findings"] if f["rule"] == "api-symbol-removed")
    assert "calculate_total" in finding["title"]
    assert "false positive" in finding["detail"]


def test_a_symbol_moved_to_another_changed_file_is_only_a_low_note():
    diff = (
        _diff("old_mod.py", removed=["def calculate_total(items):"])
        + _diff("new_mod.py", added=["def calculate_total(items):"])
    )
    result = _evaluate(diff)
    assert "api-symbol-moved" in _rules(result)
    assert "api-symbol-removed" not in _rules(result)


def test_adding_a_required_parameter_is_high_severity():
    result = _evaluate(_diff(
        "mod.py",
        removed=["def calculate_total(items):"],
        added=["def calculate_total(items, currency):"],
    ))
    finding = next(f for f in result["findings"] if f["rule"] == "api-required-param-added")
    assert finding["severity"] == "high"
    assert "currency" in finding["title"]


def test_adding_an_optional_parameter_is_not_breaking():
    result = _evaluate(_diff(
        "mod.py",
        removed=["def calculate_total(items):"],
        added=['def calculate_total(items, currency="GBP"):'],
    ))
    assert "api-required-param-added" not in _rules(result)
    assert result["breaking_change"] is False


def test_removing_a_parameter_is_reported():
    result = _evaluate(_diff(
        "mod.py",
        removed=["def calculate_total(items, currency):"],
        added=["def calculate_total(items):"],
    ))
    assert "api-param-removed" in _rules(result)


def test_an_exported_javascript_function_is_tracked():
    result = _evaluate(_diff(
        "src/api.ts",
        removed=["export function fetchInvoice(id) {"],
        added=["// removed"],
    ))
    assert "api-symbol-removed" in _rules(result)


def test_a_php_public_method_is_tracked():
    result = _evaluate(_diff(
        "src/Service.php",
        removed=["    public function calculateTotal($items)"],
        added=["    public function calculateTotal($items, $currency)"],
    ))
    assert "api-required-param-added" in _rules(result)


def test_a_go_exported_function_is_tracked_and_an_unexported_one_is_not():
    exported = _evaluate(_diff("api.go", removed=["func GetInvoice(id string) error {"]))
    unexported = _evaluate(_diff("api.go", removed=["func getInvoice(id string) error {"]))
    assert "api-symbol-removed" in _rules(exported)
    assert "api-symbol-removed" not in _rules(unexported)


# -- schema fields ---------------------------------------------------------

def test_a_removed_pydantic_field_is_reported():
    result = _evaluate(_diff(
        "schemas.py",
        removed=["    legacy_code: str = ''"],
        added=["    # legacy_code dropped"],
    ))
    assert "api-field-removed" in _rules(result)


def test_a_field_becoming_required_is_high_severity():
    result = _evaluate(_diff(
        "schemas.py",
        removed=["    currency: Optional[str] = None"],
        added=["    currency: str"],
    ))
    finding = next(f for f in result["findings"] if f["rule"] == "api-field-now-required")
    assert finding["severity"] == "high"


def test_a_typescript_optional_marker_is_honoured():
    result = _evaluate(_diff(
        "src/types.ts",
        removed=["  currency?: string"],
        added=["  currency: string"],
    ))
    assert "api-field-now-required" in _rules(result)


def test_a_graphql_required_field_addition_is_reported():
    result = _evaluate(_diff("schema.graphql", added=["  currency: String!"]))
    assert "api-required-field-added" in _rules(result)


def test_a_contract_file_change_is_always_noted():
    result = _evaluate(_diff("openapi.yaml", added=["  /invoices:"]))
    assert "api-contract-file-changed" in _rules(result)
    assert result["contract_files"] == ["openapi.yaml"]


def test_a_removed_field_from_a_contract_file_is_high_severity():
    result = _evaluate(_diff("schema.graphql", removed=["  legacyCode: String"]))
    finding = next(f for f in result["findings"] if f["rule"] == "api-field-removed")
    assert finding["severity"] == "high"


# -- scoping ---------------------------------------------------------------

def test_test_files_are_not_part_of_the_public_surface():
    result = _evaluate(_diff("tests/test_api.py", removed=["def test_thing():"]))
    assert result["status"] == "pass"
    assert result["findings"] == []


def test_public_paths_narrows_the_check():
    diff = (
        _diff("internal_tool.py", removed=["def helper():"])
        + _diff("public_api.py", removed=["def endpoint():"])
    )
    result = _evaluate(diff, {"api_policy": {"public_paths": ["public_api.py"]}})
    titles = " ".join(f["title"] for f in result["findings"])
    assert "endpoint" in titles
    assert "helper" not in titles


def test_internal_paths_can_be_extended():
    result = _evaluate(
        _diff("app/legacy/thing.py", removed=["def old_helper():"]),
        {"api_policy": {"internal_paths": ["app/legacy/**"]}},
    )
    assert result["findings"] == []


# -- the envelope ----------------------------------------------------------

def test_a_breaking_change_is_advisory_by_default():
    result = _evaluate(_diff("api.py", removed=['@router.get("/invoices")']))
    assert result["status"] == "warn"
    assert result["breaking_change"] is True


def test_failing_can_be_switched_on():
    result = _evaluate(
        _diff("api.py", removed=['@router.get("/invoices")']),
        {"api_policy": {"fail_on": "high"}},
    )
    assert result["status"] == "fail"


def test_individual_checks_can_be_disabled():
    diff = _diff("api.py", removed=['@router.get("/invoices")', "def get_invoices():"])
    result = _evaluate(diff, {"api_policy": {"check_routes": False}})
    assert "api-route-removed" not in _rules(result)
    assert "api-symbol-removed" in _rules(result)


def test_the_check_can_be_disabled_entirely():
    result = _evaluate(
        _diff("api.py", removed=['@router.get("/invoices")']),
        {"api_policy": {"enabled": False}},
    )
    assert result["status"] == "not_run"


def test_the_note_warns_that_removals_may_be_moves():
    result = _evaluate(_diff("api.py", removed=['@router.get("/invoices")']))
    assert "cannot be distinguished" in result["note"]
