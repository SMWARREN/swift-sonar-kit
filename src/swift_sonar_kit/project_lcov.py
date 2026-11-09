"""Strict native LCOV validation without rewriting scanner report bytes.

Supports ordinary TN/SF/FN/FNDA/FNF/FNH/DA/BRDA/LF/LH/BRF/BRH records.
One complete record per source is required; unsupported LCOV extensions fail closed.
SPDX-License-Identifier: GPL-3.0-only
"""

from dataclasses import dataclass, field
import hashlib
import io
import json
from typing import Callable


@dataclass
class _Record:
    path: str
    source_lines: int
    count: Callable
    lines: dict = field(default_factory=dict)
    branches: dict = field(default_factory=dict)
    functions: dict = field(default_factory=dict)
    function_hits: dict = field(default_factory=dict)
    totals: dict = field(default_factory=dict)

    def line_number(self, value: str) -> int:
        number = self.count(value, 'LCOV source line', 1)
        if number > self.source_lines:
            raise ValueError('LCOV line is outside the current source')
        return number

    @staticmethod
    def unique(target: dict, key, value) -> None:
        if key in target:
            raise ValueError('LCOV repeats an observation or total')
        target[key] = value

    def observe(self, tag: str, value: str) -> None:
        if tag in {'LF', 'LH', 'BRF', 'BRH', 'FNF', 'FNH'}:
            self.unique(self.totals, tag, self.count(value, f'LCOV {tag}'))
        elif tag == 'DA':
            parts = value.split(',')
            if len(parts) not in {2, 3} or (len(parts) == 3 and not parts[2]):
                raise ValueError('Malformed LCOV DA observation')
            number = self.line_number(parts[0])
            hits = self.count(parts[1], 'LCOV line hits')
            self.unique(self.lines, number, (hits, parts[2] if len(parts) == 3 else None))
        elif tag == 'BRDA':
            parts = value.split(',')
            if len(parts) != 4:
                raise ValueError('Malformed LCOV BRDA observation')
            identity = (self.line_number(parts[0]), self.count(parts[1], 'LCOV block'),
                        self.count(parts[2], 'LCOV branch'))
            taken = None if parts[3] == '-' else self.count(parts[3], 'LCOV branch hits')
            # A physical branch location need not have a DA observation in native V8 LCOV.
            self.unique(self.branches, identity, taken)
        elif tag == 'FN':
            parts = value.split(',', 2)
            if len(parts) < 2:
                raise ValueError('Malformed LCOV FN definition')
            start = self.line_number(parts[0])
            # LCOV supports an optional end line before the function name.
            if len(parts) == 3 and parts[1].isascii() and parts[1].isdigit():
                end, name = self.line_number(parts[1]), parts[2]
                if end < start:
                    raise ValueError('LCOV function range is reversed')
            else:
                end, name = None, value.split(',', 1)[1]
            if not name:
                raise ValueError('LCOV function requires a name')
            self.unique(self.functions, name, (start, end))
        elif tag == 'FNDA':
            parts = value.split(',', 1)
            if len(parts) != 2 or not parts[1]:
                raise ValueError('Malformed LCOV FNDA observation')
            self.unique(self.function_hits, parts[1], self.count(parts[0], 'LCOV function hits'))
        else:
            raise ValueError(f'Unsupported LCOV record: {tag}')

    def finish(self) -> dict:
        if not self.lines and not self.branches:
            raise ValueError('LCOV source has no usable line or branch observations')
        if set(self.functions) != set(self.function_hits):
            raise ValueError('LCOV function definitions and observations differ')
        expected = {'LF': len(self.lines), 'LH': sum(hits > 0 for hits, _ in self.lines.values()),
                    'BRF': len(self.branches), 'BRH': sum(hits is not None and hits > 0 for hits in self.branches.values()),
                    'FNF': len(self.functions), 'FNH': sum(hits > 0 for hits in self.function_hits.values())}
        required = {'LF', 'LH'}
        if self.branches or {'BRF', 'BRH'} & self.totals.keys():
            required.update({'BRF', 'BRH'})
        if self.functions or {'FNF', 'FNH'} & self.totals.keys():
            required.update({'FNF', 'FNH'})
        if not required <= self.totals.keys() or any(self.totals[key] != expected[key] for key in self.totals):
            raise ValueError('LCOV totals are missing or inconsistent with observations')
        return {'path': self.path, 'lines': sorted(self.lines.items()),
                'branches': [[*identity, hits] for identity, hits in sorted(self.branches.items())],
                'functions': [[name, *bounds, self.function_hits[name]] for name, bounds in sorted(self.functions.items())]}


def validate_lcov(data: bytes, resolve_source: Callable, count: Callable,
                  max_records: int, max_path_chars: int) -> dict:
    """Validate bounded UTF-8 native LCOV; resolve_source applies current path inventory."""
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError as error:
        raise ValueError('LCOV must be UTF-8') from error
    if text.startswith('\ufeff'):
        raise ValueError('LCOV must not contain a byte order mark')
    files = {}
    record = None
    pending_test = None
    for index, raw in enumerate(io.StringIO(text), 1):
        if index > max_records:
            raise ValueError('LCOV exceeds the record bound')
        line = raw.removesuffix('\n').removesuffix('\r')
        if len(line) > max_path_chars * 2 or any(ord(char) < 32 or ord(char) == 127 for char in line):
            raise ValueError('LCOV record is oversized or contains control characters')
        if not line:
            continue
        if line == 'end_of_record':
            if record is None:
                raise ValueError('LCOV terminator has no source record')
            files[record.path] = record.finish()
            record = None
            pending_test = None
            continue
        tag, separator, value = line.partition(':')
        if not separator:
            raise ValueError('Malformed LCOV record')
        if tag == 'TN':
            if record is not None or pending_test is not None:
                raise ValueError('LCOV test name is misplaced')
            pending_test = value
        elif tag == 'SF':
            if record is not None:
                raise ValueError('LCOV source record is not terminated')
            path, source_lines = resolve_source(value)
            if path in files:
                raise ValueError('LCOV repeats a source file')
            record = _Record(path, source_lines, count)
        elif record is None:
            raise ValueError('LCOV observation has no source')
        else:
            record.observe(tag, value)
    if record is not None or pending_test is not None:
        raise ValueError('LCOV is truncated or missing end_of_record')
    if not files:
        raise ValueError('LCOV contains no usable source records')
    observations = [value for _, value in sorted(files.items())]
    canonical = json.dumps(observations, separators=(',', ':'), ensure_ascii=True).encode('ascii')
    return {'format': 'lcov', 'files': len(files),
            'lines': sum(len(item['lines']) for item in observations),
            'covered_lines': sum(hits > 0 for item in observations for _, (hits, _) in item['lines']),
            'branches': sum(len(item['branches']) for item in observations),
            'covered_branches': sum(branch[3] is not None and branch[3] > 0 for item in observations for branch in item['branches']),
            'unknown_branches': sum(branch[3] is None for item in observations for branch in item['branches']),
            'functions': sum(len(item['functions']) for item in observations),
            'covered_functions': sum(function[3] > 0 for item in observations for function in item['functions']),
            'observations_sha256': hashlib.sha256(canonical).hexdigest()}
