"""One runnable regression check for tree metadata and exact differences."""
import io
from pathlib import Path
import tarfile
import tempfile
from compare import differences, filesystem, size_comparison

with tempfile.TemporaryDirectory() as directory:
    archive = Path(directory) / 'image.tar'
    with tarfile.open(archive, 'w') as stream:
        entry = tarfile.TarInfo('opt/app')
        entry.mode, entry.uid, entry.gid = 0o755, 1001, 0
        stream.addfile(entry, io.BytesIO(b''))
        link = tarfile.TarInfo('usr/bin/app')
        link.type, link.linkname = tarfile.SYMTYPE, '/opt/app'
        stream.addfile(link)
    tree = filesystem(archive)
    assert tree['/opt/app']['uid'] == 1001
    assert tree['/opt/app']['mode'] == '0o755'
    assert tree['/usr/bin/app']['link'] == '/opt/app'
    changed = {**tree, '/extra': tree['/opt/app']}
    changed['/opt/app'] = {**tree['/opt/app'], 'mode': '0o644'}
    diff = differences(tree, changed)
    assert set(diff) == {'/extra', '/opt/app'}
    assert diff['/extra']['reference'] is None
    assert differences(tree, tree) == {}
    assert differences({'ENV': 'old'}, {'ENV': 'new'})['ENV']['candidate'] == 'new'
reference = {'layer_count': 1, 'size_bytes': 100}
assert size_comparison(reference, {'layer_count': 1, 'size_bytes': 80})['delta_percent'] == -20
# A small multi-layer build must not be reported as smaller than a flat image.
pending = size_comparison(reference, {'layer_count': 2, 'size_bytes': 50})
assert pending['status'] == 'requires-flatten' and 'delta_percent' not in pending
assert size_comparison({'layer_count': 2}, {'layer_count': 1})['status'] == 'requires-flatten'
print('Tree, difference and flattened-size regression check passed')
