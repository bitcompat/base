"""One runnable regression check for tree metadata and exact differences."""
import io
from pathlib import Path
import tarfile
import tempfile
from compare import differences, filesystem

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
print('Tree and difference regression check passed')
