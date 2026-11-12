from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import termpdf


class BibliographyTests(unittest.TestCase):
    def test_unconfigured_bibliography_does_not_prevent_opening_a_pdf(self):
        for bibtool in (None, '/usr/bin/bibtool'):
            with self.subTest(bibtool=bibtool), \
                    patch.object(termpdf.config, 'BIBTEX', ''), \
                    patch.object(termpdf.shutil, 'which', return_value=bibtool), \
                    patch.object(termpdf.subprocess, 'Popen',
                                 side_effect=AssertionError('Unexpected external command')):
                self.assertIsNone(termpdf.citekey_from_path('/tmp/default.pdf'))
                with self.assertRaisesRegex(SystemExit, 'Cannot find file associated with example'):
                    termpdf.path_from_citekey('example')

    def test_configured_bibliography_still_resolves_citekeys_and_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            bibliography = Path(directory) / 'references.bib'
            bibliography.write_text(
                '@article{example, title={Example}, file={/tmp/default.pdf}}\n')
            with patch.object(termpdf.config, 'BIBTEX', str(bibliography)), \
                    patch.object(termpdf.shutil, 'which', return_value=None):
                self.assertEqual(termpdf.citekey_from_path('/tmp/default.pdf'), 'example')
                self.assertEqual(termpdf.path_from_citekey('example'), '/tmp/default.pdf')


if __name__ == '__main__':
    unittest.main()
