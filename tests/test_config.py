from gheerefill.config import ConfigError, apply_task_limits, load_profile, profile_from_dict, validate
from tests.helpers import ROOT, TempDirCase


class ConfigTest(TempDirCase):
    def write(self, text):
        p = self.tmp / "p.toml"
        p.write_text(text)
        return p

    def test_default_profile_refuses_unset_model(self):
        prof = load_profile(ROOT / "profiles" / "default.toml", env={})
        with self.assertRaises(ConfigError) as cm:
            validate(prof)
        self.assertIn("never substitutes", str(cm.exception))

    def test_env_overrides_recorded(self):
        prof = load_profile(ROOT / "profiles" / "default.toml",
                            env={"AI_MODEL": "m-1", "AI_BASE_URL": "https://x/v1", "AI_API_KEY": "secret"})
        validate(prof)
        self.assertEqual(prof.model.name, "m-1")
        self.assertEqual(prof.overrides, {"model.name": "AI_MODEL", "model.base_url": "AI_BASE_URL"})
        self.assertNotIn("secret", str(prof.to_dict()))

    def test_typos_and_types_rejected(self):
        for text, needle in [
            ("[model]\nnmae = 'x'\n", "unknown key [model].nmae"),
            ("[limits]\nmax_steps = 'ten'\n", "must be an integer"),
            ("[bogus]\n", "unknown profile section"),
            ("[model]\nprovider = 'gemini'\nname='a'\nbase_url='https://a'\n", "provider must be one of"),
            ("[model\n", "invalid TOML"),
        ]:
            with self.assertRaises(ConfigError) as cm:
                validate(load_profile(self.write(text), env={}))
            self.assertIn(needle, str(cm.exception))

    def test_task_limits_take_precedence_and_keep_reserve_valid(self):
        prof = load_profile(self.write("[model]\nname='m'\nbase_url='https://a'\n"), env={})
        p2 = apply_task_limits(prof, {"time_limit_s": 30, "max_steps": 5})
        self.assertEqual((p2.limits.time_limit_s, p2.limits.max_steps), (30.0, 5))
        self.assertLess(p2.limits.finalize_reserve_s, 30)
        validate(p2)
        self.assertEqual(prof.limits.max_steps, 150)  # original untouched

    def test_profile_roundtrip_identity(self):
        prof = load_profile(self.write("[model]\nname='m'\nbase_url='https://a'\n[model.pricing]\ninput=1.0\n"), env={})
        self.assertEqual(profile_from_dict(prof.to_dict()).identity(), prof.identity())
