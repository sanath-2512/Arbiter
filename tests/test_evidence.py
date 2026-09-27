import unittest

from arbiter.evidence import (
    VerificationRecord,
    classify_output,
    is_check_command,
    normalize_command,
    select_candidate,
    verification_status,
)


def rec(tree, outcome, key="pytest", binding="exact", i=[0]):
    i[0] += 1
    return VerificationRecord(id=f"v{i[0]}", step=i[0], tree=tree, binding=binding, command=key, check_key=key,
                              cwd="/r", exit_code=0, timed_out=False, outcome=outcome, runner="pytest", counts={},
                              detail="", output_id=None, source="agent", duration_s=1.0)


class ClassifyTest(unittest.TestCase):
    def test_pytest_variants(self):
        self.assertEqual(classify_output("===== 3 passed, 1 warning in 0.12s =====", 0).outcome, "passed")
        self.assertEqual(classify_output("..F\n=== 1 failed, 2 passed in 0.3s ===", 1).outcome, "failed")
        self.assertEqual(classify_output("3 passed in 0.05s\n", 0).outcome, "passed")  # -q
        self.assertEqual(classify_output("collected 0 items\n\n=== no tests ran in 0.01s ===", 5).outcome, "no_tests")
        self.assertEqual(classify_output("=== 4 skipped in 0.1s ===", 0).outcome, "skipped_only")
        out = ("==== ERRORS ====\n___ ERROR collecting tests/test_x.py ___\nImportError: cannot import name 'foo'\n"
               "!!! Interrupted: 1 error during collection !!!\n=== 1 error in 0.2s ===")
        self.assertEqual(classify_output(out, 2).outcome, "collection_error")
        o = classify_output("=== 2 failed, 5 passed, 1 error in 1.0s ===", 1)
        self.assertEqual((o.outcome, o.counts["failed"], o.counts["errors"]), ("failed", 2, 1))

    def test_other_runners(self):
        self.assertEqual(classify_output("Ran 3 tests in 0.001s\n\nOK (skipped=1)", 0).outcome, "passed")
        self.assertEqual(classify_output("Ran 0 tests in 0.000s\n\nNO TESTS RAN", 5).outcome, "no_tests")
        self.assertEqual(classify_output("--- FAIL: TestX (0.00s)\nFAIL\nFAIL\tgithub.com/a/b\t0.01s", 1).outcome, "failed")
        self.assertEqual(classify_output("ok  \tgithub.com/a/b\t0.01s", 0).outcome, "passed")
        self.assertEqual(classify_output("# github.com/a/b\n./x.go:3:2: undefined: foo\nFAIL\tgithub.com/a/b [build failed]", 1).outcome,
                         "collection_error")
        self.assertEqual(classify_output("test result: ok. 4 passed; 0 failed; 0 ignored; 0 measured", 0).outcome, "passed")
        self.assertEqual(classify_output("test result: FAILED. 3 passed; 1 failed; 0 ignored;", 101).outcome, "failed")
        self.assertEqual(classify_output("error[E0425]: cannot find value\nerror: could not compile `x`", 101).outcome,
                         "collection_error")
        self.assertEqual(classify_output("Tests:       1 failed, 2 passed, 3 total", 1).outcome, "failed")
        self.assertEqual(classify_output(" Tests  3 passed (3)", 0).outcome, "passed")
        self.assertEqual(classify_output("  5 passing (20ms)\n  1 failing", 1).outcome, "failed")
        self.assertEqual(classify_output("Tests run: 4, Failures: 0, Errors: 0, Skipped: 1", 0).outcome, "passed")
        self.assertEqual(classify_output("3 examples, 0 failures", 0).outcome, "passed")

    def test_exit_code_or_printed_pass_is_not_proof(self):
        self.assertEqual(classify_output("PASS\nall good", 0).outcome, "inconclusive")
        self.assertEqual(classify_output("", 0).outcome, "inconclusive")
        self.assertEqual(classify_output("=== 3 passed in 0.1s ===", 1).outcome, "inconclusive")  # e.g. coverage gate
        self.assertEqual(classify_output("=== 3 passed in 0.1s ===", None, timed_out=True).outcome, "timeout")

    def test_check_commands(self):
        for c in ["pytest -x tests/test_a.py", "cd sub && python -m pytest -q", "python3 -m unittest discover",
                  "go test ./...", "cargo test", "npm test", "npx jest foo", "tox -e py311", "./gradlew test",
                  "python runtests.py admin_views", "python manage.py test app", "make test",
                  "timeout 120 pytest -x", "FOO=1 uv run pytest tests", "source env/bin/activate && pytest"]:
            self.assertTrue(is_check_command(c), c)
        for c in ["cat tests/test_a.py", "grep -r pytest .", "ls tests", "pip install pytest", "echo test",
                  "rg 'go test' docs", "python -c 'import pytest'"]:
            self.assertFalse(is_check_command(c), c)
        self.assertEqual(normalize_command("pytest  -q tests 2>&1 | tail -n 30"), "pytest -q tests")


