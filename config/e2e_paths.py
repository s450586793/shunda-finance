from dataclasses import dataclass
from pathlib import Path

SAFETY_ERROR = "E2E path validation failed"


@dataclass(frozen=True)
class E2EPaths:
    results_root: Path
    database: Path
    media: Path
    static: Path


def validate_e2e_paths(project_root: str | Path) -> E2EPaths:
    project_root = Path(project_root)
    if not project_root.is_absolute():
        raise RuntimeError(SAFETY_ERROR)

    results_root = project_root / "test-results"
    paths = E2EPaths(
        results_root=results_root,
        database=results_root / "e2e.sqlite3",
        media=results_root / "e2e-media",
        static=results_root / "e2e-static",
    )
    for path in (
        project_root,
        paths.results_root,
        paths.database,
        paths.media,
        paths.static,
    ):
        _validate_lexical_path(path)
    return paths


def _validate_lexical_path(path: Path) -> None:
    try:
        if path.is_symlink() or path.resolve(strict=False) != path:
            raise RuntimeError(SAFETY_ERROR)
    except OSError:
        raise RuntimeError(SAFETY_ERROR) from None
