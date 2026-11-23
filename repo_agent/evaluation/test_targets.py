"""Authorize repository tests from the frozen tree without importing test modules."""
import ast
from pathlib import Path
import re
import tempfile

from repo_agent.core.check_tools import relative_file
from repo_agent.core.tools import SKIP_DIRS, SKIP_FILES
from repo_agent.evaluation.artifacts import read_reference, reference
from repo_agent.evaluation.swebench import export_patch, git


def python_test_definitions(source):
    """Recognize direct Python test definitions. Dynamic cases need an explicit target."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return set()
    functions = (ast.FunctionDef, ast.AsyncFunctionDef)
    definitions = set()
    for node in tree.body:
        if isinstance(node, functions) and (node.name == 'test' or node.name.startswith('test_')):
            definitions.add((node.name,))
        elif isinstance(node, ast.ClassDef) and (
            node.name.startswith('Test') or any(
                (getattr(base, 'id', '') or getattr(base, 'attr', '')).endswith('TestCase')
                for base in node.bases)
        ):
            definitions.update((node.name, method.name) for method in node.body
                               if isinstance(method, functions) and method.name.startswith('test'))
    return definitions


class FrozenTestTargets:
    def __init__(self, workspace, base_commit, *, authorized=()):
        self.workspace, self.base_commit = workspace, base_commit
        self.authorized = tuple(authorized)

    def _source(self, path):
        parts = path.lower().split('/')
        if (not path.endswith('.py') or any(part in SKIP_DIRS for part in parts)
                or parts[-1] in SKIP_FILES or parts[-1].startswith('.env')):
            return None
        listed = git(self.workspace, 'ls-tree', '-z', self.base_commit, '--', ':(literal)' + path).stdout
        header, _, name = listed.removesuffix(b'\0').partition(b'\t')
        fields = header.split()
        if (len(fields) != 3 or fields[0] not in {b'100644', b'100755'}
                or fields[1] != b'blob' or name.decode('utf-8') != path):
            return None
        return git(self.workspace, 'cat-file', 'blob', fields[2].decode('ascii')).stdout

    def protects(self, path):
        if any(path == target.split('::', 1)[0] for target in self.authorized):
            return True
        source = self._source(path)
        return source is not None and bool(python_test_definitions(source))

    def read(self, target):
        if any(char in target for char in ('\r', '\n', '\0')):
            raise ValueError('Invalid test target')
        path, *selectors = target.split('::')
        path = relative_file(path)
        # A final pytest parameter id may refine an already recognized test.
        names = selectors.copy()
        if names and '[' in names[-1] and names[-1].endswith(']'):
            names[-1] = names[-1].split('[', 1)[0]
        if len(names) > 2 or any(not name.isidentifier() for name in names):
            raise ValueError('Select a test file, class or case in the frozen repository')
        frozen = self._source(path)
        if frozen is None:
            raise ValueError('Select an existing regular Python test at the frozen commit')
        if target not in self.authorized and not any(
                parts[:len(names)] == tuple(names) for parts in python_test_definitions(frozen)):
            raise ValueError('Select a test with a frozen definition or an explicitly authorized target')
        local = self.workspace / path
        if local.resolve() != local or not local.is_file() or local.read_bytes() != frozen:
            raise ValueError('Test file differs from the frozen commit or is linked')
        return frozen

    def export(self, directory):
        """Validate the exact patch before publishing it as a candidate."""
        with tempfile.TemporaryDirectory(prefix='p-', dir=directory) as temporary:
            staged = Path(temporary)
            ref = export_patch(staged, self.workspace, self.base_commit)
            patch = read_reference(staged, ref)
            if patch:
                if re.search(rb'^(?:rename|copy) (?:from|to) ', patch, re.M):
                    raise ValueError('Rename and copy patches require review')
                names = git(self.workspace, 'apply', '--numstat', '-z', '--',
                            str(staged / ref['path'])).stdout.decode('utf-8').split('\0')
                for row in names:
                    if not row:
                        continue
                    fields = row.split('\t', 2)
                    if len(fields) != 3 or not fields[2]:
                        raise ValueError('Patch paths require review')
                    if self.protects(fields[2]):
                        raise ValueError('Frozen repository tests changed; patch requires review')
        output = directory / 'candidate.patch'
        with output.open('xb') as stream:
            stream.write(patch)
        return reference(directory, output)
