from acquisition.receipt.source import CaptureError
from acquisition.dataset.elements import UNDEFINED


class SequenceWalker:
    def __init__(self, reader):
        self.reader = reader

    def skip_sequence(self, element, limit=None):
        return self._walk('sequence', element.value_start, element.length, limit)

    def skip_item(self, position, length, limit=None):
        return self._walk('item', position, length, limit)

    def _walk(self, kind, position, length, limit):
        outer = len(self.reader.data) if limit is None else limit
        end = None if length == UNDEFINED else position + length
        stack = [(kind, end, outer)]
        while stack:
            kind, end, bound = stack[-1]
            ceiling = bound if end is None else end
            if position == ceiling and end is not None:
                stack.pop()
                continue
            if position >= ceiling:
                raise CaptureError('unterminated undefined sequence/item')
            if kind == 'sequence':
                tag, length, start = self.reader.item_header(position, ceiling)
                if tag == 0xe0dd:
                    if end is not None or length != 0:
                        raise CaptureError('unexpected sequence delimiter')
                    position = start
                    stack.pop()
                    continue
                if tag != 0xe000:
                    raise CaptureError('sequence requires items')
                child_end = None if length == UNDEFINED else start + length
                if length != UNDEFINED and (length % 2 or child_end > ceiling):
                    raise CaptureError('item exceeds sequence')
                stack.append(('item', child_end, ceiling))
                position = start
            else:
                if self.reader.data[position:position + 2] == b'\xfe\xff':
                    tag, length, start = self.reader.item_header(position, ceiling)
                    if tag != 0xe00d or end is not None or length != 0:
                        raise CaptureError('unexpected item delimiter')
                    position = start
                    stack.pop()
                    continue
                element = self.reader.read(position, ceiling)
                if element.vr == 'SQ':
                    child_end = None if element.length == UNDEFINED else element.end
                    stack.append(('sequence', child_end, ceiling))
                    position = element.value_start
                elif element.length == UNDEFINED:
                    raise CaptureError('undefined non-sequence in ignored item')
                else:
                    position = element.end
        return position
