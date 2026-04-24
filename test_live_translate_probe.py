import importlib.util
import sys
import unittest
from pathlib import Path


_SCRIPT_PATH = Path(__file__).resolve().parent / "testing" / "test_live_translate_mac.py"
_SPEC = importlib.util.spec_from_file_location("tv_live_translate_probe", _SCRIPT_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"Unable to load probe script from {_SCRIPT_PATH}")
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)


class LiveTranslateProbeTests(unittest.TestCase):
    def test_select_source_language_prefers_supported_detection(self):
        cached, source = _MODULE._select_source_language("de", 0.91, None, 0.65)

        self.assertEqual(cached, "de")
        self.assertEqual(source, "de")

    def test_select_source_language_reuses_cached_supported_language_on_low_confidence(self):
        cached, source = _MODULE._select_source_language("en", 0.18, "es", 0.65)

        self.assertEqual(cached, "es")
        self.assertEqual(source, "es")

    def test_select_source_language_clears_cached_language_on_confident_english(self):
        cached, source = _MODULE._select_source_language("en", 0.96, "de", 0.65)

        self.assertIsNone(cached)
        self.assertIsNone(source)


if __name__ == "__main__":
    unittest.main()