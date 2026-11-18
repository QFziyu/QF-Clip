"""Real dependency compatibility without downloading an ASR model."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
import wave


class DependencyTests(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec("faster_whisper"), "faster-whisper not installed")
    def test_whisper_audio_decoder_accepts_installed_pyav(self):
        # Catches PyAV versions that removed metadata_errors from av.open.
        from faster_whisper.audio import decode_audio
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "中文 解码测试.wav"
            with wave.open(str(path), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(16000)
                wav.writeframes(b"\0\0" * 16000)
            samples = decode_audio(str(path), sampling_rate=16000)
            self.assertEqual(len(samples), 16000)
            self.assertEqual(float(abs(samples).max()), 0.0)


if __name__ == "__main__":
    unittest.main()