class SelectionTest(unittest.TestCase):
    def test_final_state_kept_without_dominating_evidence(self):
        records = [rec("t1", "failed"), rec("t2", "passed")]
        self.assertEqual(select_candidate("t2", "b", ["b", "t1", "t2"], records)[0], "t2")

    def test_earlier_candidate_dominates_degraded_final(self):
        records = [rec("t1", "passed"), rec("t1", "passed", key="lint"), rec("t2", "failed")]
        tree, reason = select_candidate("t2", "b", ["b", "t1", "t2"], records)
        self.assertEqual(tree, "t1")
        self.assertIn("dominates", reason)

    def test_no_dominance_on_conflicting_or_disjoint_evidence(self):
        records = [rec("t1", "passed", "A"), rec("t1", "failed", "B"), rec("t2", "failed", "A"), rec("t2", "passed", "B")]
        self.assertEqual(select_candidate("t2", "b", ["b", "t1", "t2"], records)[0], "t2")
        records = [rec("t1", "passed", "A"), rec("t2", "failed", "B")]
        self.assertEqual(select_candidate("t2", "b", ["b", "t1", "t2"], records)[0], "t2")

    def test_mutated_binding_and_inconclusive_are_ignored(self):
        records = [rec("t1", "passed", binding="mutated"), rec("t2", "failed"), rec("t1", "no_tests")]
        self.assertEqual(select_candidate("t2", "b", ["b", "t1", "t2"], records)[0], "t2")

    def test_latest_verdict_per_check_wins(self):
        records = [rec("t1", "failed"), rec("t1", "passed"), rec("t2", "failed")]
        self.assertEqual(select_candidate("t2", "b", ["b", "t1", "t2"], records)[0], "t1")

    def test_empty_final_recovery(self):
        records = [rec("t1", "passed"), rec("t2", "failed")]
        self.assertEqual(select_candidate("b", "b", ["b", "t1", "t2"], records)[0], "t1")
        self.assertEqual(select_candidate("b", "b", ["b", "t1", "t2"], [])[0], "t2")
        self.assertEqual(select_candidate("b", "b", ["b", "t1"], records, recover_empty_final=False)[0], "b")
        self.assertEqual(select_candidate("b", "b", ["b"], [])[0], "b")

    def test_verification_status_is_bound_to_exact_tree(self):
        records = [rec("t1", "passed")]
        self.assertEqual(verification_status(records, "t1")[0], "checks_passed")
        self.assertEqual(verification_status(records, "t2")[0], "verification_inconclusive")  # stale
        self.assertEqual(verification_status([], "t2")[0], "verification_unavailable")
        self.assertEqual(verification_status([rec("t3", "skipped_only")], "t3")[0], "verification_inconclusive")
        self.assertEqual(verification_status([rec("t4", "passed"), rec("t4", "failed", "B")], "t4")[0], "checks_failed")
