from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import fitz

import termpdf


class ReloadDocumentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'document.pdf'
        with fitz.open() as document:
            document.new_page().insert_text((20, 30), 'Original document')
            self.original_bytes = document.tobytes()
        self.path.write_bytes(self.original_bytes)
        self.doc = termpdf.Document(str(self.path))
        self.doc.citekey = 'saved-key'
        self.doc.zoom = 2
        self.addCleanup(self.doc.close)
        cache = patch.object(termpdf.os, 'environ',
                             {**termpdf.os.environ, 'XDG_CACHE_HOME': self.directory.name})
        cache.start()
        self.addCleanup(cache.stop)

    def test_empty_in_place_save_preserves_document_and_can_be_retried(self):
        self.path.write_bytes(b'')
        with self.assertRaises(fitz.EmptyFileError):
            termpdf.reload_document(self.doc)
        self.assertFalse(self.doc.is_closed)
        self.path.write_bytes(self.original_bytes)
        with termpdf.reload_document(self.doc) as replacement:
            self.assertEqual(replacement.page_count, 1)
            self.assertEqual(replacement.zoom, 2)
            self.assertEqual(replacement.citekey, 'saved-key')
        self.assertFalse(self.doc.is_closed)

    def test_missing_file_during_save_preserves_document(self):
        self.path.unlink()
        with self.assertRaises(FileNotFoundError):
            termpdf.reload_document(self.doc)
        self.assertFalse(self.doc.is_closed)

    def test_failed_preparation_closes_candidate_and_keeps_current_document(self):
        candidate = Mock(filename=str(self.path))
        candidate.set_layout.side_effect = RuntimeError('save is incomplete')
        with patch.object(termpdf, 'Document', return_value=candidate):
            with self.assertRaises(RuntimeError):
                termpdf.reload_document(self.doc)
        candidate.close.assert_called_once_with()
        self.assertFalse(self.doc.is_closed)

    def test_file_changing_during_reload_discards_candidate(self):
        candidate = Mock(filename=str(self.path))
        candidate.set_layout.side_effect = lambda *args, **kwargs: self.path.write_bytes(b'changed')
        with patch.object(termpdf, 'Document', return_value=candidate):
            with self.assertRaises(OSError):
                termpdf.reload_document(self.doc)
        candidate.close.assert_called_once_with()
        self.assertFalse(self.doc.is_closed)


class ReloadLoopTests(unittest.TestCase):
    def run_view(self, inputs, reload):
        screen = Mock(last_key_event=None)
        doc = Mock(page=0, citekey=None)
        other_doc = Mock(page=0, citekey=None)
        buffers = termpdf.Buffers()
        buffers.docs = [doc, other_doc]
        clock = [0]
        events = iter(inputs)

        def getch():
            key, elapsed = next(events)
            clock[0] += elapsed
            return key

        screen.getch.side_effect = getch
        signal = threading.Event()
        signal.set()
        attempts = []

        def prepare(current):
            self.assertIs(current, doc)
            self.assertIs(buffers.docs[0], doc)
            doc.close.assert_not_called()
            screen.clear.assert_not_called()
            attempts.append(clock[0])
            return reload(len(attempts))

        with patch.object(termpdf, 'scr', screen), \
                patch.object(termpdf, 'bufs', buffers), \
                patch.object(termpdf, 'status_bar'), \
                patch.object(termpdf, 'detect_support', return_value=True), \
                patch.object(termpdf, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(termpdf, 'reload_document', side_effect=prepare), \
                patch.object(termpdf, 'clean_exit', side_effect=SystemExit):
            with self.assertRaises(SystemExit):
                termpdf.view(signal, doc)
        return attempts, screen, buffers, doc

    def test_failed_saves_retry_without_another_file_event_then_install_replacement(self):
        replacement = Mock(page=0, citekey=None)

        def reload(attempt):
            if attempt < 3:
                raise fitz.EmptyFileError('still saving')
            return replacement

        attempts, screen, buffers, doc = self.run_view(
            [(termpdf.FILE_CHANGED, .01), (-1, .11), (-1, .21), (ord('q'), .01)], reload)
        self.assertEqual(len(attempts), 3)
        self.assertGreaterEqual(attempts[1] - attempts[0], .1)
        self.assertGreaterEqual(attempts[2] - attempts[1], .2)
        self.assertIs(buffers.docs[0], replacement)
        self.assertEqual(screen.clear.call_count, 1)
        doc.display_page.assert_not_called()
        doc.close.assert_called_once_with()
        replacement.display_page.assert_called()

    def test_quit_remains_responsive_while_reload_is_pending(self):
        def reload(attempt):
            raise fitz.EmptyFileError('still saving')

        attempts, screen, buffers, doc = self.run_view(
            [(termpdf.FILE_CHANGED, .01), (ord('q'), .01)], reload)
        self.assertEqual(len(attempts), 1)
        doc.close.assert_not_called()
        screen.clear.assert_not_called()

    def test_switching_buffers_cancels_retry_for_old_document(self):
        def reload(attempt):
            raise fitz.EmptyFileError('still saving')

        attempts, screen, buffers, doc = self.run_view(
            [(termpdf.FILE_CHANGED, .01), (ord('b'), .01), (ord('b'), .01),
             (-1, .5), (ord('q'), .01)], reload)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(buffers.current, 1)
        doc.close.assert_not_called()

    def test_quit_cleans_up_when_saved_file_is_temporarily_missing(self):
        screen = Mock()
        doc = Mock()
        doc.write_state.side_effect = FileNotFoundError('save in progress')
        buffers = termpdf.Buffers()
        buffers.docs = [doc]
        with patch.object(termpdf, 'scr', screen), \
                patch.object(termpdf, 'bufs', buffers), \
                patch.object(termpdf.curses, 'echo'), \
                patch.object(termpdf.curses, 'curs_set'), \
                patch.object(termpdf.curses, 'endwin') as endwin:
            with self.assertRaises(SystemExit):
                termpdf.clean_exit()
        doc.close.assert_called_once_with()
        endwin.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
