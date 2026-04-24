import threading
import unittest

from audio_analysis.audio_forwarder import AudioForwarder


class AudioForwarderTests(unittest.TestCase):
    def test_format_caption_prefixes_source_language(self):
        self.assertEqual(
            AudioForwarder.format_caption("How are you?", "de"),
            "(German) How are you?",
        )

    def test_on_message_stores_formatted_caption(self):
        forwarder = object.__new__(AudioForwarder)
        forwarder._lock = threading.Lock()
        forwarder._captions = {}
        forwarder._results = {}

        forwarder._on_message(
            None,
            '{"type":"caption","session_key":7,"text":"How are you?","source_language":"de"}',
        )

        self.assertEqual(forwarder.get_latest_caption(7), "(German) How are you?")


if __name__ == "__main__":
    unittest.main()