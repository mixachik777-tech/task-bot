"""Smoke-import gate: грузит все Python-модули из app/ и валит build при
ImportError. Ловит забытые/циклические импорты, опечатки в from-import.

NameError внутри тела функции не ловится — для этого слой ruff F821 в
Dockerfile перед этим шагом.

Запускается в Dockerfile build без БД и без pytest-fixtures, поэтому
ходить во внешние сервисы (Postgres/Redis/Telegram) при импорте нельзя.
"""

from __future__ import annotations

import importlib
import pkgutil
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def iter_modules() -> list[str]:
    names: list[str] = []
    for info in pkgutil.walk_packages([str(APP)], prefix="app."):
        if info.ispkg:
            continue
        names.append(info.name)
    return sorted(names)


def main() -> int:
    failed: list[tuple[str, str]] = []
    modules = iter_modules()
    for name in modules:
        try:
            importlib.import_module(name)
        except Exception:
            failed.append((name, traceback.format_exc()))

    if failed:
        for name, tb in failed:
            print(f"\n=== IMPORT FAILED: {name} ===", file=sys.stderr)
            print(tb, file=sys.stderr)
        print(
            f"\nsmoke-imports: {len(failed)} module(s) failed out of {len(modules)}",
            file=sys.stderr,
        )
        return 1

    print(f"smoke-imports: OK ({len(modules)} modules)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
