from dataclasses import dataclass
import json
from acquisition.archive.schema import validate_manifest, IntegrityError


@dataclass(frozen=True)
class DeliveryManifest:
    value: dict


def frame_filename(index):
    if type(index) is not int or index < 0:
        raise ValueError('frame index must be nonnegative')
    return 'frame-' + str(index).zfill(6) + '.jpg'


def build_delivery_manifest(manifest):
    validate_manifest(manifest)
    value = json.loads(json.dumps(manifest))
    value['files'] = [{'index': frame['index'], 'filename': frame_filename(frame['index']),
                       'digest': frame['digest'], 'size': frame['size']}
                      for frame in value['frames']]
    if len({entry['filename'] for entry in value['files']}) != len(value['frames']):
        raise IntegrityError('duplicate delivery filename')
    return DeliveryManifest(value)


def encode_delivery_manifest(manifest):
    return json.dumps(manifest.value, ensure_ascii=True, sort_keys=True,
                      separators=(',', ':')) + '\n'
