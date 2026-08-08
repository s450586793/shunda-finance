import pytest

from apps.imports.registry import ImporterRegistry
from apps.imports.types import UnsupportedTemplateError
from tests.imports.fakes import FakeImporter


@pytest.fixture
def importer_registry():
    return ImporterRegistry()


def test_registry_rejects_unknown_headers(importer_registry):
    with pytest.raises(UnsupportedTemplateError, match="无法识别文件模板"):
        importer_registry.detect("unknown.xlsx", ["A", "B"])


@pytest.mark.parametrize("parsers", [(), (FakeImporter(), FakeImporter())])
def test_registry_rejects_empty_or_ambiguous_matches(parsers):
    registry = ImporterRegistry(parsers)

    with pytest.raises(UnsupportedTemplateError, match="无法识别文件模板"):
        registry.detect("input.csv", ["marker"])
