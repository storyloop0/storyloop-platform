"""Static public Harness import guard, shared by source and test scans."""
import ast
from pathlib import Path

PUBLIC_MODULES = frozenset({
    'storyloop_harness', 'storyloop_harness.contracts', 'storyloop_harness.ports',
    'storyloop_harness.advanced', 'storyloop_harness.generation',
    'storyloop_harness.telemetry', 'storyloop_harness.usage', 'storyloop_harness.testing',
    'storyloop_harness.context',
})
PRIVATE_ROOTS = frozenset({'core', 'runtime', 'world', 'agents', 'models', 'adapters'})
PRIVATE_TESTING_MODULES = frozenset({'memory', 'offline_model', 'projection'})
PRIVATE_CONTEXT_MODULES = frozenset({'contracts', 'types', 'budget', 'projection', 'selection', 'history', 'metrics'})


def harness_import_violations(source):
    tree = ast.parse(source)
    aliases = {}
    violations = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                aliases[item.asname or item.name.split('.')[0]] = item.name if item.asname else item.name.split('.')[0]
                if item.name.startswith('storyloop_harness') and item.name not in PUBLIC_MODULES:
                    violations.add((node.lineno, item.name))
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ''
            for item in node.names:
                aliases[item.asname or item.name] = f'{module}.{item.name}'
                if ((module == 'storyloop_harness' and item.name in PRIVATE_ROOTS) or
                        (module == 'storyloop_harness.testing' and item.name in PRIVATE_TESTING_MODULES) or
                        (module == 'storyloop_harness.context' and item.name in PRIVATE_CONTEXT_MODULES)):
                    violations.add((node.lineno, f'{module}.{item.name}'))
            if module.startswith('storyloop_harness') and module not in PUBLIC_MODULES:
                violations.add((node.lineno, module))

    def dotted(node):
        if isinstance(node, ast.Name):
            return aliases.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            base = dotted(node.value)
            return f'{base}.{node.attr}' if base else ''
        return ''

    def private_target(target):
        parts = target.split('.')
        return (len(parts) > 1 and parts[0] == 'storyloop_harness' and (
            parts[1] in PRIVATE_ROOTS or
            (len(parts) > 2 and parts[1] == 'testing' and
             parts[2] in PRIVATE_TESTING_MODULES) or
            (len(parts) > 2 and parts[1] == 'context' and
             parts[2] in PRIVATE_CONTEXT_MODULES)))

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            target = dotted(node)
            if private_target(target):
                violations.add((node.lineno, target))
        if isinstance(node, ast.Call):
            called = dotted(node.func)
            keyword = {'importlib.import_module': 'name', '__import__': 'name',
                       'unittest.mock.patch': 'target'}.get(called)
            if keyword is None:
                continue
            argument = node.args[0] if node.args else next(
                (item.value for item in node.keywords if item.arg == keyword), None)
            if not isinstance(argument, ast.Constant):
                continue
            target = argument.value
            if not isinstance(target, str) or not target.startswith('storyloop_harness'):
                continue
            if called in {'importlib.import_module', '__import__'} and target not in PUBLIC_MODULES:
                violations.add((node.lineno, target))
            elif called == 'unittest.mock.patch' and private_target(target):
                violations.add((node.lineno, target))
    return sorted(violations)


def platform_import_violations(root):
    root = Path(root)
    sources = list((root / 'src/storyloop_platform').rglob('*.py')) + list((root / 'tests').rglob('*.py'))
    assert sources, f'No Platform sources or tests in {root}'
    return [(path.relative_to(root), line, target) for path in sources
            for line, target in harness_import_violations(path.read_text(encoding='utf-8-sig'))]
