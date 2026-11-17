from dataclasses import dataclass
from acquisition.receipt.source import CaptureError


@dataclass(frozen=True)
class ScanBoundary:
    marker_position: int
    restart_count: int


def scan_entropy(data, position, restart_interval=0):
    restart_count = 0
    expected_restart = 0
    while position < len(data):
        if data[position] != 0xff:
            position += 1
            continue
        start = position
        while position < len(data) and data[position] == 0xff:
            position += 1
        if position == len(data):
            raise CaptureError('truncated entropy marker')
        code = data[position]
        if code == 0:
            if position != start + 1:
                raise CaptureError('fill bytes before stuffed zero')
            position += 1
            continue
        if 0xd0 <= code <= 0xd7:
            if not restart_interval or code != 0xd0 + expected_restart:
                raise CaptureError('restart without DRI or wrong sequence')
            expected_restart = (expected_restart + 1) % 8
            restart_count += 1
            position += 1
            continue
        return ScanBoundary(start, restart_count)
    raise CaptureError('JPEG scan lacks terminating marker')
