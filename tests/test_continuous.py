import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import fitz
import termpdf


class ContinuousViewTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        path = self.directory / 'mixed.pdf'
        with fitz.open() as pdf:
            for width, height, color in [(400, 400, (1, 0, 0)),
                                          (600, 800, (0, 1, 0)),
                                          (300, 300, (0, 0, 1))]:
                page = pdf.new_page(width=width, height=height)
                page.draw_rect(page.rect, color=None, fill=color)
            pdf.save(path)
        self.doc = termpdf.Document(str(path))
        self.addCleanup(self.doc.close)
        self.screen = termpdf.Screen()
        self.screen.width, self.screen.height = 400, 420
        self.screen.cell_width, self.screen.cell_height = 10, 20
        self.screen.cols, self.screen.rows = 40, 21
        self.screen.set_cursor = Mock()
        self.uploads, self.commands = [], []

        def upload(cmd, data):
            self.uploads.append((cmd.copy(), data))

        def command(cmd):
            self.commands.append(cmd.copy())
            return True

        for name, value in [('scr', self.screen), ('write_chunked', upload),
                            ('write_gr_cmd', command), ('write_gr_cmd_with_response', command)]:
            patcher = patch.object(termpdf, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def render(self):
        self.doc.display_page(Mock(), self.doc.page)

    def test_page_boundary_shows_both_pages_at_one_scale(self):
        self.render()
        self.assertEqual(self.screen.visible_pages, {0})
        self.doc.pan(0, 1, count=5)
        self.render()
        self.assertEqual(self.screen.visible_pages, {0, 1})
        self.assertEqual(self.doc.page_states[0].factor, 1)
        self.assertEqual(self.doc.page_states[1].factor, 1)
        placements = [cmd for cmd in self.commands if cmd['a'] == 'p'][-2:]
        self.assertEqual([(cmd['i'], cmd['h'], cmd['Y']) for cmd in placements],
                         [(1, 200, 0), (2, 184, 16)])
        # Only visible pages are rasterized, including the partial next page.
        self.assertEqual([cmd['i'] for cmd, _ in self.uploads], [1, 2])

    def test_scrolling_changes_center_page_without_changing_scale(self):
        self.render()
        self.doc.pan(0, 1, count=10)
        self.render()
        self.assertEqual(self.doc.page, 1)
        self.assertEqual(self.doc.logicalpage, '2')
        self.assertEqual(self.doc.page_states[1].factor, 1)
        self.assertEqual(self.screen.visible_pages, {1})
        self.assertTrue(any(cmd.get('d') == 'i' and cmd['i'] == 1 for cmd in self.commands))
        self.assertEqual(sum(cmd.get('d') == 'a' for cmd in self.commands), 1)

    def test_center_page_is_chosen_by_distance_to_page_centers(self):
        self.doc.ensure_view()
        self.doc.set_view_position(300, 508)
        self.assertEqual(self.doc.page, 0)
        self.doc.set_view_position(300, 509)
        self.assertEqual(self.doc.page, 1)

    def test_zero_fits_and_centers_the_nearest_page(self):
        self.doc.ensure_view()
        self.doc.zoom = 3
        self.doc.set_view_position(300, 900)
        self.assertEqual(self.doc.page, 1)
        self.doc.reset_view()
        self.render()
        self.assertEqual(self.doc.zoom, 1)
        self.assertEqual(self.doc.page, 1)
        self.assertEqual(self.doc.fit_page, 1)
        self.assertEqual(self.doc.view_position(), (300, 816))
        self.assertEqual(self.doc.page_states[1].factor, .5)
        self.assertEqual(self.screen.visible_pages, {1})

    def test_next_and_previous_page_reset_zoom_and_fit_the_target_page(self):
        self.doc.change_zoom(4)
        self.doc.next_page()
        self.assertEqual((self.doc.page, self.doc.zoom, self.doc.fit_page), (1, 1, 1))
        self.assertEqual(self.doc.view_position(), (300, 816))
        self.doc.change_zoom(2)
        self.doc.prev_page()
        self.assertEqual((self.doc.page, self.doc.zoom, self.doc.fit_page), (0, 1, 0))
        self.assertEqual(self.doc.view_position(), (300, 200))

    def test_document_ends_and_horizontal_page_edges_are_bounded(self):
        self.doc.change_zoom(10)
        self.doc.pan(1, 1, count=100000)
        self.render()
        self.assertEqual(self.doc.page, 2)
        self.assertEqual(self.screen.visible_pages, {2})
        self.assertLessEqual(self.doc.camera_y, self.doc.canvas_height)
        self.doc.pan(-1, -1, count=100000)
        self.render()
        self.assertEqual(self.doc.page, 0)
        self.assertEqual(self.screen.visible_pages, {0})
        self.assertGreaterEqual(self.doc.camera_y, 0)

    def test_small_scrolls_across_a_boundary_reuse_cached_images(self):
        self.doc.ensure_view()
        self.doc.set_view_position(300, 400)
        self.render()
        uploads = len(self.uploads)
        self.doc.pan(0, 1, count=.1)
        self.render()
        self.assertEqual(self.screen.visible_pages, {0, 1})
        self.assertEqual(len(self.uploads), uploads)

    def test_zoomed_out_view_can_show_three_pages_at_once(self):
        self.doc.zoom = .25
        self.doc.ensure_view()
        self.doc.set_view_position(300, self.doc.canvas_height / 2)
        self.render()
        self.assertEqual(self.screen.visible_pages, {0, 1, 2})
        self.assertTrue(all(self.doc.page_states[p].factor == .25 for p in range(3)))

    def test_resize_keeps_camera_position_and_rotating_keeps_center_page(self):
        self.doc.ensure_view()
        self.doc.set_view_position(300, 816)
        self.screen.width, self.screen.height = 600, 620
        self.doc.mark_all_pages_stale()
        self.render()
        self.assertEqual(self.doc.page, 1)
        self.doc.rotation = 90
        self.doc.mark_all_pages_stale()
        self.render()
        self.assertEqual(self.doc.page, 1)
        self.assertAlmostEqual(self.doc.pan_y, .5)

    def test_saved_settings_restore_continuous_camera_and_scale(self):
        self.doc.ensure_view()
        self.doc.zoom = 2
        self.doc.set_view_position(300, 900)
        state_path = self.directory / 'state.json'
        with patch.object(termpdf, 'get_cachefile', return_value=state_path):
            self.doc.write_state()
        with termpdf.Document(self.doc.filename) as restored:
            for key, value in json.loads(state_path.read_text()).items():
                setattr(restored, key, value)
            restored.ensure_view()
            self.assertEqual(restored.view_position(), (300, 900))
            self.assertEqual(restored.zoom, 2)
            self.assertEqual(restored.page, 1)


if __name__ == '__main__':
    unittest.main()
