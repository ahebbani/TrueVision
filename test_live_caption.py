import os
import tempfile
import time
import unittest

from audio_analysis.live_caption import CaptionConfig, LiveCaptioner


class _FakeRecorder:
    def __init__(self, audio_path: str):
        self.audio_path = audio_path
        self.flush_calls = []

    def flush_to_wav(self, seconds=None):
        self.flush_calls.append(seconds)
        return True


class _FakeCursor:
    def __init__(self):
        self.calls = []

    def execute(self, sql, params):
        self.calls.append((sql, params))


class _SlowTranscriber:
    def __init__(self, delay: float, text: str):
        self.delay = delay
        self.text = text
        self.calls = 0

    def transcribe(self, audio_path: str) -> str:
        self.calls += 1
        time.sleep(self.delay)
        return self.text


class LiveCaptionerTests(unittest.TestCase):
    def test_update_returns_without_waiting_for_transcription(self):
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(b"audio")
            audio_path = tmp.name

        transcriber = _SlowTranscriber(0.2, "one two three four")
        captioner = LiveCaptioner(transcriber, CaptionConfig(interval_sec=0.0, max_words=2))
        cursor = _FakeCursor()
        active_recorders = {1: _FakeRecorder(audio_path)}
        active_meetings = {1: 99}

        try:
            started = time.perf_counter()
            captioner.update(active_recorders, active_meetings, cursor)
            elapsed = time.perf_counter() - started

            self.assertLess(elapsed, 0.1)

            deadline = time.time() + 2.0
            caption = None
            while time.time() < deadline:
                captioner.update(active_recorders, active_meetings, cursor)
                caption = captioner.get_caption_for_present({1: 'present'})
                if caption:
                    break
                time.sleep(0.02)

            self.assertEqual(caption, "three four")
            self.assertEqual(transcriber.calls, 1)
            self.assertEqual(len(cursor.calls), 1)
            self.assertEqual(cursor.calls[0][1], ("one two three four", 99))
            self.assertGreaterEqual(len(active_recorders[1].flush_calls), 1)
            self.assertTrue(all(v == 8.0 for v in active_recorders[1].flush_calls))
        finally:
            captioner.stop()
            os.unlink(audio_path)

    def test_clear_drops_stale_inflight_results(self):
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(b"audio")
            audio_path = tmp.name

        transcriber = _SlowTranscriber(0.15, "stale result")
        captioner = LiveCaptioner(transcriber, CaptionConfig(interval_sec=0.0, max_words=3))
        cursor = _FakeCursor()
        active_recorders = {7: _FakeRecorder(audio_path)}
        active_meetings = {7: 11}

        try:
            captioner.update(active_recorders, active_meetings, cursor)
            captioner.clear(7)

            deadline = time.time() + 1.0
            while time.time() < deadline:
                captioner.update({}, {}, cursor)
                time.sleep(0.02)

            self.assertIsNone(captioner.get_caption_for_present({7: 'present'}))
            self.assertEqual(cursor.calls, [])
        finally:
            captioner.stop()
            os.unlink(audio_path)

    def test_status_reports_progress_before_caption_arrives(self):
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(b"audio")
            audio_path = tmp.name

        transcriber = _SlowTranscriber(0.2, "hello there")
        captioner = LiveCaptioner(transcriber, CaptionConfig(interval_sec=0.0, max_words=5, window_sec=3.0))
        cursor = _FakeCursor()
        active_recorders = {5: _FakeRecorder(audio_path)}

        try:
            captioner.update(active_recorders, {}, cursor)
            self.assertEqual(captioner.get_status_for_present({5: 'present'}), "Transcribing...")
            self.assertEqual(active_recorders[5].flush_calls, [3.0])

            deadline = time.time() + 2.0
            while time.time() < deadline:
                captioner.update(active_recorders, {}, cursor)
                if captioner.get_caption_for_present({5: 'present'}):
                    break
                time.sleep(0.02)

            self.assertEqual(captioner.get_caption_for_present({5: 'present'}), "hello there")
            self.assertEqual(captioner.get_status_for_present({5: 'present'}), "Captions live")
        finally:
            captioner.stop()
            os.unlink(audio_path)


if __name__ == "__main__":
    unittest.main()