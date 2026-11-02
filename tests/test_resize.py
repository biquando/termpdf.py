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

    def test_drain_preserves_first_and_later_resize_events(self):
        for events in ([curses.KEY_RESIZE, -1, -1],
                       [27, ord('_'), curses.KEY_RESIZE],
                       [curses.KEY_RESIZE, curses.KEY_RESIZE, -1]):
            with self.subTest(events=events):
                screen = termpdf.Screen()
                screen.stdscr = Mock()
                screen.stdscr.getch.side_effect = events
                with patch.object(termpdf, 'monotonic',
                                  side_effect=[0, .01, .02, .03, .11]), \
                        patch.object(curses, 'ungetch') as ungetch:
                    screen.swallow_keys()
                ungetch.assert_called_once_with(curses.KEY_RESIZE)
                self.assertEqual(screen.stdscr.getch.call_count, 3)
                # Reply draining should wait for input rather than spin.
                self.assertTrue(all(args[0] > 0 for args, _ in
                                    screen.stdscr.timeout.call_args_list))
                screen.stdscr.nodelay.assert_called_once_with(False)

    def test_drain_discards_replies_without_inventing_resize(self):
        screen = termpdf.Screen()
        screen.stdscr = Mock()
        screen.stdscr.getch.side_effect = [27, ord('_'), ord('G')]
        with patch.object(termpdf, 'monotonic',
                          side_effect=[0, .01, .02, .03, .11]), \
                patch.object(curses, 'ungetch') as ungetch:
            screen.swallow_keys()
        ungetch.assert_not_called()

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
        screen.stdscr.getch.side_effect = [curses.KEY_RESIZE, ord('q')]
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
        self.assertEqual(screen.stdscr.timeout.call_args_list,
                         [call(100), call(100)])

    def test_dialogs_handle_repeated_resizes_and_use_rebuilt_pads(self):
        for method_name in ('show_toc', 'show_meta', 'show_links'):
            with self.subTest(dialog=method_name):
                screen = Mock()
                screen.stdscr.getch.side_effect = [
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
