"""Collect license texts for the installed dependency closure used by a build."""
import argparse
import importlib.metadata as metadata
import json
from pathlib import Path
import re

from packaging.requirements import Requirement


def generate(requirements_path, output):
    queue = [Requirement(line.strip()) for line in requirements_path.read_text().splitlines()
             if line.strip() and not line.lstrip().startswith('#')]
    visited = set()
    packages = {}
    while queue:
        requirement = queue.pop()
        if requirement.marker is not None and not requirement.marker.evaluate({'extra': ''}):
            continue
        name = re.sub(r'[-_.]+', '-', requirement.name).lower()
        extras = frozenset(requirement.extras)
        key = (name, extras)
        if key in visited:
            continue
        visited.add(key)
        distribution = metadata.distribution(requirement.name)
        packages[name] = distribution
        for raw in distribution.requires or []:
            dependency = Requirement(raw)
            if dependency.marker is None or any(dependency.marker.evaluate({'extra': extra})
                                                for extra in [''] + sorted(extras)):
                dependency.marker = None  # Already evaluated in the requesting package's extra context.
                queue.append(dependency)
    sections = ['Third-party Python dependency notices\nGenerated from the installed build environment.\n']
    inventory = []
    for name, distribution in sorted(packages.items()):
        licenses = []
        for item in distribution.files or []:
            if re.match(r'(?i)^(licen[cs]e|copying|notice|copyright)([._-]|$)', Path(str(item)).name):
                path = distribution.locate_file(item)
                if path.is_file():
                    licenses.append((str(item), path.read_text(errors='replace')))
        if not licenses:
            raise RuntimeError(f'No packaged license text found for {name} {distribution.version}')
        sections.append(f'\n=== {name} {distribution.version} ===\n')
        for path, text in licenses:
            sections.append(f'\n--- {path} ---\n{text}\n')
        inventory.append({'name': name, 'version': distribution.version,
                          'license_files': [path for path, _ in licenses]})
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(''.join(sections))
    output.with_suffix('.json').write_text(json.dumps(inventory, indent=2) + '\n')
    print(f'Collected notices for {len(inventory)} installed dependencies.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--requirements', type=Path, default=Path('requirements.txt'))
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    generate(args.requirements, args.output)
