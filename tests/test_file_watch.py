import curses
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, Mock, patch

import termpdf


class FileChangeInputTests(unittest.TestCase):
    def setUp(self):
        self.signal = termpdf.FileChangeSignal()
        self.addCleanup(self.signal.close)
        self.screen = termpdf.Screen()
        self.screen.file_change = self.signal
        self.screen.stdscr = Mock()
        self.screen.stdscr.getch.return_value = -1
        self.screen.timeout(1000)
        self.input_fd, self.input_writer = os.pipe()
        self.addCleanup(os.close, self.input_fd)
        self.addCleanup(os.close, self.input_writer)
        stdin = patch.object(termpdf.sys, 'stdin', Mock(fileno=lambda: self.input_fd))
        stdin.start()
        self.addCleanup(stdin.stop)

    def test_signal_coalesces_notifications_and_can_be_reused(self):
        for _ in range(100):
            self.signal.set()
        self.assertTrue(self.signal.is_set())
        self.assertEqual(select.select([self.signal], [], [], 0)[0], [self.signal])
        self.assertEqual(os.read(self.signal.fileno(), 100), b'\0')
        self.signal.clear()
        self.assertFalse(self.signal.is_set())
        self.assertEqual(select.select([self.signal], [], [], 0)[0], [])
        self.signal.set()
        self.signal.clear()
        self.assertEqual(select.select([self.signal], [], [], 0)[0], [])

    def test_file_change_interrupts_wait_without_terminal_input(self):
        waiting, completed = threading.Event(), threading.Event()
        result = []
        original_select = select.select

        def wait(*args):
            waiting.set()
            return original_select(*args)

        def read():
            result.append(self.screen.getch())
            completed.set()

        with patch.object(termpdf.select, 'select', side_effect=wait):
            reader = threading.Thread(target=read)
            reader.start()
            try:
                self.assertTrue(waiting.wait(1))
                self.signal.set()
                # The configured input timeout is a full second. The pipe
                # must wake it without waiting for that timeout to expire.
                self.assertTrue(completed.wait(.3))
                self.assertEqual(result, [termpdf.FILE_CHANGED])
            finally:
                self.signal.set()
                reader.join(2)
        self.screen.stdscr.timeout.assert_called_with(1000)

    def test_buffered_resize_is_delivered_without_waiting_on_stdin(self):
        self.screen.stdscr.getch.return_value = curses.KEY_RESIZE
        with patch.object(termpdf.select, 'select') as wait:
            self.assertEqual(self.screen.getch(), curses.KEY_RESIZE)
        wait.assert_not_called()

    def test_key_read_at_same_time_as_file_change_is_preserved(self):
        def key():
            self.signal.set()
            return ord('j')

        self.screen.stdscr.getch.side_effect = key
        self.assertEqual(self.screen.getch(), termpdf.FILE_CHANGED)
        self.signal.clear()
        self.assertEqual(self.screen.getch(), ord('j'))
        self.assertEqual(self.screen.stdscr.getch.call_count, 1)

    def test_idle_wait_blocks_instead_of_spinning(self):
        self.screen.timeout(30)
        start = time.monotonic()
        self.assertEqual(self.screen.getch(), -1)
        self.assertGreaterEqual(time.monotonic() - start, .025)
        self.assertLessEqual(self.screen.stdscr.getch.call_count, 3)
        self.screen.stdscr.timeout.assert_called_with(30)

    def test_partial_key_sequence_survives_file_change(self):
        self.screen.key_sequence.extend(b'\033[106;1:')
        self.signal.set()
        self.assertEqual(self.screen.getch(), termpdf.FILE_CHANGED)
        self.signal.clear()
        self.screen.stdscr.getch.side_effect = b'1u'
        self.assertEqual(self.screen.getch(), ord('j'))

    def test_file_change_cancels_search_and_remains_pending_for_viewer(self):
        self.signal.set()
        self.assertEqual(self.screen.getstr(), b'')
        self.assertTrue(self.signal.is_set())

    def test_file_change_closes_dialogs(self):
        for method in ('show_toc', 'show_meta', 'show_links'):
            with self.subTest(dialog=method):
                doc = MagicMock(page=0, papersize=3,
                                page_states=[termpdf.Page_State(0)],
                                metadata={'title': 'Example'})
                doc.get_toc.return_value = [[1, 'Chapter', 1]]
                doc.current_chap.return_value = 0
                doc.__getitem__.return_value.get_links.return_value = [
                    {'kind': 2, 'uri': 'https://example.com', 'from': (0, 0, 1, 1)},
                ]
                doc.get_text_intersecting_Rect.return_value = ['Example']
                win, pad = Mock(), Mock()
                win.getbegyx.return_value = (1, 2)
                win.getmaxyx.return_value = (24, 80)
                self.screen.create_text_win = Mock(return_value=(win, pad))
                self.screen.clear = Mock()
                self.signal.set()
                with patch.object(termpdf, 'scr', self.screen):
                    getattr(termpdf.Document, method)(doc, Mock())
                self.assertTrue(self.signal.is_set())

    def test_pending_save_starts_refresh_before_redrawing_old_document(self):
        doc = Mock(page=0, citekey=None, filename='/nonexistent/test.pdf')
        buffers = termpdf.Buffers()
        buffers.docs = [doc]
        self.signal.set()
        with patch.object(termpdf, 'scr', self.screen), \
                patch.object(termpdf, 'bufs', buffers), \
                patch.object(termpdf, 'status_bar'), \
                patch.object(termpdf, 'detect_support', return_value=True), \
                patch.object(self.screen, 'get_size'), \
                patch.object(self.screen, 'init_curses'), \
                patch.object(self.screen, 'clear'), \
                patch.object(self.screen, 'enable_keyboard'), \
                patch.object(self.screen, 'disable_keyboard'), \
                patch.object(termpdf, 'Document', side_effect=SystemExit) as reopen:
            with self.assertRaises(SystemExit):
                termpdf.view(self.signal, doc)
        doc.display_page.assert_not_called()
        doc.write_state.assert_called_once_with()
        reopen.assert_called_once_with(doc.filename)
        self.assertFalse(self.signal.is_set())


