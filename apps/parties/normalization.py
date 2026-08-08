from unicodedata import normalize


def normalize_party_text(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("往来单位文本必须是字符串")

    normalized = " ".join(normalize("NFKC", value).split()).casefold()
    if not normalized:
        raise ValueError("往来单位文本不能为空")
    return normalized
