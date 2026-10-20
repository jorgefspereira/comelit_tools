"""Offline regressions for the monitor SDP and recording failure handling."""
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import av
from aiortc import RTCConfiguration, RTCPeerConnection
from aiortc.rtcsctptransport import RTCSctpTransport
import comelit_stream as client


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_offer_preserves_certificate_and_media(self):
        pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        try:
            pc.addTransceiver('video', direction='recvonly')
            pc.createDataChannel('libcomelit-channel')
            offer = (await pc.createOffer()).sdp
            result = client.compatible_offer(offer)
            fingerprints = [s for s in result.splitlines() if s.startswith('a=fingerprint:')]
            expected = [s for s in offer.splitlines() if s.startswith('a=fingerprint:sha-256 ')]
            self.assertTrue(expected)
            self.assertEqual(fingerprints, expected)
            self.assertEqual(
                [s for s in offer.splitlines() if not s.startswith('a=fingerprint:')],
                [s for s in result.splitlines() if not s.startswith('a=fingerprint:')])
            self.assertTrue(result.endswith('\r\n'))
        finally:
            await pc.close()

    async def test_rejection_preserves_output_and_library_role(self):
        class Response:
            status = 500
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            async def text(self):
                return '{"errorCode":"DEVCOM:MESSAGE:BUSY"}'
        class Session:
            def put(self, *args, **kwargs):
                return Response()
        original_role = RTCSctpTransport.is_server
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'existing.mp4'
            output.write_bytes(b'previous recording')
            with patch.object(client, 'ICE_SERVERS', []), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit):
                    await client.run_webrtc({}, 'test', str(output), 1, None, Session(),
                                            sctp_role='server')
            self.assertEqual(output.read_bytes(), b'previous recording')
            self.assertEqual(list(Path(directory).iterdir()), [output])
            self.assertIs(RTCSctpTransport.is_server, original_role)

    def test_recording_requires_decodable_video(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'video.mp4'
            with av.open(str(output), 'w') as container:
                stream = container.add_stream('libx264', rate=10)
                stream.width, stream.height = 64, 48
                stream.pix_fmt = 'yuv420p'
                frame = av.VideoFrame(64, 48, 'yuv420p')
                for plane in frame.planes:
                    plane.update(bytes(plane.buffer_size))
                for packet in stream.encode(frame):
                    container.mux(packet)
                for packet in stream.encode(None):
                    container.mux(packet)
            self.assertEqual(client.verify_recording(str(output)), (64, 48))
            output.write_bytes(b'')
            with self.assertRaises(av.error.FFmpegError):
                client.verify_recording(str(output))


if __name__ == '__main__':
    unittest.main()
