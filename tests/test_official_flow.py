"""The official evaluation procedure: export AI_API_KEY; make setup; make run; then the GitHub issue /
test case is supplied to the running harness. Exercised with a fake GitHub API, a local git remote
standing in for github.com, and the scripted model (no live calls)."""

import json
import os
import pty
import select
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from gheerefill.intake import IntakeError, compose_issue_text, parse_github_ref, parse_input
from gheerefill.resolve import choose_model, resolve, rule_matches
from gheerefill.config import ConfigError, load_profile
from gheerefill.models.base import ErrorClass, ModelError
from tests.fake_openai_server import FakeOpenAIServer
from tests.helpers import CALC, ROOT, TEST_CMD, TempDirCase, git, make_repo, tc, turn

FIX = tc("edit_file", path="calc/ops.py", old_str="return a // b", new_str="return a / b")


class FakeGitHub:
    def __init__(self, issues):
        self.issues, self.requests = issues, []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.requests.append({"path": self.path, "auth": self.headers.get("Authorization")})
                parts = self.path.split("?")[0].strip("/").split("/")
                key = "/".join(parts[1:5]) if len(parts) >= 5 else ""
                issue = outer.issues.get(key)
                if issue is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                body = json.dumps(issue.get("_comments", []) if parts[-1] == "comments" else issue).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def __enter__(self):
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *a):
        self.httpd.shutdown()
        self.httpd.server_close()


def issue_json(state="open", created="2026-01-02T00:00:00Z", comments=None):
    return {"title": "divide() truncates", "body": "divide(7, 2) returns 3; expected 3.5.", "state": state,
            "created_at": created, "user": {"login": "reporter"}, "comments": len(comments or []),
            "html_url": "https://github.com/acme/calc/issues/7", "labels": [{"name": "bug"}],
            "_comments": comments or []}


class IntakeParsingTest(TempDirCase):
    def test_forms(self):
        self.assertEqual(parse_github_ref("https://github.com/acme/calc/issues/7"), ("acme", "calc", 7))
        self.assertEqual(parse_github_ref("https://github.com/acme/calc/pull/9#discussion"), ("acme", "calc", 9))
        self.assertEqual(parse_github_ref("acme/calc#7"), ("acme", "calc", 7))
        self.assertIsNone(parse_github_ref("see acme/calc#7 please"))
        reqs = parse_input("https://github.com/a/b/issues/1\nhttps://github.com/a/b/issues/2", base_dir=self.tmp)
        self.assertEqual([r.github for r in reqs], [("a", "b", 1), ("a", "b", 2)])
        (self.tmp / "issue.md").write_text("acme/calc#7\n")
        self.assertEqual(parse_input("@issue.md", base_dir=self.tmp)[0].github, ("acme", "calc", 7))
        text = "{this is not json} but an issue about dicts\nsecond line"
        self.assertEqual(parse_input(text, base_dir=self.tmp)[0].issue_text, text)
        with self.assertRaises(IntakeError):
            parse_input("@missing.md", base_dir=self.tmp)

    def test_url_followed_by_instructions_and_url_inside_text(self):
        (req,) = parse_input("https://github.com/acme/calc/issues/7\nKeep the public API unchanged.\n", base_dir=self.tmp)
        self.assertEqual((req.github, req.metadata["instructions"]), (("acme", "calc", 7), "Keep the public API unchanged."))
        text = "divide() truncates, like https://github.com/acme/calc/issues/3 did\nplease fix"
        self.assertEqual(parse_input(text, base_dir=self.tmp)[0].issue_text, text)  # a mention is not the task

    def test_images_are_announced_not_silently_dropped(self):
        from gheerefill.intake import image_note

        body = "Broken layout:\n![screenshot](https://example.com/a.png)\n<img src='b.png' width=300>\nsee above"
        self.assertIn("2 image(s)", image_note(body))
        self.assertEqual(image_note("no pictures here [link](https://x)"), "")

    def test_prepared_checkout_detection_reads_config_only(self):
        from gheerefill.intake import origin_matches

        repo = make_repo(self.tmp / "co", CALC)
        for url, ok in (("https://github.com/acme/calc.git", True), ("git@github.com:acme/calc.git", True),
                        ("https://github.com/Acme/Calc/", True), ("https://github.com/acme/calculator.git", False),
                        ("https://github.com/other/calc.git", False)):
            git(repo, "remote", "remove", "origin") if "origin" in git(repo, "remote") else None
            git(repo, "remote", "add", "origin", url)
            self.assertEqual(origin_matches(repo, "acme", "calc"), ok, url)

    def test_issue_text_includes_discussion_and_is_bounded(self):
        meta = {"title": "T", "html_url": "u", "state": "open", "created_at": "c", "labels": ["bug"], "body": "B",
                "comments": [{"author": "x", "created_at": "d", "body": "the real cause is Y"}]}
        text = compose_issue_text(meta)
        self.assertIn("the real cause is Y", text)
        meta["body"] = "z" * 100_000
        self.assertLess(len(compose_issue_text(meta)), 61_000)


