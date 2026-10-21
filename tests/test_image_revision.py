"""Check the actual workflow's revision arguments for publication and preview."""
from pathlib import Path
import re

workflow = (Path(__file__).resolve().parents[1] / '.github/workflows/build.yaml').read_text()
for job in ['build_package_amd64', 'build_package_arm64', 'build_package']:
    section = workflow.split(f'\n  {job}:\n', 1)[1].split('\n  build_package', 1)[0]
    block = section.split('build-args: |-', 1)[1].split('push:', 1)[0]
    value = re.search(r'^\s*IMAGE_REVISION=(.+)$', block, re.M).group(1)
    # The authoritative value follows caller-supplied arguments.
    assert block.index('inputs.build-args') < block.index('IMAGE_REVISION=')
    for push, revision in [(True, '1'), (True, '7'), (False, '7')]:
        if value.startswith('${{'):
            expression = value[3:-2].strip().replace('inputs.push', 'push')
            expression = expression.replace('steps.version-tags.outputs.revision', 'revision')
            expression = expression.replace('&&', 'and').replace('||', 'or')
            actual = eval(expression, {'__builtins__': {}}, {'push': push, 'revision': revision})
        else:
            actual = value
        expected = revision if job == 'build_package' and push else '0'
        assert actual == expected, (job, push, actual, expected)
print('PASS: both native builds and previews use 0; publication uses the computed tag revision')
