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
            _TranscriptionResult(text="hello world", language="es", language_probability=0.92),
            _TranscriptionResult(text="hello again", language="es", language_probability=0.95),
        ]

        caption = handler.maybe_caption(1)
        self.assertEqual(caption, "hello world")
        self.assertEqual(handler.buffer_sizes, [8])
        self.assertEqual(handler.transcribe_calls[:2], [("transcribe", None), ("translate", "es")])

        sess = handler._sessions[1]
        self.assertEqual(sess.detected_language, "es")
        self.assertTrue(sess.translation_enabled)

        handler.append_audio(1, b"mnop")
        caption = handler.maybe_caption(1)
        self.assertEqual(caption, "hello again")
        self.assertEqual(handler.buffer_sizes, [8, 8])
        self.assertEqual(handler.transcribe_calls[2], ("translate", "es"))

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
        self.assertEqual(handler._sessions[2].detected_language, "fr")
        self.assertFalse(handler._sessions[2].translation_enabled)

    def test_final_transcribe_uses_cached_translation_language(self):
        handler = _StubHandler(self._make_cfg())
        handler.start_session(3)
        handler.append_audio(3, b"abcdefghijkl")
        sess = handler._sessions[3]
        sess.detected_language = "de"
        sess.translation_enabled = True
        handler.responses = [
            _TranscriptionResult(text="good morning", language="de", language_probability=0.97),
        ]

        transcript = handler.final_transcribe(3)
        self.assertEqual(transcript, "good morning")
        self.assertEqual(handler.buffer_sizes, [12])
        self.assertEqual(handler.transcribe_calls, [("translate", "de")])

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


if __name__ == "__main__":
    unittest.main()