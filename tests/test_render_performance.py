import base64
import io
import random
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import Mock, patch
import zlib

import termpdf


class RecordingBuffer(io.BytesIO):
    def __init__(self):
        super().__init__()
        self.write_sizes = []

    def write(self, data):
        self.write_sizes.append(len(data))
        return super().write(data)


class ImageTransferTests(unittest.TestCase):
    def upload(self, data, image_format):
        output = SimpleNamespace(buffer=RecordingBuffer(), flush=Mock())
        command = {'i': 7, 's': 1000, 'v': 400, 'f': image_format, 't': 'd', 'q': 2}
        original = command.copy()
        with patch.object(termpdf.sys, 'stdout', output):
            termpdf.write_chunked(command, data)
        self.assertEqual(command, original)
        stream = output.buffer.getvalue()
        self.assertTrue(stream.endswith(b'\033\\'))
        packets = []
        for packet in stream.split(b'\033\\')[:-1]:
            self.assertTrue(packet.startswith(b'\033_G'))
            header, payload = packet[3:].split(b';', 1)
            fields = dict(field.split(b'=', 1) for field in header.split(b','))
            self.assertLessEqual(len(payload), 4096)
            packets.append((fields, payload))
        self.assertEqual(packets[0][0][b'i'], b'7')
        self.assertEqual(packets[0][0][b'q'], b'2')
        self.assertEqual(packets[0][0][b'f'], str(image_format).encode())
        for fields, _ in packets[:-1]:
            self.assertEqual(fields[b'm'], b'1')
        self.assertEqual(packets[-1][0][b'm'], b'0')
        for fields, _ in packets[1:]:
            self.assertEqual(set(fields), {b'm'})
        decoded = base64.b64decode(b''.join(payload for _, payload in packets))
        if packets[0][0].get(b'o') == b'z':
            decoded = zlib.decompress(decoded)
        self.assertEqual(decoded, data)
        output.flush.assert_called_once_with()
        return packets, output.buffer.write_sizes

    def test_rgb_and_rgba_data_round_trip_with_fast_compression(self):
        for image_format, pixel in ((24, b'\x10\x20\x30'), (32, b'\x10\x20\x30\xff')):
            with self.subTest(image_format=image_format):
                packets, _ = self.upload(pixel * 100000, image_format)
                self.assertEqual(packets[0][0][b'o'], b'z')

    def test_noisy_image_skips_full_compression_and_batches_protocol_chunks(self):
        data = random.Random(17).randbytes(1200000)
        with patch.object(termpdf.zlib, 'compress', wraps=zlib.compress) as compress:
            packets, writes = self.upload(data, 24)
        self.assertNotIn(b'o', packets[0][0])
        self.assertTrue(compress.called)
        # Large incompressible images must not spend CPU compressing every pixel.
        self.assertTrue(all(len(args[0]) <= 65536 for args, _ in compress.call_args_list))
        self.assertGreater(len(packets), 100)
        self.assertLess(len(writes), len(packets) / 2)
        self.assertLessEqual(max(writes), 70000)

    def test_png_bytes_are_transferred_without_recompression(self):
        data = b'\x89PNG\r\n\x1a\n' + random.Random(29).randbytes(12000)
        with patch.object(termpdf.zlib, 'compress') as compress:
            packets, _ = self.upload(data, 100)
        compress.assert_not_called()
        self.assertNotIn(b'o', packets[0][0])


class FrameSchedulingTests(unittest.TestCase):
    def test_frame_wait_accounts_for_render_time_and_does_not_delay_slow_frames(self):
        for render_time, expected_wait in ((.005, 12), (.025, 0)):
            with self.subTest(render_time=render_time):
                clock = [1.0]
                starts = []
                screen = Mock()
                screen.getch.side_effect = [-1, ord('q')]
                doc = Mock(page=0, citekey=None)

                def render(*args):
                    starts.append(clock[0])
                    clock[0] += render_time

                def timeout(milliseconds):
                    if milliseconds >= 0:
                        clock[0] += milliseconds / 1000

                doc.display_page.side_effect = render
                screen.timeout.side_effect = timeout
                with patch.object(termpdf, 'scr', screen), \
                        patch.object(termpdf, 'monotonic', side_effect=lambda: clock[0]), \
                        patch.object(termpdf, 'HeldPan', return_value=Mock(active=True)), \
                        patch.object(termpdf, 'status_bar'), \
                        patch.object(termpdf, 'detect_support', return_value=True), \
                        patch.object(termpdf, 'clean_exit', side_effect=SystemExit):
                    with self.assertRaises(SystemExit):
                        termpdf.view(threading.Event(), doc)
                waits = [args[0] for args, _ in screen.timeout.call_args_list if args[0] >= 0]
                self.assertEqual(waits, [expected_wait, expected_wait])
                self.assertEqual(len(starts), 2)
                self.assertAlmostEqual(starts[1] - starts[0], render_time + expected_wait / 1000)


if __name__ == '__main__':
    unittest.main()
