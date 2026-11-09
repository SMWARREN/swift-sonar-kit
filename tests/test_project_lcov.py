import hashlib
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from swift_sonar_kit import project_artifacts as pa


VALID = ('TN:\nSF:src/app.ts\nFN:1,main\nFNDA:2,main\nFNF:1\nFNH:1\n'
         'DA:1,2\nDA:3,0\nBRDA:2,0,0,-\nBRDA:2,0,1,2\n'
         'LF:2\nLH:1\nBRF:2\nBRH:1\nend_of_record\n')


class JavaScriptCoverageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / 'src').mkdir()
        (self.root / 'build').mkdir()
        (self.root / 'src/app.ts').write_text('one\ntwo\nthree\n')
        (self.root / 'src/check.test.ts').write_text('test\n')
        self.inventory = {'sources': ['src/app.ts'], 'tests': ['src/check.test.ts']}
        self.artifact = {'kind': 'javascript-coverage', 'path': 'build/lcov.info'}
        self.now = time.time()

    def validate(self, data=VALID):
        report = self.root / self.artifact['path']
        report.write_bytes(data.encode() if isinstance(data, str) else data)
        return pa.validate_artifact(self.root, self.artifact, self.inventory, self.now - 1, self.now + 30)

    def test_native_branch_only_line_and_unknown_taken_preserved(self):
        result = self.validate()
        self.assertEqual(result['coverage']['format'], 'lcov')
        self.assertEqual(result['coverage']['lines'], 2)
        self.assertEqual(result['coverage']['covered_lines'], 1)
        self.assertEqual(result['coverage']['branches'], 2)
        self.assertEqual(result['coverage']['covered_branches'], 1)
        self.assertEqual(result['coverage']['unknown_branches'], 1)
        self.assertEqual(result['coverage']['functions'], 1)
        self.assertEqual(result['digest']['sha256'], hashlib.sha256(VALID.encode()).hexdigest())
        self.assertEqual((self.root / 'build/lcov.info').read_bytes(), VALID.encode())

    def test_absolute_source_crlf_and_optional_checksum(self):
        data = VALID.replace('SF:src/app.ts', f'SF:{self.root}/src/app.ts').replace('DA:1,2', 'DA:1,2,abcdef').replace('\n', '\r\n')
        self.assertEqual(self.validate(data)['coverage']['files'], 1)

    def test_zero_function_and_branch_totals(self):
        data = 'SF:src/app.ts\nFNF:0\nFNH:0\nDA:1,0\nLF:1\nLH:0\nBRF:0\nBRH:0\nend_of_record\n'
        self.assertEqual(self.validate(data)['coverage']['covered_lines'], 0)

    def test_multiple_sources_and_function_end_line(self):
        (self.root / 'src/other.jsx').write_text('a\nb\nc\n')
        self.inventory['sources'].append('src/other.jsx')
        data = VALID + VALID.replace('src/app.ts', 'src/other.jsx').replace('FN:1,main', 'FN:1,3,main')
        self.assertEqual(self.validate(data)['coverage']['files'], 2)

    def test_branch_counts_remain_distinct_from_line_hits(self):
        first = self.validate()['coverage']['observations_sha256']
        second = self.validate(VALID.replace('BRDA:2,0,0,-', 'BRDA:2,0,0,0'))['coverage']['observations_sha256']
        self.assertNotEqual(first, second)

    def test_branch_only_source_record_keeps_zero_line_totals(self):
        data = ('SF:src/app.ts\nBRDA:2,0,0,-\nLF:0\nLH:0\n'
                'BRF:1\nBRH:0\nend_of_record\n')
        coverage = self.validate(data)['coverage']
        self.assertEqual(coverage['lines'], 0)
        self.assertEqual(coverage['branches'], 1)
        self.assertEqual(coverage['unknown_branches'], 1)

    def test_function_names_can_contain_commas(self):
        data = VALID.replace('FN:1,main', 'FN:1,main,callback').replace(
            'FNDA:2,main', 'FNDA:2,main,callback')
        self.assertEqual(self.validate(data)['coverage']['covered_functions'], 1)

    def test_rejects_malformed_empty_truncated_and_duplicate_records(self):
        samples = ['', 'TN:\n', 'end_of_record\n', VALID.replace('end_of_record\n', ''),
                   VALID + 'TN:unfinished\n', VALID + VALID, VALID.replace('SF:src/app.ts', 'DA:1,1'),
                   VALID.replace('DA:1,2', 'DA:1,2\nDA:1,2'), VALID.replace('LF:2', 'LF:2\nLF:2'),
                   VALID.replace('BRDA:2,0,0,-', 'BRDA:2,0,0,-\nBRDA:2,0,0,-'),
                   VALID.replace('FN:1,main', 'FN:1,main\nFN:1,main'),
                   VALID.replace('FNDA:2,main', 'FNDA:2,main\nFNDA:2,main'),
                   VALID.replace('LF:2\n', ''), VALID.replace('LH:1\n', ''),
                   VALID.replace('BRH:1\n', ''), VALID.replace('FNH:1\n', ''),
                   VALID.replace('FNDA:2,main\n', ''), VALID.replace('FNDA:2,main', 'FNDA:2,other'),
                   VALID.replace('DA:1,2', 'UNKNOWN:1,2'), '\ufeff' + VALID, b'\xff']
        for sample in samples:
            with self.subTest(sample=sample), self.assertRaises(ValueError):
                self.validate(sample)

    def test_rejects_invalid_numbers_bounds_and_inconsistent_totals(self):
        substitutions = [('DA:1,2', value) for value in ('DA:0,2', 'DA:4,2', 'DA:-1,2', 'DA:one,2', 'DA:1,-1', 'DA:1,1.2', 'DA:1,NaN', 'DA:1,9999999999999')]
        substitutions += [('BRDA:2,0,0,-', value) for value in ('BRDA:4,0,0,-', 'BRDA:2,-1,0,-', 'BRDA:2,0,x,-', 'BRDA:2,0,0,-2', 'BRDA:2,0,0,NaN')]
        substitutions += [('LF:2', 'LF:3'), ('LH:1', 'LH:2'), ('BRF:2', 'BRF:3'), ('BRH:1', 'BRH:2'), ('FNF:1', 'FNF:2'), ('FNH:1', 'FNH:0'), ('FN:1,main', 'FN:4,main'), ('FN:1,main', 'FN:2,1,main'), ('FNDA:2,main', 'FNDA:-1,main')]
        for before, after in substitutions:
            with self.subTest(after=after), self.assertRaises(ValueError):
                self.validate(VALID.replace(before, after))

    def test_rejects_test_outside_parent_non_source_and_wrong_language_paths(self):
        for path in ('src/check.test.ts', '../outside.ts', '/outside.ts', 'src/missing.ts', 'src/../src/app.ts'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.validate(VALID.replace('src/app.ts', path))
        (self.root / 'src/data.py').write_text('a\nb\nc\n')
        self.inventory['sources'].append('src/data.py')
        with self.assertRaises(ValueError):
            self.validate(VALID.replace('src/app.ts', 'src/data.py'))

    def test_rejects_source_and_report_symlinks(self):
        (self.root / 'src/linked.ts').symlink_to(self.root / 'src/app.ts')
        self.inventory['sources'].append('src/linked.ts')
        with self.assertRaises(ValueError):
            self.validate(VALID.replace('src/app.ts', 'src/linked.ts'))
        (self.root / 'build/lcov.info').unlink()
        (self.root / 'build/original.info').write_text(VALID)
        (self.root / 'build/lcov.info').symlink_to(self.root / 'build/original.info')
        with self.assertRaises(ValueError):
            pa.validate_artifact(self.root, self.artifact, self.inventory, self.now - 1, self.now + 30)

    def test_enforces_record_and_size_bounds(self):
        with patch.object(pa, 'MAX_RECORDS', 2), self.assertRaises(ValueError):
            self.validate()
        with patch.object(pa, 'MAX_XML_BYTES', 8), self.assertRaises(ValueError):
            self.validate()

    def test_rejects_stale_and_mutated_artifacts(self):
        self.validate()
        report = self.root / 'build/lcov.info'
        os.utime(report, (self.now - 100, self.now - 100))
        with self.assertRaises(ValueError):
            pa.validate_artifact(self.root, self.artifact, self.inventory, self.now - 1, self.now + 30)
        self.validate()
        original = pa._validate_coverage
        def mutate(*args):
            result = original(*args)
            report.write_text(VALID + '\n')
            return result
        with patch.object(pa, '_validate_coverage', side_effect=mutate), self.assertRaises(ValueError):
            pa.validate_artifact(self.root, self.artifact, self.inventory, self.now - 1, self.now + 30)


if __name__ == '__main__':
    unittest.main()
