"""Source graph checks only: never run env.py, migrations, or database I/O."""
import ast
from contextlib import chdir, contextmanager, ExitStack
import io
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import warnings

from alembic.script import ScriptDirectory
from alembic.command import heads as alembic_heads
from alembic.config import Config


SCRIPTS = Path(__file__).resolve().parents[1] / 'alembic'
BRIDGES = {
    '20261105_0038': '20261105_0038_stage3_source_cache_parcel_index.py',
    '20261107_0039': '20261107_0039_stage3_landcover_generations.py',
    '20261115_0040': '20261115_0040_stage3_grid_repairs.py',
}


def source_graph(directory):
    graph = {}
    files = sorted((directory / 'versions').glob('*.py'))
    if len(files) > 128:
        raise AssertionError('Migration source budget exceeded')
    for path in files:
        if path.stat().st_size > 256 * 1024:
            raise AssertionError('Migration source budget exceeded')
        fields = {}
        for node in ast.parse(path.read_bytes(), filename=str(path)).body:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
            for target in targets:
                if isinstance(target, ast.Name) and target.id in ('revision', 'down_revision', 'depends_on'):
                    fields[target.id] = ast.literal_eval(node.value)
        if 'revision' not in fields:
            continue
        revision = fields['revision']
        if not isinstance(revision, str) or not revision or revision in graph:
            raise AssertionError('Invalid or duplicate migration revision')
        for key in ('down_revision', 'depends_on'):
            refs = fields.get(key)
            refs = () if refs is None else (refs,) if isinstance(refs, str) else tuple(refs)
            if any(not isinstance(ref, str) or not ref for ref in refs):
                raise AssertionError('Invalid migration reference')
            fields[key] = refs
        graph[revision] = fields
    return graph


def assert_closed(graph):
    visited, active = set(), set()
    def visit(revision):
        if revision not in graph:
            raise AssertionError('missing migration: ' + revision)
        if revision in active:
            raise AssertionError('Migration graph cycle')
        if revision in visited:
            return
        active.add(revision)
        for parent in graph[revision]['down_revision'] + graph[revision]['depends_on']:
            visit(parent)
        active.remove(revision)
        visited.add(revision)
    for revision in graph:
        visit(revision)


@contextmanager
def no_database():
    # Head discovery imports revision definitions, but must not run env.py or
    # any upgrade/downgrade, engine connection, operation or network request.
    with ExitStack() as stack:
        checks = [stack.enter_context(patch(name, side_effect=AssertionError('Database access forbidden')))
                  for name in ('sqlalchemy.create_engine', 'sqlalchemy.engine.Engine.connect',
                               'alembic.op.execute', 'alembic.op.get_bind',
                               'socket.socket.connect')]
        checks.append(stack.enter_context(patch.object(
            ScriptDirectory, 'run_env', side_effect=AssertionError('env.py execution forbidden'))))
        yield
        for check in checks:
            check.assert_not_called()


class MigrationGraphClosureTest(unittest.TestCase):
    def test_static_graph_is_closed_unique_and_acyclic(self):
        assert_closed(source_graph(SCRIPTS))

    def test_alembic_discovers_single_head_and_all_revisions_without_database(self):
        graph = source_graph(SCRIPTS)
        assert_closed(graph)
        expected_heads = set(graph) - {parent for value in graph.values() for parent in value['down_revision']}
        self.assertEqual(len(expected_heads), 1)
        with no_database():
            scripts = ScriptDirectory(str(SCRIPTS))
            self.assertEqual(set(scripts.get_heads()), expected_heads)
            self.assertEqual(scripts.get_current_head(), next(iter(expected_heads)))
            self.assertEqual({r.revision for r in scripts.walk_revisions()}, set(graph))
            output = io.StringIO()
            with chdir(SCRIPTS.parent):
                alembic_heads(Config(str(SCRIPTS.parent / 'alembic.ini'), stdout=output))
            self.assertIn(next(iter(expected_heads)), output.getvalue())
            self.assertIn('(head)', output.getvalue())

    def test_missing_bridge_files_reproduce_static_graph_failure(self):
        graph = source_graph(SCRIPTS)
        for absent in [(revision,) for revision in BRIDGES] + [tuple(BRIDGES)]:
            with self.subTest(absent=absent):
                broken = {k: v for k, v in graph.items() if k not in absent}
                with self.assertRaisesRegex(AssertionError, 'missing migration'):
                    assert_closed(broken)

    def test_native_alembic_rejects_missing_bridge_files_without_database(self):
        for absent in [(revision,) for revision in BRIDGES] + [tuple(BRIDGES)]:
            with self.subTest(absent=absent), tempfile.TemporaryDirectory(
                    prefix='.migration-graph-', dir=Path(__file__).resolve().parent) as temp:
                root = Path(temp)
                versions = root / 'versions'
                versions.mkdir()
                excluded = {BRIDGES[revision] for revision in absent}
                for path in (SCRIPTS / 'versions').glob('*.py'):
                    if path.name not in excluded:
                        shutil.copyfile(path, versions / path.name)
                with no_database(), warnings.catch_warnings():
                    warnings.simplefilter('ignore', UserWarning)
                    with self.assertRaises(KeyError):
                        ScriptDirectory(str(root)).get_heads()


if __name__ == '__main__':
    unittest.main()
