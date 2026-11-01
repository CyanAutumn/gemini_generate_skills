"""Content-based media validation with tiny, locally encoded video fixtures."""

from __future__ import annotations

import hashlib
import random
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

import av
from PIL import Image

SCRIPTS = Path(__file__).resolve().parents[1] / "gemini-browser" / "scripts"
sys.path.insert(0, str(SCRIPTS))
from local_state import verify_image, verify_media, verify_video


def make_video(path: Path, *, webm=False):
    options = {} if webm else {"movflags": "+faststart"}
    with av.open(str(path), "w", format="webm" if webm else "mp4", options=options) as output:
        stream = output.add_stream("libvpx-vp9" if webm else "libx264", rate=10)
        stream.width, stream.height = 96, 64
        stream.pix_fmt = "yuv420p"
        if webm:
            stream.options = {"deadline": "realtime", "cpu-used": "8"}
        else:
            stream.options = {"preset": "ultrafast", "crf": "20"}
        rng = random.Random(20261101)
        for index in range(12):
            image = Image.frombytes("RGB", (96, 64), rng.randbytes(96 * 64 * 3))
            frame = av.VideoFrame.from_image(image)
            frame.pts, frame.time_base = index, Fraction(1, 10)
            for packet in stream.encode(frame):
                output.mux(packet)
        for packet in stream.encode():
            output.mux(packet)


class MediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="gemini-media-tests-")
        cls.root = Path(cls.temporary.name)
        cls.mp4 = cls.root / "valid.mp4"
        cls.webm = cls.root / "valid.webm"
        make_video(cls.mp4)
        make_video(cls.webm, webm=True)
        cls.png = cls.root / "source.png"
        Image.new("RGB", (40, 30), "orange").save(cls.png)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_valid_mp4_is_fully_decoded_and_metadata_and_hash_match(self):
        result = verify_video(self.mp4)
        self.assertEqual(result["format"], "MP4")
        self.assertEqual(result["media_type"], "video")
        self.assertEqual((result["width"], result["height"]), (96, 64))
        self.assertEqual(result["decoded_frames"], 12)
        self.assertEqual(result["video_streams"], 1)
        self.assertAlmostEqual(result["duration_seconds"], 1.2, places=2)
        self.assertEqual(result["size_bytes"], self.mp4.stat().st_size)
        self.assertEqual(result["sha256"], hashlib.sha256(self.mp4.read_bytes()).hexdigest())

    def test_valid_webm_is_recognized_by_container_and_decoded(self):
        result = verify_video(self.webm)
        self.assertEqual(result["format"], "WEBM")
        self.assertEqual(result["decoded_frames"], 12)
        self.assertAlmostEqual(result["duration_seconds"], 1.2, places=2)

    def test_truncated_mp4_and_webm_are_rejected(self):
        for source in (self.mp4, self.webm):
            with self.subTest(container=source.suffix):
                path = self.root / ("truncated" + source.suffix)
                data = source.read_bytes()
                path.write_bytes(data[:len(data) // 2])
                with self.assertRaisesRegex(ValueError, "视频校验失败"):
                    verify_video(path)

    def test_late_corruption_is_detected_after_initial_frames(self):
        path = self.root / "late-corruption.mp4"
        data = bytearray(self.mp4.read_bytes())
        corrupt_start = 3 * len(data) // 4
        data[corrupt_start:] = bytes(len(data) - corrupt_start)
        path.write_bytes(data)
        with self.assertRaisesRegex(ValueError, "视频校验失败"):
            verify_video(path)

    def test_empty_html_and_renamed_picture_are_not_videos(self):
        samples = {"empty.mp4": b"", "error.mp4": b"<!doctype html><html>download failed</html>",
                   "picture.mp4": self.png.read_bytes()}
        for name, data in samples.items():
            with self.subTest(name=name):
                path = self.root / name
                path.write_bytes(data)
                with self.assertRaisesRegex(ValueError, "视频校验失败"):
                    verify_video(path)

    def test_audio_only_mp4_is_not_accepted_as_video(self):
        path = self.root / "audio-only.m4a"
        with av.open(str(path), "w", format="mp4") as output:
            stream = output.add_stream("aac", rate=8000)
            frame = av.AudioFrame(format="fltp", layout="mono", samples=800)
            frame.sample_rate, frame.pts, frame.time_base = 8000, 0, Fraction(1, 8000)
            for plane in frame.planes:
                plane.update(bytes(plane.buffer_size))
            for packet in stream.encode(frame):
                output.mux(packet)
            for packet in stream.encode():
                output.mux(packet)
        with self.assertRaisesRegex(ValueError, "没有可播放的视频流"):
            verify_video(path)

    def test_media_autodetection_uses_content_even_with_wrong_extension(self):
        renamed_video = self.root / "video-named-picture.jpg"
        renamed_video.write_bytes(self.mp4.read_bytes())
        self.assertEqual(verify_media(renamed_video)["media_type"], "video")
        renamed_image = self.root / "picture-named-video.mp4"
        renamed_image.write_bytes(self.png.read_bytes())
        self.assertEqual(verify_media(renamed_image)["media_type"], "image")
        unknown_extension = self.root / "native-download.download"
        unknown_extension.write_bytes(self.webm.read_bytes())
        self.assertEqual(verify_media(unknown_extension)["format"], "WEBM")

    def test_explicit_media_type_is_enforced(self):
        self.assertEqual(verify_media(self.png, "image")["format"], "PNG")
        self.assertEqual(verify_media(self.mp4, "video")["format"], "MP4")
        with self.assertRaises(ValueError):
            verify_media(self.png, "video")
        with self.assertRaises(ValueError):
            verify_media(self.mp4, "image")
        with self.assertRaises(ValueError):
            verify_media(self.mp4, "audio")

    def test_unsupported_bitmap_is_rejected_and_image_interface_is_unchanged(self):
        path = self.root / "unsupported.bmp"
        Image.new("RGB", (8, 8)).save(path)
        with self.assertRaises(ValueError):
            verify_media(path)
        info = verify_image(self.png)
        self.assertEqual(info["format"], "PNG")
        self.assertNotIn("media_type", info)


if __name__ == "__main__":
    unittest.main()
