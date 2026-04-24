import unittest

from summarization.audio_ws import AudioServerConfig, AudioTranscriptionHandler, _TranscriptionResult


class _StubHandler(AudioTranscriptionHandler):
    def __init__(self, cfg: AudioServerConfig):
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


class SummarizationAudioWsTests(unittest.TestCase):
    def _make_cfg(self, whisper_model: str = "small") -> AudioServerConfig:
        return AudioServerConfig(
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

    def test_live_caption_uses_rolling_window_and_translation(self):
        handler = _StubHandler(self._make_cfg())
        handler.start_session(1)
        handler.append_audio(1, b"abcdefghijkl")
        handler.responses = [
            _TranscriptionResult(text="hola mundo", language="es", language_probability=0.92),
            _TranscriptionResult(text="hello world", language="es", language_probability=0.92),
        ]

        caption = handler.maybe_caption(1)
        self.assertEqual(caption["text"], "hello world")
        self.assertEqual(caption["source_language"], "es")
        self.assertEqual(handler.buffer_sizes, [8])
        self.assertEqual(handler.transcribe_calls, [("transcribe", None), ("translate", "es")])

    def test_final_transcribe_reuses_locked_language(self):
        handler = _StubHandler(self._make_cfg())
        handler.start_session(2)
        handler.append_audio(2, b"abcdefghijkl")
        sess = handler._sessions[2]
        sess.detected_language = "de"
        sess.translation_enabled = True
        handler.responses = [
            _TranscriptionResult(text="good morning", language="de", language_probability=0.95),
        ]

        transcript = handler.final_transcribe(2)
        self.assertEqual(transcript, "good morning")
        self.assertEqual(handler.buffer_sizes, [12])
        self.assertEqual(handler.transcribe_calls, [("translate", "de")])


if __name__ == "__main__":
    unittest.main()