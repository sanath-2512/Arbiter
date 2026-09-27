"""Zero-config resolution for DeepSeek and Qwen keys and for self-hosted OpenAI-compatible servers."""

from arbiter.config import ConfigError, load_profile
from arbiter.models.base import ErrorClass, ModelError
from arbiter.resolve import resolve
from tests.helpers import ROOT, TempDirCase

HEX_KEY = "sk-" + "0123456789abcdef" * 2
DEEPSEEK = "https://api.deepseek.com"
INTL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"


def lister_for(table):
    """base_url -> list of models, or an exception to raise; records calls."""
    calls = []

    def lister(cfg, key):
        calls.append(cfg.base_url)
        v = table.get(cfg.base_url, ModelError(ErrorClass.AUTH, "Incorrect API key provided.", status=401))
        if isinstance(v, Exception):
            raise v
        return v
    return lister, calls


class FamilyResolutionTest(TempDirCase):
    def setUp(self):
        super().setUp()
        self.profile = load_profile(ROOT / "profiles" / "default.toml", env={})

    def test_deepseek_key(self):
        lister, calls = lister_for({DEEPSEEK: ["deepseek-v4-pro", "deepseek-flash"]})
        r = resolve(self.profile, HEX_KEY, lister=lister)
        self.assertEqual((r.model.base_url, r.model.name), (DEEPSEEK, "deepseek-v4-pro"))
        self.assertEqual(calls, [DEEPSEEK])  # nothing else contacted
        self.assertEqual((r.model.reasoning_passback, r.model.max_output_tokens), ("auto", 32768))
        self.assertNotIn("X-DashScope-CacheControl", r.model.extra_headers)

    def test_dashscope_key_with_the_same_format(self):
        lister, calls = lister_for({INTL: ["qwen-plus", "qwen3-coder-plus", "qwen3.8-max"]})
        r = resolve(self.profile, HEX_KEY, lister=lister)
        self.assertEqual((r.model.base_url, r.model.name), (INTL, "qwen3.8-max"))
        self.assertEqual(calls, [DEEPSEEK, INTL])  # stops at the first endpoint that knows the key
        self.assertEqual(r.model.extra_headers.get("X-DashScope-CacheControl"), "enable")
        self.assertTrue(any("not accepted by DeepSeek" in n for n in r.notes), r.notes)

    def test_key_known_nowhere_fails_before_work(self):
        lister, calls = lister_for({})
        with self.assertRaises(ConfigError) as cm:
            resolve(self.profile, HEX_KEY, lister=lister)
        self.assertIn("tried DeepSeek", str(cm.exception))
        self.assertEqual(len(calls), 6)

    def test_quota_at_the_right_vendor_stops_there(self):
        lister, calls = lister_for({DEEPSEEK: ModelError(ErrorClass.QUOTA, "Insufficient Balance", status=402)})
        with self.assertRaises(ConfigError):
            resolve(self.profile, HEX_KEY, lister=lister)
        self.assertEqual(calls, [DEEPSEEK])

    def test_endpoint_without_model_list_is_probed(self):
        lister, _ = lister_for({DEEPSEEK: ModelError(ErrorClass.UNSUPPORTED, "Not Found", status=404),
                                INTL: ModelError(ErrorClass.UNSUPPORTED, "Not Found", status=404)})
        probes = []

        def prober(cfg, key, model):
            probes.append((cfg.base_url, model))
            return "auth" if cfg.base_url == DEEPSEEK else "ok"

        r = resolve(self.profile, HEX_KEY, lister=lister, prober=prober)
        self.assertEqual((r.model.base_url, r.model.name), (INTL, "qwen3.8-max"))
        self.assertEqual(probes, [(DEEPSEEK, "deepseek-v4-pro"), (INTL, "qwen3.8-max")])

    def test_coding_plan_key(self):
        lister, calls = lister_for({"https://coding.dashscope.aliyuncs.com/v1": ["qwen3-coder-plus"]})
        r = resolve(self.profile, "sk-sp-" + "a" * 32, lister=lister)
        self.assertEqual((r.model.name, calls), ("qwen3-coder-plus", ["https://coding-intl.dashscope.aliyuncs.com/v1",
                                                                     "https://coding.dashscope.aliyuncs.com/v1"]))

    def test_self_hosted_server_any_key(self):
        profile = load_profile(ROOT / "profiles" / "default.toml", env={"AI_BASE_URL": "http://gpu-box:8000/v1"})
        lister, calls = lister_for({"http://gpu-box:8000/v1": ["BAAI/bge-m3", "Qwen/Qwen3-Coder-30B-A3B-Instruct"]})
        r = resolve(profile, "EMPTY", lister=lister)
        self.assertEqual((r.model.provider, r.model.name), ("openai_chat", "Qwen/Qwen3-Coder-30B-A3B-Instruct"))
        self.assertEqual(calls, ["http://gpu-box:8000/v1"])

    def test_self_hosted_server_with_a_recognised_key_format(self):
        profile = load_profile(ROOT / "profiles" / "default.toml", env={"AI_BASE_URL": "http://gpu-box:8000/v1"})
        lister, calls = lister_for({"http://gpu-box:8000/v1": ["deepseek-ai/DeepSeek-V4-Flash"]})
        r = resolve(profile, HEX_KEY, lister=lister)
        self.assertEqual((r.model.base_url, r.model.name), ("http://gpu-box:8000/v1", "deepseek-ai/DeepSeek-V4-Flash"))
        self.assertEqual(calls, ["http://gpu-box:8000/v1"])  # the key goes only where the evaluator pointed it
