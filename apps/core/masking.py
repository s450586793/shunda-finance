def mask_account(value: str) -> str:
    """Mask an account identifier while retaining its last four characters."""
    if not isinstance(value, str):
        raise TypeError("账户标识必须是字符串")
    suffix = value[-4:]
    return "*" * max(len(value) - 4, 0) + suffix