class ResolutionTest(TempDirCase):
    def setUp(self):
        super().setUp()
        self.profile = load_profile(ROOT / "profiles" / "default.toml", env={})

    def test_rules_and_no_key_spraying(self):
        calls = []

        def lister(cfg, key):
            calls.append(cfg.base_url)
            return ["claude-sonnet-4-5-20250929", "claude-opus-4-1-20250805"]

        r = resolve(self.profile, "sk-ant-api03-xyz", lister=lister)
        self.assertEqual((r.model.provider, r.model.name), ("anthropic_messages", "claude-sonnet-4-5-20250929"))
        self.assertEqual(calls, ["https://api.anthropic.com"])  # exactly one endpoint contacted
        self.assertTrue(rule_matches("regex:^sk-(proj|svcacct|admin)-|T3BlbkFJ", "sk-" + "a" * 20 + "T3BlbkFJ" + "b" * 20))
        self.assertFalse(rule_matches("regex:^sk-[0-9a-f]{32}$", "sk-" + "A" * 48))

    def test_pinned_model_is_never_substituted(self):
        self.profile.model.name = "prescribed-model-x"
        r = resolve(self.profile, "sk-ant-api03-xyz", lister=lambda c, k: ["claude-sonnet-4-5"])
        self.assertEqual(r.model.name, "prescribed-model-x")
        self.assertTrue(any("never substituted" in n for n in r.notes))

    def test_auth_failure_stops_before_work_and_unavailable_prefs_refuse(self):
        def bad(cfg, key):
            raise ModelError(ErrorClass.AUTH, "invalid x-api-key")

        with self.assertRaises(ConfigError):
            resolve(self.profile, "sk-ant-api03-xyz", lister=bad)
        with self.assertRaises(ConfigError) as cm:
            resolve(self.profile, "sk-ant-api03-xyz", lister=lambda c, k: ["some-other-model"])
        self.assertIn("none of the configured models", str(cm.exception))

    def test_choose_model_prefers_order_then_newest_dated_variant(self):
        avail = ["gpt-5-2025-08-07", "gpt-5-2025-10-01", "gpt-4o"]
        self.assertEqual(choose_model(["gpt-5.1", "gpt-5", "gpt-4o"], avail), "gpt-5-2025-10-01")
        self.assertIsNone(choose_model(["gpt-5"], ["gpt-5-mini"]))  # a different model is not a variant


