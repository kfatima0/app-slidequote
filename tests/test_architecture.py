"""The architectural rules, enforced rather than documented.

Both rules exist for the same reason: to keep the mock-to-real migration a
composition change instead of a refactor. A convention that is only written down
erodes; these fail the build.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import ClassVar

import pytest

SRC = Path(__file__).resolve().parents[1] / "backend" / "src" / "slidequote"

#: Layers that must not know an adapter exists.
PURE_LAYERS = ("domain", "app", "ports")

#: The one module allowed to name concrete adapters, plus the layers whose job
#: is to be one. Everything else in PURE_LAYERS is checked.
COMPOSITION = {"composition.py"}


def _python_files(*packages: str) -> list[Path]:
    files: list[Path] = []
    for package in packages:
        files.extend(sorted((SRC / package).rglob("*.py")))
    return files


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


def _relative(path: Path) -> str:
    return str(path.relative_to(SRC)).replace("\\", "/")


class TestDependencyDirection:
    """domain/, app/ and ports/ depend on interfaces only."""

    @pytest.mark.parametrize("path", _python_files(*PURE_LAYERS), ids=_relative)
    def test_no_adapter_imports(self, path: Path) -> None:
        offenders = [i for i in _imports(path) if i.startswith("slidequote.adapters")]
        assert not offenders, (
            f"{_relative(path)} imports {offenders}. Swapping a mock for a real "
            f"integration must be a change to composition.py, not to business logic."
        )

    @pytest.mark.parametrize("path", _python_files("domain"), ids=_relative)
    def test_the_domain_imports_nothing_from_the_app_layer(self, path: Path) -> None:
        """Dependencies point inward. The domain is the centre."""
        outward = ("slidequote.app", "slidequote.api", "slidequote.config")
        offenders = [i for i in _imports(path) if i.startswith(outward)]
        assert not offenders, f"{_relative(path)} imports {offenders}"

    def test_composition_is_the_only_place_that_names_adapters(self) -> None:
        """Not a rule about tidiness: it is what makes the migration plan honest.

        Every row of MIGRATION_PLAN.md §1 promises "files touched: adapters +
        composition". This test is that promise.
        """
        for path in _python_files(*PURE_LAYERS):
            assert path.name not in COMPOSITION


class TestTimeIsInjected:
    """No wall-clock reads in business logic (§2.5).

    A single `datetime.now()` under domain/ or app/ would be a second clock that
    the demo cannot accelerate and a test cannot control, and it would only show
    up as a flaky timeout months later.
    """

    CLOCK_CALLS: ClassVar[set[tuple[str, str]]] = {
        ("datetime", "now"),
        ("datetime", "utcnow"),
        ("time", "time"),
    }

    @pytest.mark.parametrize("path", _python_files("domain", "app"), ids=_relative)
    def test_no_wall_clock_reads(self, path: Path) -> None:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        offenders: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            value = node.func.value
            if isinstance(value, ast.Name) and (value.id, node.func.attr) in self.CLOCK_CALLS:
                offenders.append(f"{value.id}.{node.func.attr}() at line {node.lineno}")

        assert not offenders, (
            f"{_relative(path)} reads the wall clock: {offenders}. "
            f"Time is an injected dependency; take it from the Clock port."
        )


class TestModelsAreBuildable:
    """Every Pydantic model must be constructible at runtime.

    A field annotated with a type imported only under TYPE_CHECKING type-checks
    perfectly and then raises the first time the model is instantiated. That has
    already happened twice here — once across all four ports, once on the event
    bus — and both times only surfaced when something actually ran.
    """

    def test_every_model_in_the_package_can_be_built(self) -> None:
        import importlib
        import pkgutil

        import pydantic

        import slidequote

        failures: list[str] = []
        for module_info in pkgutil.walk_packages(slidequote.__path__, prefix="slidequote."):
            module = importlib.import_module(module_info.name)
            for name, obj in vars(module).items():
                if not isinstance(obj, type) or not issubclass(obj, pydantic.BaseModel):
                    continue
                if obj.__module__ != module_info.name:
                    continue
                try:
                    obj.model_rebuild(_types_namespace=None)
                except Exception as exc:
                    failures.append(f"{module_info.name}.{name}: {exc}")

        assert not failures, "models that cannot be built at runtime:\n" + "\n".join(failures)
