from pathlib import Path

# Pytest imports this directory as the top-level ``updater`` package.
__path__.append(str(Path(__file__).parents[2] / "updater"))