class OfficialFlowTest(TempDirCase):
    def setUp(self):
        super().setUp()
        self.upstream = make_repo(self.tmp / "remote" / "acme" / "calc", CALC)  # stands in for github.com/acme/calc
        self.script = self.tmp / "script.json"
        self.script.write_text(json.dumps([turn(FIX), turn(tc("bash", command=TEST_CMD)), turn(tc("submit"))]))
        self.profile = self.tmp / "p.toml"
        self.profile.write_text(f'[model]\nprovider = "fake"\nscript = "{self.script}"\n')
        self.out = self.tmp / "out"
        self.ws = self.tmp / "workspace"

    def env(self, gh, **extra):
        e = {k: v for k, v in os.environ.items() if not k.startswith(("AI_", "ISSUE", "REPO", "BASE", "GHEEREFILL_"))}
        e.update(AI_API_KEY="sk-fake-000000000000", GHEEREFILL_GITHUB_API=gh.url,
                 GHEEREFILL_GITHUB_CLONE_BASE=f"file://{self.tmp / 'remote'}", GHEEREFILL_WORKSPACE=str(self.ws),
                 GHEEREFILL_PROFILE=str(self.profile), GHEEREFILL_OUT=str(self.out),
                 no_proxy="127.0.0.1,localhost", NO_PROXY="127.0.0.1,localhost", **extra)
        return e

    def test_make_run_with_github_issue_url(self):
        with FakeGitHub({"acme/calc/issues/7": issue_json(comments=[{"user": {"login": "m"}, "created_at": "x",
                                                                      "body": "maintainer: keep divide(6,3)==2"}])}) as gh:
            p = subprocess.run(["make", "-s", "run", "ISSUE=https://github.com/acme/calc/issues/7"], cwd=ROOT,
                               env=self.env(gh), capture_output=True, text=True, timeout=180, stdin=subprocess.DEVNULL)
        self.assertEqual(p.returncode, 0, p.stderr[-3000:])
        rec = json.loads(p.stdout.strip().splitlines()[-1])
        self.assertEqual(rec["task_id"], "acme__calc-7")
        self.assertEqual(rec["verification"]["status"], "checks_passed")
        self.assertTrue(rec["submission_ready"])
        repo = Path(rec["run_dir"]).parents[1]  # sanity: run dir is outside the clone
        clone = self.ws / "acme__calc__7"
        self.assertIn("a / b", (clone / "calc" / "ops.py").read_text())  # deliverable left in the clone
        self.assertNotIn(str(clone), str(repo))
        task = json.loads((Path(rec["run_dir"]) / "task.json").read_text())
        self.assertIn("maintainer: keep divide(6,3)==2", task["issue"])  # discussion included
        self.assertTrue((Path(rec["run_dir"]) / "report.md").exists())
        self.assertTrue(any("cloned" in n for n in rec["intake"]))

    def test_closed_issue_warns_and_before_issue_base(self):
        from tests.helpers import GIT_ENV

        pre_issue = git(self.upstream, "rev-parse", "HEAD").strip()
        (self.upstream / "calc" / "ops.py").write_text("def divide(a, b):\n    return a / b  # the upstream fix\n")
        subprocess.run(["git", "commit", "-qam", "fix #7"], cwd=self.upstream, check=True,
                       env={**GIT_ENV, "GIT_COMMITTER_DATE": "2030-01-01T00:00:00Z", "GIT_AUTHOR_DATE": "2030-01-01T00:00:00Z"})
        created = "2029-06-01T00:00:00Z"  # after the base commit (now), before the fix (2030)
        with FakeGitHub({"acme/calc/issues/7": issue_json(state="closed", created=created)}) as gh:
            p = subprocess.run([sys.executable, "-m", "gheerefill", "run", "--issue", "acme/calc#7"], cwd=ROOT,
                               env=self.env(gh), capture_output=True, text=True, timeout=180, stdin=subprocess.DEVNULL)
            self.assertIn("issue is CLOSED", p.stderr)  # default branch used, with an explicit warning
            p = subprocess.run([sys.executable, "-m", "gheerefill", "run", "--issue", "acme/calc#7", "--base",
                                "before-issue"], cwd=ROOT, env=self.env(gh), capture_output=True, text=True,
                               timeout=180, stdin=subprocess.DEVNULL)
        rec = json.loads(p.stdout.strip().splitlines()[-1])
        base_note = [n for n in rec["intake"] if n.startswith("base commit:")][0]
        self.assertIn(pre_issue, base_note)  # the fix committed after the issue is not in the base
        self.assertIn("+    return a / b", (Path(rec["run_dir"]) / "patch.diff").read_text())

    def test_plain_text_issue_with_local_repo_and_missing_repo_error(self):
        local = make_repo(self.tmp / "local", CALC)
        with FakeGitHub({}) as gh:
            p = subprocess.run([sys.executable, "-m", "gheerefill", "run"], input="divide truncates; use true division\n",
                               cwd=ROOT, env=self.env(gh, REPO=str(local)), capture_output=True, text=True, timeout=180)
            rec = json.loads(p.stdout.strip().splitlines()[-1])
            self.assertEqual(rec["verification"]["status"], "checks_passed", p.stderr[-2000:])
            p = subprocess.run([sys.executable, "-m", "gheerefill", "run"], input="an issue without a repo\n",
                               cwd=ROOT, env=self.env(gh), capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 1)
        self.assertIn("needs a repository", json.loads(p.stdout)["error"]["message"])

    def test_existing_checkout_is_used_in_place_instead_of_cloning(self):
        checkout = make_repo(self.tmp / "evaluator-checkout", CALC)
        git(checkout, "remote", "add", "origin", "https://github.com/acme/calc.git")
        with FakeGitHub({"acme/calc/issues/7": issue_json()}) as gh:
            p = subprocess.run([sys.executable, "-m", "gheerefill", "run", "--issue", "acme/calc#7"], cwd=ROOT,
                               env=self.env(gh, GHEEREFILL_CALLER_DIR=str(checkout)), capture_output=True, text=True,
                               timeout=180, stdin=subprocess.DEVNULL)
        rec = json.loads(p.stdout.strip().splitlines()[-1])
        self.assertTrue(rec["submission_ready"], p.stderr[-2000:])
        self.assertIn("a / b", (checkout / "calc" / "ops.py").read_text())
        self.assertFalse(self.ws.exists() and any(self.ws.iterdir()))  # nothing cloned
        self.assertTrue(any("existing checkout" in n for n in rec["intake"]))

    def test_no_input_is_not_a_failure(self):
        with FakeGitHub({}) as gh:
            p = subprocess.run(["make", "-s", "run"], cwd=ROOT, env=self.env(gh), capture_output=True, text=True,
                               timeout=60, stdin=subprocess.DEVNULL)
        self.assertEqual(p.returncode, 0)
        self.assertIn("no task supplied", p.stderr)

    def test_interactive_terminal_session(self):
        with FakeGitHub({"acme/calc/issues/7": issue_json()}) as gh:
            pid, fd = pty.fork()
            if pid == 0:
                os.chdir(ROOT)
                os.execvpe(sys.executable, [sys.executable, "-m", "gheerefill", "run"], self.env(gh, NO_COLOR="1"))
            out = b""

            def read_until(needle: bytes, timeout: float = 60) -> None:
                nonlocal out
                end = time.time() + timeout
                while needle not in out and time.time() < end:
                    r, _, _ = select.select([fd], [], [], 0.2)
                    if r:
                        try:
                            out += os.read(fd, 65536)
                        except OSError:
                            break
                self.assertIn(needle, out, out.decode(errors="replace")[-3000:])

            read_until(b"issue> ")
            self.assertIn(b"isolation", out)
            os.write(fd, b"https://github.com/acme/calc/issues/7\n")
            read_until(b"Result: acme__calc-7")
            read_until(b"issue> ")  # ready for the next issue
            os.write(fd, b"/quit\n")
            _, status = os.waitpid(pid, 0)
            os.close(fd)
        self.assertEqual(os.waitstatus_to_exitcode(status), 0)
        self.assertIn(b"+    return a / b", out)
        self.assertIn(b"checks_passed", out)


    def _pty_session(self, gh, extra_env):
        pid, fd = pty.fork()
        if pid == 0:
            os.chdir(ROOT)
            os.execvpe(sys.executable, [sys.executable, "-m", "gheerefill", "run"], self.env(gh, NO_COLOR="1", **extra_env))
        buf = {"out": b""}

        def read_until(needle: bytes, timeout: float = 60) -> None:
            end = time.time() + timeout
            while needle not in buf["out"] and time.time() < end:
                r, _, _ = select.select([fd], [], [], 0.2)
                if r:
                    try:
                        buf["out"] += os.read(fd, 65536)
                    except OSError:
                        break
            self.assertIn(needle, buf["out"], buf["out"].decode(errors="replace")[-3000:])

        return pid, fd, buf, read_until

    def test_interactive_bracketed_paste_and_ctrl_c_at_prompt(self):
        local = make_repo(self.tmp / "local", CALC)
        with FakeGitHub({}) as gh:
            pid, fd, buf, read_until = self._pty_session(gh, {"REPO": str(local)})
            read_until(b"issue> ")
            self.assertIn(b"\x1b[?2004h", buf["out"])  # bracketed paste enabled for the prompt
            os.write(fd, b"\x1b[200~divide() truncates\n\nExpected: divide(7, 2) == 3.5\x1b[201~\n")
            read_until(b"Result: issue-")
            read_until(b"issue> ")
            os.write(fd, b"\x03")  # Ctrl-C while waiting for input: leave at once
            _, status = os.waitpid(pid, 0)
            try:
                buf["out"] += os.read(fd, 65536)
            except OSError:
                pass
            os.close(fd)
        self.assertEqual(os.waitstatus_to_exitcode(status), 0)
        self.assertIn(b"checks_passed", buf["out"])
        self.assertIn(b"\x1b[?2004l", buf["out"])  # and disabled again on the way out


