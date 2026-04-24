import unittest

from server.audio_ws import AudioTranscriptionHandler, _TranscriptionResult
from server.config import ServerConfig


class _StubHandler(AudioTranscriptionHandler):
    def __init__(self, cfg: ServerConfig):
        super().__init__(cfg)
        self.buffer_sizes = []
        self.transcribe_calls = []
        self.responses = []

    def _buffer_to_wav(self, buf: bytes) -> str:
        self.buffer_sizes.append(len(buf))
        return f"fake-{len(self.buffer_sizes)}.wav"

    def _cleanup_wav(self, wav_path: str) -> None:
        return None

    def _transcribe_wav(self, wav_path: str, *, task: str = "transcribe", language=None):
        self.transcribe_calls.append((task, language))
        if not self.responses:
            raise AssertionError("No stub response available")
        return self.responses.pop(0)


class ServerAudioWsTests(unittest.TestCase):
    def _make_cfg(self, whisper_model: str = "small") -> ServerConfig:
        return ServerConfig(
            whisper_model=whisper_model,
            whisper_device="cpu",
            whisper_compute_type="int8",
            caption_interval_sec=0.0,
            caption_window_sec=1.0,
            caption_max_words=30,
            sample_rate=4,
            channels=1,
            bytes_per_sample=2,
        )

    def test_live_caption_uses_rolling_window_and_translates_supported_language(self):
        handler = _StubHandler(self._make_cfg())
        handler.start_session(1)
        handler.append_audio(1, b"abcdefghijkl")
        handler.responses = [
            _TranscriptionResult(text="hola mundo", language="es", language_probability=0.92),
            _TranscriptionResult(text="hello world", language="en", language_probability=0.99),
            _TranscriptionResult(text="hola otra vez", language="es", language_probability=0.95),
            _TranscriptionResult(text="hello again", language="en", language_probability=0.99),
        ]

        caption = handler.maybe_caption(1)
        self.assertEqual(caption, "hello world")
        self.assertEqual(handler.buffer_sizes, [8])
        self.assertEqual(handler.transcribe_calls[:2], [("transcribe", None), ("translate", "es")])
        self.assertEqual(handler.caption_source_language(1), "es")

        sess = handler._sessions[1]
        self.assertEqual(sess.detected_language, "es")
        self.assertTrue(sess.translation_enabled)

        handler.append_audio(1, b"mnop")
        caption = handler.maybe_caption(1)
        self.assertEqual(caption, "hello again")
        self.assertEqual(handler.buffer_sizes, [8, 8])
        self.assertEqual(
            handler.transcribe_calls[2:],
            [("transcribe", None), ("translate", "es")],
        )
        self.assertEqual(handler.caption_source_language(1), "es")

    def test_live_caption_passes_through_unsupported_language(self):
        handler = _StubHandler(self._make_cfg())
        handler.start_session(2)
        handler.append_audio(2, b"abcdefghijkl")
        handler.responses = [
            _TranscriptionResult(
                text="bonjour tout le monde",
                language="fr",
                language_probability=0.88,
            ),
        ]

        caption = handler.maybe_caption(2)
        self.assertEqual(caption, "bonjour tout le monde")
        self.assertEqual(handler.buffer_sizes, [8])
        self.assertEqual(handler.transcribe_calls, [("transcribe", None)])
        self.assertIsNone(handler._sessions[2].detected_language)
        self.assertFalse(handler._sessions[2].translation_enabled)

    def test_low_confidence_english_does_not_block_later_german_translation(self):
        cfg = self._make_cfg()
        cfg.translation_detection_min_probability = 0.65
        handler = _StubHandler(cfg)
        handler.start_session(5)
        handler.append_audio(5, b"abcdefghijkl")
        handler.responses = [
            _TranscriptionResult(text="how are", language="en", language_probability=0.18),
            _TranscriptionResult(text="guten morgen", language="de", language_probability=0.91),
            _TranscriptionResult(text="good morning", language="de", language_probability=0.91),
        ]

        first_caption = handler.maybe_caption(5)
        self.assertEqual(first_caption, "how are")
        self.assertIsNone(handler._sessions[5].detected_language)
        self.assertFalse(handler._sessions[5].translation_enabled)

        handler.append_audio(5, b"mnop")
        second_caption = handler.maybe_caption(5)
        self.assertEqual(second_caption, "good morning")
        self.assertEqual(handler._sessions[5].detected_language, "de")
        self.assertTrue(handler._sessions[5].translation_enabled)
        self.assertEqual(
            handler.transcribe_calls,
            [("transcribe", None), ("transcribe", None), ("translate", "de")],
        )

    def test_confident_english_clears_cached_translation_language(self):
        cfg = self._make_cfg()
        cfg.translation_detection_min_probability = 0.65
        handler = _StubHandler(cfg)
        handler.start_session(6)
        handler.append_audio(6, b"abcdefghijkl")
        handler.responses = [
            _TranscriptionResult(text="hola mundo", language="es", language_probability=0.93),
            _TranscriptionResult(text="hello world", language="en", language_probability=0.99),
            _TranscriptionResult(text="hello there", language="en", language_probability=0.97),
        ]

        first_caption = handler.maybe_caption(6)
        self.assertEqual(first_caption, "hello world")
        self.assertEqual(handler.caption_source_language(6), "es")
        self.assertEqual(handler._sessions[6].detected_language, "es")

        handler.append_audio(6, b"mnop")
        second_caption = handler.maybe_caption(6)
        self.assertEqual(second_caption, "hello there")
        self.assertIsNone(handler.caption_source_language(6))
        self.assertIsNone(handler._sessions[6].detected_language)
        self.assertFalse(handler._sessions[6].translation_enabled)
        self.assertEqual(
            handler.transcribe_calls,
            [("transcribe", None), ("translate", "es"), ("transcribe", None)],
        )

    def test_final_transcribe_uses_cached_translation_language(self):
        handler = _StubHandler(self._make_cfg())
        handler.start_session(3)
        handler.append_audio(3, b"abcdefghijkl")
        sess = handler._sessions[3]
        sess.detected_language = "de"
        sess.translation_enabled = True
        sess.detected_language_probability = 0.97
        handler.responses = [
            _TranscriptionResult(text="guten morgen", language="de", language_probability=0.97),
            _TranscriptionResult(text="good morning", language="de", language_probability=0.97),
        ]

        transcript = handler.final_transcribe(3)
        self.assertEqual(transcript, "good morning")
        self.assertEqual(handler.buffer_sizes, [12])
        self.assertEqual(handler.transcribe_calls, [("transcribe", None), ("translate", "de")])
        self.assertEqual(handler.caption_source_language(3), "de")

    def test_english_only_whisper_model_disables_translation(self):
        handler = _StubHandler(self._make_cfg(whisper_model="small.en"))
        handler.start_session(4)
        handler.append_audio(4, b"abcdefghijkl")
        handler.responses = [
            _TranscriptionResult(text="hola mundo", language="es", language_probability=0.92),
        ]

        caption = handler.maybe_caption(4)
        self.assertEqual(caption, "hola mundo")
        self.assertEqual(handler.transcribe_calls, [("transcribe", None)])
        self.assertFalse(handler._sessions[4].translation_enabled)
        self.assertIsNone(handler.caption_source_language(4))


if __name__ == "__main__":
    unittest.main()