import os
import tempfile
import unittest

from audio_analysis.live_caption import CaptionConfig, LiveCaptioner
from audio_analysis.transcription import LiveTranscriptionResult


class _FakeRecorder:
    def __init__(self, audio_path: str):
        self.audio_path = audio_path


class _FakeCursor:
    def __init__(self):
        self.calls = []

    def execute(self, sql, params):
        self.calls.append((sql, params))


class _FakeTranscriber:
    def __init__(self, result: LiveTranscriptionResult):
        self.result = result
        self.calls = 0

    def transcribe_live(self, audio_path: str) -> LiveTranscriptionResult:
        self.calls += 1
        return self.result


class LiveCaptionTranslationTests(unittest.TestCase):
    def test_display_caption_prefixes_detected_language(self):
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(b"audio")
            audio_path = tmp.name

        transcriber = _FakeTranscriber(
            LiveTranscriptionResult(text="How are you", detected_language="de", translated=True)
        )
        captioner = LiveCaptioner(transcriber, CaptionConfig(interval_sec=0.0, max_words=5))
        cursor = _FakeCursor()
        active_recorders = {1: _FakeRecorder(audio_path)}

        try:
            captioner.update(active_recorders, {1: 11}, cursor)

            self.assertEqual(captioner.get_caption_for_present({1: 'present'}), "How are you")
            self.assertEqual(
                captioner.get_display_caption_for_present({1: 'present'}),
                "(German) How are you",
            )
            self.assertEqual(transcriber.calls, 1)
            self.assertEqual(cursor.calls[0][1], ("How are you", 11))
        finally:
            os.unlink(audio_path)

    def test_display_caption_keeps_english_unprefixed(self):
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(b"audio")
            audio_path = tmp.name

        transcriber = _FakeTranscriber(
            LiveTranscriptionResult(text="Hello there", detected_language="en", translated=False)
        )
        captioner = LiveCaptioner(transcriber, CaptionConfig(interval_sec=0.0, max_words=5))

        try:
            captioner.update({2: _FakeRecorder(audio_path)}, {}, _FakeCursor())

            self.assertEqual(captioner.get_caption_for_present({2: 'present'}), "Hello there")
            self.assertEqual(
                captioner.get_display_caption_for_present({2: 'present'}),
                "Hello there",
            )
        finally:
            os.unlink(audio_path)


if __name__ == "__main__":
    unittest.main()