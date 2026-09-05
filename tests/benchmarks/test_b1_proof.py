from benchmarks.b1_proof.run import run_c1, run_c2


def test_b1_exact_contract_passes():
    report = run_c1()
    assert report["passed"] is True
    assert report["exact_scenarios"]["recall_percent"] == 100.0
    assert all(
        row["ids_sha256"] == row["expected_ids_sha256"]
        for row in report["exact_scenarios"]["rows"]
    )


def test_b1_state_and_fault_matrix_passes():
    report = run_c2()
    assert report["passed"] is True
    assert report["child_sitemap_partial_visible"]["entry"]["status"] == "partial"
