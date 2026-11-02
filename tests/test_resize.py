import curses
import threading
import unittest
from unittest.mock import MagicMock, Mock, call, patch

import termpdf


class ResizeTests(unittest.TestCase):
    def test_initialization_refreshes_without_consuming_input(self):
        screen = termpdf.Screen()
        window = Mock()
        with patch.object(curses, 'initscr', return_value=window), \
                patch.object(curses, 'noecho'), \
                patch.object(curses, 'curs_set'), \
                patch.object(curses, 'mousemask'):
            screen.init_curses()
        window.refresh.assert_called_once_with()
        window.getch.assert_not_called()

    def test_graphics_reply_preserves_keys_and_resize_events_in_order(self):
        screen = termpdf.Screen()
        screen.stdscr = Mock()
        screen.stdscr.getch.side_effect = [
            ord('L'), curses.KEY_RESIZE, 27, ord('H'),
            *b'\033_Gi=2;OK\033\\', *b'\033_Gi=1;OK\033\\',
        ]
        self.assertTrue(screen.graphics_response(1))
        self.assertEqual([screen.getch() for _ in range(4)],
                         [ord('L'), curses.KEY_RESIZE, 27, ord('H')])
        screen.stdscr.timeout.assert_called_once_with(100)
        screen.stdscr.nodelay.assert_called_once_with(False)

    def test_escape_immediately_before_graphics_reply_is_preserved(self):
        screen = termpdf.Screen()
        screen.stdscr = Mock()
        screen.stdscr.getch.side_effect = [27, *b'\033_Gi=1,p=1;OK\033\\', -1]
        self.assertTrue(screen.graphics_response(1))
        self.assertEqual(screen.getch(), 27)
        self.assertFalse(screen.pending_keys)

    def test_graphics_reply_reports_missing_image_and_has_a_timeout(self):
        screen = termpdf.Screen()
        screen.stdscr = Mock()
        screen.stdscr.getch.side_effect = b'\033_Gi=1;ENOENT:image missing\033\\'
        self.assertFalse(screen.graphics_response(1))
        screen.stdscr.getch.side_effect = None
        screen.stdscr.getch.return_value = -1
        with patch.object(termpdf, 'monotonic', side_effect=[0, 0, 3]):
            self.assertFalse(screen.graphics_response(1))

    def test_resize_refreshes_curses_before_next_input(self):
        for curses_size in [(24, 80), (30, 100)]:
            with self.subTest(curses_size=curses_size):
                screen = termpdf.Screen()
                screen.rows, screen.cols = 30, 100
                screen.get_size = Mock()
                screen.stdscr = Mock()
                screen.stdscr.getmaxyx.return_value = curses_size
                with patch.object(curses, 'resizeterm') as resizeterm:
                    screen.resize()
                if curses_size == (30, 100):
                    resizeterm.assert_not_called()
                else:
                    resizeterm.assert_called_once_with(30, 100)
                self.assertEqual(screen.stdscr.mock_calls,
                                 [call.getmaxyx(), call.clear(), call.refresh()])

    def test_resize_redraws_without_reopening_document(self):
        screen = Mock()
        screen.getch.side_effect = [curses.KEY_RESIZE, ord('q')]
        doc = Mock(page=0, citekey=None)
        other_doc = Mock()
        buffers = termpdf.Buffers()
        buffers.docs = [doc, other_doc]
        order = Mock()
        order.attach_mock(doc.display_page, 'display')
        order.attach_mock(screen.resize, 'resize')
        order.attach_mock(doc.mark_all_pages_stale, 'invalidate_current')
        order.attach_mock(other_doc.mark_all_pages_stale, 'invalidate_other')
        with patch.object(termpdf, 'scr', screen), \
                patch.object(termpdf, 'bufs', buffers), \
                patch.object(termpdf, 'status_bar') as bar_type, \
                patch.object(termpdf, 'detect_support', return_value=True), \
                patch.object(termpdf, 'Document') as document_type, \
                patch.object(termpdf, 'clean_exit', side_effect=SystemExit):
            with self.assertRaises(SystemExit):
                termpdf.view(threading.Event(), doc)
        document_type.assert_not_called()
        doc.write_state.assert_not_called()
        screen.init_curses.assert_called_once_with()
        self.assertEqual(order.mock_calls, [
            call.display(bar_type.return_value, 0),
            call.resize(),
            call.invalidate_current(),
            call.invalidate_other(),
            call.display(bar_type.return_value, 0),
        ])
        # Keep the existing low-CPU polling interval for file-change checks.
        self.assertEqual(screen.timeout.call_args_list,
                         [call(100), call(-1), call(100), call(-1)])

    def test_dialogs_handle_repeated_resizes_and_use_rebuilt_pads(self):
        for method_name in ('show_toc', 'show_meta', 'show_links'):
            with self.subTest(dialog=method_name):
                screen = Mock()
                screen.getch.side_effect = [
                    curses.KEY_RESIZE, curses.KEY_RESIZE, 27,
                ]
                doc = MagicMock(page=0, papersize=3,
                                page_states=[termpdf.Page_State(0)],
                                metadata={'title': 'Example'})
                doc.get_toc.return_value = [[1, 'Chapter', 1]]
                doc.current_chap.return_value = 0
                doc.__getitem__.return_value.get_links.return_value = [
                    {'kind': 2, 'uri': 'https://example.com', 'from': (0, 0, 1, 1)},
                ]
                doc.get_text_intersecting_Rect.return_value = ['Example']
                other_doc = Mock()
                buffers = termpdf.Buffers()
                buffers.docs = [doc, other_doc]
                pads = []
                windows = []
                sizes = [(24, 80), (18, 70), (30, 100)]
                for size in sizes:
                    win, pad = Mock(), Mock()
                    win.getbegyx.return_value = (1, 2)
                    win.getmaxyx.return_value = size
                    windows.append((win, pad))
                    pads.append(pad)
                screen.create_text_win.side_effect = windows

                with patch.object(termpdf, 'scr', screen), \
                        patch.object(termpdf, 'bufs', buffers):
                    getattr(termpdf.Document, method_name)(doc, Mock())

                self.assertEqual(screen.resize.call_count, 2)
                self.assertEqual(doc.mark_all_pages_stale.call_count, 2)
                self.assertEqual(other_doc.mark_all_pages_stale.call_count, 2)
                doc.set_layout.assert_not_called()
                screen.init_curses.assert_not_called()
                self.assertEqual(screen.create_text_win.call_count, 3)
                for pad, (height, width) in zip(pads, sizes):
                    pad.refresh.assert_called_once_with(
                        0, 0, 4, 4, height - 1, width - 1)


if __name__ == '__main__':
    unittest.main()
