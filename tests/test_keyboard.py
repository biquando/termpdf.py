import curses
import io
import unittest
from unittest.mock import Mock, patch

import termpdf


class KeyboardTests(unittest.TestCase):
    def setUp(self):
        self.screen = termpdf.Screen()
        self.screen.stdscr = Mock()

    def feed(self, data):
        self.screen.stdscr.getch.side_effect = [*data, -1]

    def test_rex_press_repeat_and_release_events(self):
        self.feed(b'\033[?31u\033[108:76;2;76u\033[108:76;2:2;76u\033[108;2:3u')
        self.assertEqual(self.screen.getch(), ord('L'))
        self.assertEqual(self.screen.keyboard_flags, 31)
        self.assertEqual(self.screen.last_key_event,
                         termpdf.KeyEvent(ord('L'), 108, 1, 1))
        self.assertEqual(self.screen.getch(), ord('L'))
        self.assertEqual(self.screen.last_key_event.event_type, 2)
        self.assertEqual(self.screen.getch(), termpdf.KEY_EVENT)
        self.assertEqual(self.screen.last_key_event.code, 108)
        self.assertEqual(self.screen.last_key_event.event_type, 3)

    def test_release_matches_physical_key_after_shift_is_released(self):
        self.feed(b'\033[108:76;2;76u\033[108;1:3u')
        self.assertEqual(self.screen.getch(), ord('L'))
        self.assertEqual(self.screen.getch(), termpdf.KEY_EVENT)
        self.assertEqual(self.screen.last_key_event.code, ord('l'))
        self.assertEqual(self.screen.last_key_event.modifiers, 0)

    def test_navigation_zoom_control_keys_and_enter_decode_normally(self):
        self.feed(b'\033[106;1;106u\033[61:43;2;43u\033[45;1;45u'
                  b'\033[114;5u\033[13u\033[1;1A\033[1;1:3A')
        self.assertEqual([self.screen.getch() for _ in range(7)],
                         [ord('j'), ord('+'), ord('-'), 18, 10,
                          curses.KEY_UP, termpdf.KEY_EVENT])

    def test_partial_sequence_survives_timeout_and_resize(self):
        self.feed([*b'\033[108;2:', -1, curses.KEY_RESIZE, *b'1u'])
        self.assertEqual(self.screen.getch(), -1)
        self.assertEqual(self.screen.getch(), curses.KEY_RESIZE)
        self.assertEqual(self.screen.getch(), ord('L'))

    def test_focus_loss_is_reported_and_focus_gain_is_ignored(self):
        self.feed(b'\033[O\033[Iq')
        self.assertEqual(self.screen.getch(), termpdf.FOCUS_OUT)
        self.assertEqual(self.screen.getch(), ord('q'))

    def test_arrow_release_events_keep_distinct_direction_codes(self):
        self.feed(b'\033[1;1:3A\033[1;1:3B\033[1;1:3C\033[1;1:3D')
        for code in (curses.KEY_UP, curses.KEY_DOWN, curses.KEY_RIGHT, curses.KEY_LEFT):
            self.assertEqual(self.screen.getch(), termpdf.KEY_EVENT)
            self.assertEqual(self.screen.last_key_event.code, code)

    def test_keyboard_mode_is_pushed_and_popped_only_once(self):
        output = Mock()
        with patch.object(termpdf.sys, 'stdout', output):
            self.screen.enable_keyboard()
            self.screen.enable_keyboard()
            self.screen.disable_keyboard()
            self.screen.disable_keyboard()
        self.assertEqual([args[0] for args, _ in output.buffer.write.call_args_list],
                         [b'\033[>31u\033[?u\033[?1004h', b'\033[?1004l\033[<u'])

    def test_search_text_uses_decoded_keys_and_ignores_key_releases(self):
        self.feed(b'\033[97;1;97u\033[97;1:3u\033[98;1;98u'
                  b'\033[127u\033[99;1;99u\033[13u')
        with patch.object(termpdf.sys, 'stdout', io.StringIO()):
            self.assertEqual(self.screen.getstr(), b'ac')

    def test_resizing_during_search_preserves_the_resize_for_the_viewer(self):
        self.feed([curses.KEY_RESIZE])
        self.assertEqual(self.screen.getstr(), b'')
        self.assertEqual(self.screen.getch(), curses.KEY_RESIZE)


class HeldPanTests(unittest.TestCase):
    def setUp(self):
        self.pan = termpdf.HeldPan()
        self.doc = Mock()
        self.clock = [0]
        timer = patch.object(termpdf, 'monotonic', side_effect=lambda: self.clock[0])
        timer.start()
        self.addCleanup(timer.stop)

    def test_holding_moves_without_repeat_events_and_release_stops(self):
        self.pan.press(self.doc, ord('l'), 1, 0)
        self.clock[0] = .02
        self.pan.advance(self.doc)
        self.doc.pan.assert_called_once_with(1, 0, count=.15)
        self.clock[0] = .04
        self.pan.advance(self.doc)
        self.assertEqual(self.doc.pan.call_count, 2)
        self.pan.release(ord('l'))
        self.clock[0] = .06
        self.pan.advance(self.doc)
        self.assertFalse(self.pan.active)
        self.assertEqual(self.doc.pan.call_count, 2)

    def test_releasing_one_of_two_keys_keeps_the_other_direction(self):
        self.pan.press(self.doc, ord('l'), 1, 0)
        self.pan.press(self.doc, ord('j'), 0, 1)
        self.clock[0] = .02
        self.pan.advance(self.doc)
        dx, dy = self.doc.pan.call_args.args
        self.assertAlmostEqual(dx, 2 ** -.5)
        self.assertAlmostEqual(dy, 2 ** -.5)
        self.pan.release(ord('l'))
        self.clock[0] = .04
        self.pan.advance(self.doc)
        self.doc.pan.assert_called_with(0, 1, count=.15)

    def test_stopping_or_switching_documents_cancels_held_keys(self):
        self.pan.press(self.doc, ord('l'), 1, 0)
        self.pan.stop()
        self.clock[0] = .02
        self.pan.advance(self.doc)
        self.doc.pan.assert_not_called()
        self.pan.press(self.doc, ord('l'), 1, 0)
        self.pan.advance(Mock())
        self.assertFalse(self.pan.active)
        self.doc.pan.assert_not_called()


if __name__ == '__main__':
    unittest.main()
