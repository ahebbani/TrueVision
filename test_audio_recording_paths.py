import os
import tempfile
import unittest

from audio_analysis.esp32_serial_audio import ESP32SerialRecorder


class _FakeReceiver:
    def __init__(self):
        self.calls = []
        self.cleared = False

    def clear_buffer(self):
        self.cleared = True

    def write_to_wav(self, output_path, seconds=None):
        self.calls.append((output_path, seconds))
        os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)
        with open(output_path, 'wb') as f:
            f.write(b'fake')
        return True


class AudioRecordingPathTests(unittest.TestCase):
    def test_live_caption_flush_uses_sidecar_file(self):
        receiver = _FakeReceiver()
        recorder = ESP32SerialRecorder(receiver)

        with tempfile.TemporaryDirectory() as tmpdir:
            final_path = recorder.start(tmpdir, 'audio_only')
            flushed = recorder.flush_to_wav(seconds=2.0)

            self.assertTrue(flushed)
            self.assertTrue(receiver.cleared)
            self.assertEqual(len(receiver.calls), 1)
            flush_path, flush_seconds = receiver.calls[0]
            self.assertEqual(flush_seconds, 2.0)
            self.assertEqual(flush_path, recorder.caption_audio_path)
            self.assertNotEqual(flush_path, final_path)
            self.assertTrue(os.path.exists(flush_path))
            self.assertFalse(os.path.exists(final_path))

    def test_stop_writes_final_file_and_removes_sidecar(self):
        receiver = _FakeReceiver()
        recorder = ESP32SerialRecorder(receiver)

        with tempfile.TemporaryDirectory() as tmpdir:
            final_path = recorder.start(tmpdir, 'audio_only')
            recorder.flush_to_wav(seconds=2.0)
            sidecar_path = recorder.caption_audio_path

            stopped_path = recorder.stop()

            self.assertEqual(stopped_path, final_path)
            self.assertEqual(len(receiver.calls), 2)
            self.assertEqual(receiver.calls[1][0], final_path)
            self.assertTrue(os.path.exists(final_path))
            self.assertFalse(os.path.exists(sidecar_path))


if __name__ == '__main__':
    unittest.main()