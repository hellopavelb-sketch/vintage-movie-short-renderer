import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
import app


class TrailerTests(unittest.TestCase):
    def test_protected_and_foreign_media_are_rejected(self):
        base = "https://vod-ap-amt.tv.apple.com/itunes-assets/VideoPreview/test/index.m3u8"
        playlists = [
            '#EXTM3U\n#EXTINF:10,\nseg.mp4\n#EXT-X-ENDLIST\n#EXT-X-KEY:METHOD=SAMPLE-AES,URI="skd://key"',
            '#EXTM3U\n#EXTINF:1000,\nseg.mp4\n#EXT-X-ENDLIST',
            '#EXTM3U\n#EXTINF:10,\nhttps://127.0.0.1/test.mp4\n#EXT-X-ENDLIST',
        ]
        for playlist in playlists:
            with self.subTest(playlist=playlist), self.assertRaises(RuntimeError):
                app.apple_segments(playlist, base)

    def test_heygen_cannot_be_a_trailer(self):
        async def check():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app), base_url="http://test") as client:
                response = await client.post("/render", json={
                    "presenter_url": "https://files2.heygen.ai/presenter.mp4",
                    "trailer_url": "https://files2.heygen.ai/another-presenter.mp4",
                })
                self.assertEqual(response.status_code, 422)
        with patch.object(app, "TOKEN", ""):
            asyncio.run(check())

    def test_html_is_not_accepted_as_video(self):
        real_client = httpx.AsyncClient
        transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"<!DOCTYPE html>" + b" " * 2000, headers={"content-type": "text/html"}))
        async def check():
            with patch.object(app.httpx, "AsyncClient", side_effect=lambda **kwargs: real_client(transport=transport, **kwargs)):
                with self.assertRaisesRegex(RuntimeError, "not a direct video"):
                    await app.preflight_url("https://example.com/trailer", "trailer")
        asyncio.run(check())

    def test_partial_render_is_not_returned(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "final.mp4"
            output.write_bytes(b"x" * 12000)
            states = [
                ({"status": "rendering"}, output.name, output),
                ({"status": "completed"}, output.name, output),
            ]
            with patch.object(app, "TOKEN", ""), patch.object(app, "_resolve_output", side_effect=states) as resolve, patch.object(app.time, "sleep"):
                result = app.download_result("a" * 32)
                self.assertEqual(resolve.call_count, 2)
                self.assertEqual(result.path, output)

    def test_restart_marks_interrupted_jobs_failed(self):
        import json
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for state in ("queued", "downloading", "rendering", "completed", "failed"):
                job = root / state
                job.mkdir()
                app.write_status(job / "status.json", status=state)
            with patch.object(app, "ROOT", root):
                app.recover_interrupted_jobs()
            for state in ("queued", "downloading", "rendering"):
                result = json.loads((root / state / "status.json").read_text())
                self.assertEqual(result["status"], "failed")
                self.assertIn("existing presenter", result["error"])
            for state in ("completed", "failed"):
                self.assertEqual(json.loads((root / state / "status.json").read_text())["status"], state)


if __name__ == "__main__":
    unittest.main()