class FileChangeWatcherTests(unittest.TestCase):
    def test_retarget_during_stat_does_not_mix_old_metadata_with_new_path(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            first, second, link = (directory / name for name in
                                   ('first.pdf', 'second.pdf', 'document.pdf'))
            first.write_bytes(b'first')
            second.write_bytes(b'second')
            link.symlink_to(first)
            with patch.object(termpdf, 'FileObserver', return_value=Mock()):
                watcher = termpdf.FileChangeWatcher(link)
            try:
                signature = watcher.signature

                def retarget_during_stat(path):
                    snapshot = signature(path)
                    replacement = directory / 'new-link'
                    replacement.symlink_to(second)
                    os.replace(replacement, link)
                    return snapshot

                with patch.object(watcher, 'signature', side_effect=retarget_during_stat):
                    watcher.on_any_event(termpdf.FileModifiedEvent(str(directory)))
                notifications = int(watcher.is_set())
                watcher.clear()
                watcher.on_any_event(termpdf.FileModifiedEvent(str(link)))
                notifications += int(watcher.is_set())
                self.assertEqual(notifications, 1)
            finally:
                watcher.close()

    def test_symlink_retarget_is_reported_once_before_watch_is_updated(self):
        for same_inode in (False, True):
            with self.subTest(same_inode=same_inode), \
                    tempfile.TemporaryDirectory() as directory:
                directory = Path(directory)
                first = directory / 'first' / 'real.pdf'
                second = directory / 'second' / 'real.pdf'
                first.parent.mkdir()
                second.parent.mkdir()
                first.write_bytes(b'original')
                if same_inode:
                    os.link(first, second)
                else:
                    second.write_bytes(b'new target')
                link = directory / 'document.pdf'
                link.symlink_to(first)
                with patch.object(termpdf, 'FileObserver', return_value=Mock()):
                    watcher = termpdf.FileChangeWatcher(link)
                try:
                    replacement = directory / 'new-link'
                    replacement.symlink_to(second)
                    os.replace(replacement, link)
                    watcher.on_any_event(termpdf.FileModifiedEvent(str(link)))
                    self.assertTrue(watcher.is_set())
                    watcher.clear()

                    for path in (link, directory, first, first.parent):
                        watcher.on_any_event(termpdf.FileModifiedEvent(str(path)))
                        self.assertFalse(watcher.is_set(), 'duplicate symlink notification')

                    watcher.watch(link)
                    self.assertFalse(watcher.is_set())
                    second.write_bytes(b'new target saved')
                    watcher.on_any_event(termpdf.FileModifiedEvent(str(second)))
                    self.assertTrue(watcher.is_set())
                finally:
                    watcher.close()


class NativeFileWatchTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'document.pdf'
        self.path.write_bytes(b'original')
        self.watcher = termpdf.FileChangeWatcher(self.path)
        self.addCleanup(self.watcher.close)
        self.watcher.start()

    def assert_changed(self):
        self.assertTrue(self.watcher.wait(2), 'native file notification was not received')
        self.assertEqual(select.select([self.watcher], [], [], 2)[0], [self.watcher])
        self.watcher.clear()

    def test_save_during_startup_is_not_missed(self):
        self.watcher.close()
        self.watcher = termpdf.FileChangeWatcher(self.path)
        self.addCleanup(self.watcher.close)
        self.path.write_bytes(b'saved before observer started')
        self.watcher.start()
        self.assertTrue(self.watcher.is_set())

    def test_in_place_saves_and_repeated_atomic_replacements(self):
        self.path.write_bytes(b'updated in place')
        self.assert_changed()
        for index in range(10):
            replacement = self.path.with_name('replacement.pdf')
            replacement.write_bytes(str(index).encode())
            os.replace(replacement, self.path)
            self.assert_changed()

    def test_reading_document_and_editing_siblings_do_not_trigger_reload(self):
        self.path.read_bytes()
        self.path.with_name('unrelated.pdf').write_bytes(b'unrelated')
        self.assertFalse(self.watcher.wait(.15))
        self.path.write_bytes(b'target changed')
        self.assert_changed()
        self.assertFalse(self.watcher.wait(.15), 'duplicate notification caused another refresh')

    def test_delete_and_recreate_keeps_watching(self):
        self.path.unlink()
        self.assertFalse(self.watcher.wait(.1), 'must not reload a missing file')
        self.path.write_bytes(b'recreated')
        self.assert_changed()

    def test_switching_buffers_follows_active_path_and_directory(self):
        sibling = self.path.with_name('sibling.pdf')
        other = self.path.parent / 'subdir' / 'another.pdf'
        other.parent.mkdir()
        for new_path in (sibling, other):
            new_path.write_bytes(b'original')
            self.watcher.watch(new_path)
            self.path.write_bytes(b'old buffer saved')
            self.assertFalse(self.watcher.wait(.1))
            new_path.write_bytes(b'new buffer saved')
            self.assert_changed()
            self.path = new_path

    def test_close_stops_threads_and_closes_wakeup_pipe(self):
        self.watcher.close()
        self.watcher.close()
        self.assertFalse(self.watcher._observer.is_alive())
        with self.assertRaises(OSError):
            os.fstat(self.watcher.fileno())
        self.watcher.set()

    def test_symlink_target_writes_and_replacements_in_another_directory(self):
        self.watcher.close()
        target = self.path.parent / 'targets' / 'real.pdf'
        target.parent.mkdir()
        target.write_bytes(b'original target')
        self.path.unlink()
        self.path.symlink_to(target)
        self.watcher = termpdf.FileChangeWatcher(self.path)
        self.addCleanup(self.watcher.close)
        self.watcher.start()
        target.write_bytes(b'updated target')
        self.assert_changed()
        replacement = target.with_name('replacement.pdf')
        replacement.write_bytes(b'atomic save')
        os.replace(replacement, target)
        self.assert_changed()
        target.write_bytes(b'updated after atomic save')
        self.assert_changed()

    def test_retargeted_symlink_moves_the_target_watch(self):
        self.watcher.close()
        targets = []
        for name in ('first', 'second'):
            target = self.path.parent / name / 'real.pdf'
            target.parent.mkdir()
            target.write_bytes(name.encode())
            targets.append(target)
        self.path.unlink()
        self.path.symlink_to(targets[0])
        self.watcher = termpdf.FileChangeWatcher(self.path)
        self.addCleanup(self.watcher.close)
        self.watcher.start()
        link = self.path.with_name('new-link')
        link.symlink_to(targets[1])
        os.replace(link, self.path)
        self.assert_changed()
        self.watcher.watch(self.path)
        # Publish one complete save so an in-place truncate/write cannot
        # leave a second valid notification pending for the final assertion.
        replacement = targets[1].with_name('replacement.pdf')
        replacement.write_bytes(b'new target saved')
        os.replace(replacement, targets[1])
        self.assert_changed()
        targets[0].write_bytes(b'old target saved')
        self.assertFalse(self.watcher.wait(.15))

    def test_linux_symlink_watches_link_and_resolved_target_directories(self):
        target = self.path.parent / 'targets' / 'real.pdf'
        target.parent.mkdir()
        target.write_bytes(b'original target')
        link = self.path.with_name('linked.pdf')
        link.symlink_to(target)
        observer = Mock()
        with patch.object(termpdf.sys, 'platform', 'linux'), \
                patch.object(termpdf, 'FileObserver', return_value=observer):
            watcher = termpdf.FileChangeWatcher(link)
            try:
                self.assertEqual({call.args[1] for call in observer.schedule.call_args_list},
                                 {str(link.parent), str(target.parent.resolve())})
                target.write_bytes(b'updated in another directory')
                watcher.on_any_event(termpdf.FileModifiedEvent(str(target.resolve())))
                self.assertTrue(watcher.is_set())
            finally:
                watcher.close()


@unittest.skipUnless(sys.platform == 'darwin', 'macOS kqueue backend')
class KqueueShutdownTests(unittest.TestCase):
    def test_idle_shutdown_interrupts_long_native_wait_and_releases_resources(self):
        with tempfile.TemporaryDirectory() as directory:
            observer = termpdf.FileObserver(timeout=5)
            self.addCleanup(observer.stop)
            observer.schedule(termpdf.FileSystemEventHandler(), directory, recursive=False)
            emitter = next(iter(observer.emitters))
            waiting = threading.Event()
            read_events = emitter._read_events

            def read(timeout):
                waiting.set()
                return read_events(timeout)

            with patch.object(emitter, '_read_events', side_effect=read):
                observer.start()
                self.assertTrue(waiting.wait(1), 'watcher never entered its native wait')
                started = time.monotonic()
                observer.stop()
                observer.join(1)
                elapsed = time.monotonic() - started

            self.assertLess(elapsed, .3, 'shutdown waited for the native timeout')
            self.assertFalse(observer.is_alive())
            self.assertFalse(emitter.is_alive())
            self.assertTrue(emitter._kq.closed)
            for descriptor in (emitter._shutdown_signal.read_fd,
                               emitter._shutdown_signal.write_fd):
                with self.assertRaises(OSError):
                    os.fstat(descriptor)

    def test_closing_before_start_releases_native_wait_and_shutdown_pipe(self):
        with tempfile.TemporaryDirectory() as directory:
            observer = termpdf.FileObserver()
            observer.schedule(termpdf.FileSystemEventHandler(), directory, recursive=False)
            emitter = next(iter(observer.emitters))
            observer.stop()
            self.assertTrue(emitter._kq.closed)
            self.assertTrue(emitter._shutdown_signal.closed)

    def test_startup_and_buffer_switch_in_large_directory_with_low_fd_limit(self):
        script = '''
import os
from pathlib import Path
import resource
import tempfile
import termpdf
with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    crowded = root / 'crowded'
    crowded.mkdir()
    for index in range(300):
        (crowded / str(index)).touch()
    first, second = crowded / 'document.pdf', root / 'other.pdf'
    first.write_bytes(b'original')
    second.write_bytes(b'original')
    _, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, hard))
    baseline = len(os.listdir('/dev/fd'))
    watcher = termpdf.FileChangeWatcher(first)
    try:
        watcher.start()
        for path in (second, first):
            watcher.watch(path)
            path.write_bytes(b'updated')
            assert watcher.wait(2), 'save was not detected'
            watcher.clear()
            assert len(os.listdir('/dev/fd')) - baseline < 20, 'unbounded descriptor count'
    finally:
        watcher.close()
    assert len(os.listdir('/dev/fd')) == baseline, 'watch descriptors leaked'
'''
        result = subprocess.run([sys.executable, '-c', script], cwd=Path(termpdf.__file__).parent,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
