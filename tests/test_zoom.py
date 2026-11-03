import curses
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import Mock, call, patch

import fitz

import termpdf


class ZoomTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        path = Path(self.directory.name) / 'example.pdf'
        with fitz.open() as pdf:
            page = pdf.new_page(width=400, height=400)
            for row in range(4):
                for col in range(4):
                    color = (row / 4, col / 4, (row + col) / 8)
                    page.draw_rect(fitz.Rect(col * 100, row * 100,
                                             (col + 1) * 100, (row + 1) * 100),
                                   color=None, fill=color)
            pdf.new_page(width=300, height=500)
            pdf.save(path)
        self.doc = termpdf.Document(str(path))
        self.addCleanup(self.doc.close)
        self.doc.logical_pages = ['1', '2']
        self.screen = termpdf.Screen()
        self.screen.width, self.screen.height = 400, 420
        self.screen.cols, self.screen.rows = 40, 21
        self.screen.cell_width, self.screen.cell_height = 10, 20
        self.screen.set_cursor = Mock()
        self.frames = []
        self.placements = []

        def upload(cmd, samples):
            self.frames.append((cmd.copy(), samples))

        def place(cmd):
            if cmd.get('a') == 'p':
                self.placements.append(cmd.copy())
            return True

        for name, value in [('scr', self.screen),
                            ('write_chunked', upload),
                            ('write_gr_cmd', place),
                            ('write_gr_cmd_with_response', place)]:
            patcher = patch.object(termpdf, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def render(self):
        self.doc.display_page(Mock(), self.doc.page)
        return self.frames[-1][0], self.placements[-1]

    def visible_samples(self):
        cmd, samples = self.frames[-1]
        placement = self.placements[-1]
        x, y = placement['x'], placement['y']
        width, height = placement['w'], placement['h']
        return b''.join(samples[((y + row) * cmd['s'] + x) * 3:
                                ((y + row) * cmd['s'] + x + width) * 3]
                        for row in range(height))

    def test_pdf_zoom_changes_scale_and_reset_restores_fit(self):
        self.render()
        self.assertEqual(self.doc.page_states[0].factor, 1)
        self.doc.change_zoom(1)
        self.render()
        self.assertEqual(self.doc.page_states[0].factor, 1.25)
        self.doc.pan(1, 1)
        self.render()
        self.assertGreater(self.doc.pan_x, .5)
        self.assertGreater(self.doc.pan_y, .5)
        self.doc.reset_view()
        self.render()
        self.assertEqual((self.doc.zoom, self.doc.pan_x, self.doc.pan_y), (1, .5, .5))
        self.assertEqual(self.doc.page_states[0].factor, 1)

    def test_reflowable_document_uses_visual_zoom_without_changing_pagination(self):
        path = Path(self.directory.name) / 'example.html'
        path.write_text('<html><body><p>Example text</p></body></html>')
        with termpdf.Document(str(path)) as doc:
            self.assertTrue(doc.is_reflowable)
            pages = doc.page_count
            doc.display_page(Mock(), 0)
            factor = doc.page_states[0].factor
            doc.change_zoom(1)
            doc.display_page(Mock(), 0)
            self.assertAlmostEqual(doc.page_states[0].factor, factor * 1.25)
            self.assertEqual(doc.page_count, pages)

    def test_zoom_out_centers_pages_and_vertical_pan_can_reach_the_next_page(self):
        self.doc.change_zoom(-1)
        self.render()
        cmd = next(cmd for cmd, _ in self.frames if cmd['i'] == 1)
        placement = next(cmd for cmd in self.placements if cmd['i'] == 1)
        self.assertEqual((cmd['s'], cmd['v']), (320, 320))
        self.screen.set_cursor.assert_any_call(5, 3)
        self.doc.pan(1, 1, count=100)
        self.render()
        self.assertEqual((self.doc.pan_x, self.doc.pan_y), (.5, .5))
        self.assertEqual(self.doc.page, 1)
        self.assertEqual(placement['C'], 1)

    def test_rendered_viewport_matches_full_raster_at_every_rotation(self):
        for page_rotation in (0, 90, 180, 270):
            self.doc[0].set_rotation(page_rotation)
            for rotation in (0, 90, 180, 270):
                with self.subTest(page_rotation=page_rotation, rotation=rotation):
                    self.doc.rotation = rotation
                    self.doc.zoom = 2
                    self.doc.pan_x, self.doc.pan_y = .625, .375
                    self.doc.camera_x = self.doc.camera_y = None
                    self.doc.mark_all_pages_stale()
                    cmd, placement = self.render()
                    self.assertLessEqual(cmd['s'], 800)
                    self.assertLessEqual(cmd['v'], 800)
                    full = self.doc[0].get_pixmap(
                        matrix=fitz.Matrix(2, 2).prerotate(rotation))
                    samples = full.samples
                    expected = b''.join(
                        samples[(y * full.width + 300) * 3:
                                (y * full.width + 700) * 3]
                        for y in range(100, 500))
                    self.assertEqual(self.visible_samples(), expected)
                    self.assertEqual((placement['w'], placement['h']), (400, 400))

    def test_high_zoom_and_pan_remain_bounded_and_reuse_unchanged_view(self):
        self.doc.change_zoom(10000)
        self.assertEqual(self.doc.zoom, 8)
        cmd, _ = self.render()
        self.assertEqual((cmd['s'], cmd['v']), (800, 800))
        self.render()
        self.assertEqual(len(self.frames), 1)
        self.doc.pan(1, 1, count=10000)
        self.render()
        state = self.doc.page_states[self.doc.page]
        self.assertEqual(self.doc.page, 1)
        self.assertEqual(state.origin, (2000, 3600))
        self.doc.pan(-1, -1, count=10000)
        self.render()
        self.assertEqual(self.doc.page_states[0].origin, (0, 0))
        self.doc.change_zoom(-10000)
        self.render()
        self.assertEqual(self.doc.zoom, .25)
        self.assertEqual((self.doc.pan_x, self.doc.pan_y), (.5, .5))

    def test_nearby_pan_frames_reuse_upload_without_deleting_or_waiting(self):
        self.doc.zoom = 4
        with patch.object(termpdf, 'write_gr_cmd', wraps=termpdf.write_gr_cmd) as commands, \
                patch.object(termpdf, 'write_gr_cmd_with_response',
                             wraps=termpdf.write_gr_cmd_with_response) as acknowledgments:
            self.render()
            for _ in range(3):
                self.doc.pan(1, 0)
                self.render()
        self.assertEqual(len(self.frames), 1)
        self.assertEqual(acknowledgments.call_count, 1)
        deletes = [args[0] for args, _ in commands.call_args_list if args[0]['a'] == 'd']
        self.assertEqual(len(deletes), 1)
        self.assertEqual(self.doc.page_states[0].origin, (720, 600))
        self.assertTrue(all(cmd['p'] == 1 for cmd in self.placements))
        self.assertTrue(all(cmd['q'] == 2 for cmd in self.placements[1:]))

    def test_crossing_cache_boundary_uploads_new_bounded_region(self):
        self.doc.zoom = 8
        self.render()
        first_cache = tuple(self.doc.page_states[0].cache_rect)
        self.doc.pan(1, 0, count=10)
        self.render()
        self.assertEqual(len(self.frames), 2)
        self.assertNotEqual(tuple(self.doc.page_states[0].cache_rect), first_cache)
        self.assertIn(self.doc.page_states[0].viewport, self.doc.page_states[0].cache_rect)
        self.assertTrue(all(cmd['s'] <= 802 and cmd['v'] <= 802
                            for cmd, _ in self.frames))

    def test_returning_to_evicted_page_reuploads_automatically(self):
        self.render()
        self.doc.goto_page(1)
        self.render()
        self.doc.goto_page(0)
        with patch.object(termpdf, 'write_gr_cmd_with_response',
                          side_effect=[False, True]) as acknowledgments:
            self.render()
        self.assertEqual(acknowledgments.call_count, 2)
        self.assertEqual(len(self.frames), 3)
        self.assertFalse(self.doc.page_states[0].stale)

    def test_animation_interpolates_and_repeated_keys_accumulate(self):
        self.doc.zoom = 4
        self.render()
        clock = [0]
        animation = termpdf.PanAnimation()
        with patch.object(termpdf, 'monotonic', side_effect=lambda: clock[0]):
            animation.start(self.doc, 1, 0)
            self.assertEqual(self.doc.pan_x, .5)
            self.assertTrue(animation.active)
            clock[0] = .05
            animation.advance(self.doc)
            self.assertAlmostEqual(self.doc.pan_x, .5 + .025 * .875)
            animation.start(self.doc, 1, 0)
            self.assertAlmostEqual(animation.target[0], 220)
            clock[0] = .2
            animation.advance(self.doc)
            self.assertAlmostEqual(self.doc.pan_x, .55)
            self.assertFalse(animation.active)

    def test_finishing_animation_allows_reset_and_navigation_to_take_over(self):
        self.doc.zoom = 4
        self.render()
        animation = termpdf.PanAnimation()
        animation.start(self.doc, 1, 0)
        animation.finish(self.doc)
        self.doc.reset_view()
        animation.advance(self.doc)
        self.assertEqual((self.doc.zoom, self.doc.pan_x, self.doc.pan_y), (1, .5, .5))
        self.assertFalse(animation.active)

    def test_fractional_zoom_clips_non_square_pages_without_pixel_gaps(self):
        self.doc.goto_page(1)
        page = self.doc[1]
        page.draw_rect(fitz.Rect(0, 0, 300, 500), color=None, fill=(.8, .2, .1))
        page.draw_rect(fitz.Rect(50, 100, 260, 450), color=None, fill=(.1, .4, .9))
        for rotation in (0, 90, 180, 270):
            for zoom in (1.25 ** 3, 8):
                with self.subTest(rotation=rotation, zoom=zoom):
                    self.doc.rotation = rotation
                    self.doc.zoom = zoom
                    self.doc.pan_x, self.doc.pan_y = .7, .3
                    self.doc.camera_x = self.doc.camera_y = None
                    cmd, placement = self.render()
                    state = self.doc.page_states[1]
                    full = page.get_pixmap(matrix=fitz.Matrix(
                        state.factor, state.factor).prerotate(rotation))
                    # Compare the pixels Kitty will display with a crop of an
                    # independently rendered full-page reference image.
                    visible = state.viewport
                    sx, sy = int(visible[0] - full.x), int(visible[1] - full.y)
                    width, height = placement['w'], placement['h']
                    expected = b''.join(
                        full.samples[((sy + y) * full.width + sx) * 3:
                                     ((sy + y) * full.width + sx + width) * 3]
                        for y in range(height))
                    source_x, source_y = placement['x'], placement['y']
                    samples = self.frames[-1][1]
                    actual = b''.join(
                        samples[((source_y + y) * cmd['s'] + source_x) * 3:
                                ((source_y + y) * cmd['s'] + source_x + width) * 3]
                        for y in range(height))
                    self.assertEqual(actual, expected)
                    self.assertLessEqual(cmd['s'], self.screen.width * 2 + 2)
                    self.assertLessEqual(cmd['v'], (self.screen.height - self.screen.cell_height) * 2 + 2)

    def test_resize_retains_zoom_and_recalculates_viewport(self):
        self.doc.zoom = 2
        self.render()
        self.screen.width, self.screen.height = 300, 620
        self.screen.cols, self.screen.rows = 30, 31
        self.doc.mark_all_pages_stale()
        cmd, placement = self.render()
        self.assertEqual(self.doc.zoom, 2)
        self.assertEqual((cmd['s'], cmd['v']), (600, 600))
        self.assertEqual((placement['w'], placement['h']), (300, 600))

    def test_coordinate_conversion_accounts_for_pan_zoom_and_rotation(self):
        for rotation in (0, 90, 180, 270):
            with self.subTest(rotation=rotation):
                self.doc.zoom = 2
                self.doc.rotation = rotation
                self.doc.pan_x, self.doc.pan_y = .625, .375
                self.doc.camera_x = self.doc.camera_y = None
                self.render()
                cells = ((1, 1), (3, 4), (20, 10))
                points = self.doc.cells_to_pixels(*cells)
                self.assertEqual(tuple(self.doc.pixels_to_cells(*points)), cells)
                if rotation == 0:
                    self.assertEqual(points[0], (150, 50))

    def test_manual_crop_maps_visible_cells_back_to_document_coordinates(self):
        self.doc.zoom = 2
        self.doc.pan_x, self.doc.pan_y = .625, .375
        self.render()
        termpdf.crop_to_selection(self.doc, 0, 2, [1, 2])
        self.assertEqual(self.doc.manualcroprect, [(150, 50), (160, 70)])
        self.assertTrue(self.doc.manualcrop)

    def test_page_navigation_retains_zoom_and_recenters(self):
        self.doc.zoom = 2
        self.doc.pan_x, self.doc.pan_y = .75, .25
        self.doc.goto_page(1)
        self.assertEqual((self.doc.zoom, self.doc.pan_x, self.doc.pan_y), (2, .5, .5))
        self.render()
        self.assertEqual(self.doc.page, 1)

    def test_document_settings_include_zoom_and_pan(self):
        path = Path(self.directory.name) / 'state.json'
        self.doc.zoom = 2
        self.doc.pan_x, self.doc.pan_y = .75, .25
        with patch.object(termpdf, 'get_cachefile', return_value=path):
            self.doc.write_state()
        state = json.loads(path.read_text())
        self.assertEqual((state['zoom'], state['pan_x'], state['pan_y']), (2, .75, .25))


class ZoomKeyTests(unittest.TestCase):
    def run_keys(self, keys, scroll=None):
        screen = Mock()
        screen.height, screen.cell_height = 420, 20
        screen.last_scroll_event = scroll
        screen.getch.side_effect = [*keys, ord('q')]
        doc = Mock(page=0, citekey=None, pan_x=.5, pan_y=.5)
        doc.view_position.return_value = (0, 0)
        with patch.object(termpdf, 'scr', screen), \
                patch.object(termpdf, 'status_bar'), \
                patch.object(termpdf, 'detect_support', return_value=True), \
                patch.object(termpdf, 'clean_exit', side_effect=SystemExit):
            with self.assertRaises(SystemExit):
                termpdf.view(threading.Event(), doc)
        return doc

    def test_zoom_pan_and_reset_bindings(self):
        doc = self.run_keys(map(ord, '+=-hjkl0'))
        self.assertEqual(doc.change_zoom.call_args_list, [call(1), call(1), call(-1)])
        self.assertEqual(doc.pan.call_args_list, [
            call(-1, 0, count=1), call(0, 1, count=1),
            call(0, -1, count=1), call(1, 0, count=1),
        ])
        doc.reset_view.assert_called_once_with()
        doc.next_page.assert_not_called()
        doc.prev_page.assert_not_called()
        doc.next_chap.assert_not_called()
        doc.prev_chap.assert_not_called()
        doc.set_layout.assert_not_called()

    def test_zero_inside_numeric_counts_does_not_reset(self):
        doc = self.run_keys(map(ord, '10G10l2+3-'))
        doc.goto_logical_page.assert_called_once_with(10)
        doc.pan.assert_called_once_with(1, 0, count=10)
        self.assertEqual(doc.change_zoom.call_args_list, [call(2), call(-3)])
        doc.reset_view.assert_not_called()

    def test_ctrl_n_and_ctrl_p_navigate(self):
        doc = self.run_keys([14, 16])
        doc.next_page.assert_called_once_with(1)
        doc.prev_page.assert_called_once_with(1)
        doc.next_chap.assert_not_called()
        doc.prev_chap.assert_not_called()
        doc.pan.assert_not_called()

    def test_ctrl_u_and_ctrl_d_scroll_half_windows_with_counts_without_resetting_zoom(self):
        doc = self.run_keys([21, ord('3'), 4])
        self.assertEqual(doc.pan.call_args_list,
                         [call(0, -1, count=5), call(0, 1, count=15)])
        doc.reset_view.assert_not_called()
        doc.change_zoom.assert_not_called()
        doc.next_page.assert_not_called()
        doc.prev_page.assert_not_called()

    def test_uppercase_hjkl_do_nothing_and_leave_counts_intact(self):
        doc = self.run_keys(map(ord, 'HJKL10Hj'))
        doc.pan.assert_called_once_with(0, 1, count=10)
        doc.next_page.assert_not_called()

    def test_arrow_keys_pan_instead_of_navigating(self):
        doc = self.run_keys([curses.KEY_UP, curses.KEY_DOWN,
                             curses.KEY_LEFT, curses.KEY_RIGHT])
        self.assertEqual(doc.pan.call_args_list, [
            call(0, -1, count=1), call(0, 1, count=1),
            call(-1, 0, count=1), call(1, 0, count=1),
        ])
        doc.next_page.assert_not_called()
        doc.prev_page.assert_not_called()
        doc.next_chap.assert_not_called()
        doc.prev_chap.assert_not_called()

    def test_mouse_scroll_moves_the_document_in_each_axis(self):
        for direction in ((0, 1), (1, 0)):
            with self.subTest(direction=direction):
                doc = self.run_keys([termpdf.SCROLL], termpdf.ScrollEvent(*direction))
                doc.pan.assert_called_once_with(*direction, count=.25)


if __name__ == '__main__':
    unittest.main()