class ParameterAdaptationTest(TempDirCase):
    def test_max_tokens_rejected_then_adapted(self):
        from tests.test_eval_pipeline import SCRIPT

        repo = make_repo(self.tmp / "repo", CALC)
        prof = self.tmp / "p.toml"
        prof.write_text('[model]\nprovider = "openai_chat"\nname = "m"\nbase_url = "http://127.0.0.1:1/v1"\n')
        with FakeOpenAIServer(SCRIPT) as srv:
            real = srv.httpd.RequestHandlerClass.do_POST
            state = {"n": 0}

            def picky(handler):
                n = int(handler.headers.get("Content-Length", 0))
                body = json.loads(handler.rfile.read(n))
                if "max_tokens" in body:
                    data = json.dumps({"error": {"message": "Unsupported parameter: 'max_tokens' is not supported "
                                                            "with this model. Use 'max_completion_tokens' instead."}}).encode()
                    handler.send_response(400)
                    handler.send_header("Content-Length", str(len(data)))
                    handler.end_headers()
                    handler.wfile.write(data)
                    state["n"] += 1
                    return
                import io
                handler.rfile = io.BytesIO(json.dumps(body).encode())
                handler.headers.replace_header("Content-Length", str(len(json.dumps(body))))
                real(handler)

            srv.httpd.RequestHandlerClass.do_POST = picky
            env = {**os.environ, "AI_API_KEY": "sk-fake-0000000000", "AI_BASE_URL": srv.base_url,
                   "no_proxy": "127.0.0.1", "NO_PROXY": "127.0.0.1"}
            p = subprocess.run([sys.executable, "-m", "gheerefill", "run", "--profile", str(prof), "--no-discover",
                                "--out", str(self.tmp / "out")],
                               input=json.dumps({"task_id": "a", "repo_path": str(repo), "issue": "fix divide"}) + "\n",
                               cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
        rec = json.loads(p.stdout.strip().splitlines()[-1])
        self.assertEqual(rec["termination"], "model_submitted", p.stderr[-2000:])
        self.assertEqual(state["n"], 1)
        self.assertTrue(any("max_completion_tokens" in n for n in rec["notes"]))


class CredentialFailureTest(TempDirCase):
    def test_rejected_key_mid_run_exits_nonzero_after_one_request(self):
        """With an explicit endpoint there is no model listing up front: the first request is where a
        wrong key shows. The run must stop there (no retries) and exit non-zero with a clear message."""
        from tests.test_eval_pipeline import SCRIPT

        repo = make_repo(self.tmp / "repo", CALC)
        prof = self.tmp / "p.toml"
        prof.write_text('[model]\nprovider = "openai_chat"\nname = "m"\nbase_url = "http://127.0.0.1:1/v1"\n')
        with FakeOpenAIServer(SCRIPT) as srv:
            state = {"n": 0}

            def reject(handler):
                handler.rfile.read(int(handler.headers.get("Content-Length", 0)))
                data = json.dumps({"error": {"message": "Incorrect API key provided.", "type": "invalid_request_error",
                                             "code": "invalid_api_key"}}).encode()
                handler.send_response(401)
                handler.send_header("Content-Length", str(len(data)))
                handler.end_headers()
                handler.wfile.write(data)
                state["n"] += 1

            srv.httpd.RequestHandlerClass.do_POST = reject
            env = {**os.environ, "AI_API_KEY": "sk-wrong-0000000000", "AI_BASE_URL": srv.base_url,
                   "no_proxy": "127.0.0.1", "NO_PROXY": "127.0.0.1"}
            p = subprocess.run([sys.executable, "-m", "gheerefill", "run", "--profile", str(prof), "--no-discover",
                                "--out", str(self.tmp / "out")],
                               input=json.dumps({"task_id": "a", "repo_path": str(repo), "issue": "fix divide"}) + "\n",
                               cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
        rec = json.loads(p.stdout.strip().splitlines()[-1])
        self.assertEqual(p.returncode, 2, p.stderr[-2000:])
        self.assertEqual(rec["termination"], "model_error:authentication")
        self.assertEqual(state["n"], 1)
        self.assertIn("rejected AI_API_KEY", p.stderr)
        self.assertNotIn("sk-wrong-0000000000", p.stdout + p.stderr)
        self.assertEqual(git(repo, "status", "--porcelain").strip(), "")


class DotenvTest(TempDirCase):
    def test_environment_wins_over_dotenv(self):
        from gheerefill.cli import load_dotenv

        f = self.tmp / ".env"
        f.write_text("# local\nAI_API_KEY=from-file\nEMPTY=\nexport OTHER_X='q'\n")
        from unittest import mock

        with mock.patch.dict(os.environ, {"AI_API_KEY": "from-env"}, clear=False):
            os.environ.pop("OTHER_X", None)
            loaded = load_dotenv(f)
            self.assertEqual(os.environ["AI_API_KEY"], "from-env")
            self.assertEqual(os.environ.get("OTHER_X"), "q")
            self.assertEqual(loaded, ["OTHER_X"])
            os.environ.pop("OTHER_X", None)


class ProviderLimitTest(TempDirCase):
    def test_output_cap_is_parsed_from_provider_errors(self):
        from gheerefill.models.base import output_token_limit

        cases = [
            ("max_tokens is too large: 32768. This model supports at most 16384 completion tokens, whereas you "
             "provided 32768.", 16384),
            ("max_tokens: 64000 > 32000, which is the maximum allowed number of output tokens for claude-x", 32000),
            ("Invalid max_tokens value, the valid range of max_tokens is [1, 8192]", 8192),
            ("`max_tokens` must be less than or equal to `8192`, the maximum value for `max_tokens` is less than the "
             "`context_window` for this model", 8192),
        ]
        for msg, want in cases:
            self.assertEqual(output_token_limit(msg, 65536), want, msg)
        self.assertIsNone(output_token_limit("This model's maximum context length is 128000 tokens", 8192))
        self.assertIsNone(output_token_limit("max_tokens: 9000 > 8192, which is the maximum", 8192))  # not lower

    def test_client_adapts_output_cap_without_changing_model(self):
        from gheerefill.config import ModelConfig
        from gheerefill.models.openai_chat import OpenAIChatClient

        cfg = ModelConfig(provider="openai_chat", name="m", base_url="https://x/v1", max_output_tokens=32768)
        client = OpenAIChatClient(cfg, "k")
        err = ModelError(ErrorClass.MALFORMED_REQUEST, "This model supports at most 16384 completion tokens")
        self.assertIn("16384", client.adapt(err))
        self.assertEqual((cfg.max_output_tokens, cfg.name), (16384, "m"))

    def test_restricted_key_without_model_listing_continues_unverified(self):
        profile = load_profile(ROOT / "profiles" / "default.toml", env={})

        def forbidden(cfg, key):
            raise ModelError(ErrorClass.AUTH, "You have insufficient permissions for this operation. Missing scopes: "
                                              "api.model.read", status=401)

        r = resolve(profile, "sk-proj-" + "a" * 40, lister=forbidden)
        self.assertEqual(r.model.name, "gpt-5.1")
        self.assertIn("unverified", " ".join(r.notes))
        self.assertEqual(r.model.max_output_tokens, 32768)
        a = resolve(profile, "sk-ant-api03-xyz", lister=lambda c, k: ["claude-sonnet-5"])
        self.assertTrue(a.model.prompt_cache)
