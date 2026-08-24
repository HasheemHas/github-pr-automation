"""Label derivation from paths, PR text and AI metadata."""
from __future__ import annotations

from scripts.common.labels import derive_labels, missing_labels


def test_backend_paths_add_the_backend_label():
    assert "backend" in derive_labels(["backend/app/main.py"])


def test_frontend_and_admin_share_a_label():
    assert "frontend" in derive_labels(["frontend/src/a.tsx"])
    assert "frontend" in derive_labels(["admin/src/a.tsx"])


def test_terraform_implies_devops():
    labels = derive_labels(["terraform/main.tf"])
    assert {"terraform", "devops"} <= set(labels)


def test_helm_implies_kubernetes_and_devops():
    labels = derive_labels(["helm/Chart.yaml"])
    assert {"helm", "kubernetes", "devops"} <= set(labels)


def test_workflow_changes_are_ci_and_high_risk():
    labels = derive_labels([".github/workflows/ci.yml"])
    assert "ci" in labels
    assert "high-risk" in labels


def test_auth_changes_are_security_and_high_risk():
    labels = derive_labels(["backend/app/auth/jwt.py"])
    assert "security" in labels
    assert "high-risk" in labels


def test_a_small_ordinary_change_is_low_risk():
    labels = derive_labels(["backend/app/main.py"])
    assert "low-risk" in labels


def test_a_large_change_set_is_medium_risk():
    labels = derive_labels([f"backend/app/f{i}.py" for i in range(40)])
    assert "medium-risk" in labels


def test_the_title_contributes_labels():
    assert "fix" in derive_labels(["a.py"], title="fix: correct the thing")
    assert "feature" in derive_labels(["a.py"], title="feat: add the thing")
    assert "breaking-change" in derive_labels(["a.py"], title="breaking change: drop v1")


def test_ai_metadata_risk_wins_over_the_path_heuristic():
    labels = derive_labels(["backend/app/main.py"], metadata={"risk": "high"})
    assert "high-risk" in labels
    assert "low-risk" not in labels


def test_an_out_of_enum_ai_risk_is_ignored():
    labels = derive_labels(["backend/app/main.py"], metadata={"risk": "catastrophic"})
    assert "low-risk" in labels
    assert not any(label.endswith("-risk") and label != "low-risk" for label in labels)


def test_ai_metadata_change_type_becomes_a_label():
    assert "refactor" in derive_labels(["a.py"], metadata={"change_type": "refactor"})


def test_an_ai_breaking_change_flag_is_honoured():
    assert "breaking-change" in derive_labels(["a.py"], metadata={"breaking_change": True})


def test_absent_metadata_still_produces_labels():
    # An AI failure must not stop labelling.
    assert derive_labels(["backend/app/main.py"], metadata=None)


def test_exactly_one_risk_label_is_assigned():
    for paths in ([], ["a.py"], [".github/workflows/ci.yml"], [f"f{i}.py" for i in range(40)]):
        labels = derive_labels(paths)
        risk = [x for x in labels if x.endswith("-risk")]
        assert len(risk) == 1, (paths, risk)


def test_labels_are_sorted_and_unique():
    labels = derive_labels(["backend/app/main.py", "backend/app/other.py"])
    assert labels == sorted(set(labels))


def test_missing_labels_is_case_insensitive():
    assert missing_labels(["Backend", "new"], ["backend"]) == ["new"]
