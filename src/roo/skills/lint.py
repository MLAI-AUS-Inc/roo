"""Run from src: python -m roo.skills.lint [--required name ...]."""
import argparse
from pathlib import Path
from .catalogue import inspect_catalogue


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--skills-dir', type=Path, default=Path('skills'))
    parser.add_argument('--required', nargs='*', default=[])
    parser.add_argument('--public-defaults', action='store_true')
    args = parser.parse_args(argv)
    required = set(args.required)
    if args.public_defaults:
        from ..config import Settings
        required.update(Settings.PUBLIC_DEFAULT_SKILLS)
    packages = inspect_catalogue(args.skills_dir)
    from .registry import HANDLERS, EVENT_ONLY_SKILLS, REQUIRED_CLIENTS
    errors = 0
    for package in packages:
        for item in package.diagnostics:
            print(f'{item.severity.upper()} {item.path}: {item.message}')
            errors += item.severity == 'error'
        if package.name and not any(d.severity == 'error' for d in package.diagnostics) and package.name not in HANDLERS and package.name not in EVENT_ONLY_SKILLS:
            print(f'ERROR {package.path}: no registered execution handler')
            errors += 1
        if package.name in REQUIRED_CLIENTS and (package.path.name != 'SKILL.md'
                                                or not (package.path.parent / 'client.py').is_file()):
            print(f'ERROR {package.path}: required client.py is missing')
            errors += 1
    present = {p.name for p in packages if not any(d.severity == 'error' for d in p.diagnostics)}
    for name in sorted(required - present):
        print(f'ERROR {args.skills_dir}: required skill {name!r} is missing or invalid')
        errors += 1
    print(f'{len(packages)} packages inspected; {errors} errors. No skill clients imported.')
    return int(bool(errors))


if __name__ == '__main__':
    raise SystemExit(main())
