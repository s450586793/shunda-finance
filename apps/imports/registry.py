from collections.abc import Iterable
from typing import Protocol

from .parsers.agricultural_bank import AgriculturalBankImporter
from .parsers.tax_invoice import TaxInvoiceImporter
from .parsers.wechat import WechatImporter
from .types import ParsedRow, UnsupportedTemplateError


class Importer(Protocol):
    source_kinds: frozenset[str]

    def supports(self, filename: str, headers: list[str]) -> bool: ...

    def parse(self, file_obj) -> Iterable[ParsedRow]: ...


class ImporterRegistry:
    def __init__(self, parsers: Iterable[Importer] | None = None):
        self.parsers = (
            tuple(parsers)
            if parsers is not None
            else (TaxInvoiceImporter(), AgriculturalBankImporter(), WechatImporter())
        )

    def detect(self, filename: str, headers: list[str]) -> Importer:
        matches = [
            parser for parser in self.parsers if parser.supports(filename, headers)
        ]
        if len(matches) != 1:
            raise UnsupportedTemplateError("无法识别文件模板")
        return matches[0]
