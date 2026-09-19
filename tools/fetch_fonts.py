#!/usr/bin/env python3
"""Compatibility entrypoint for the shared reader-font updater."""
from pathlib import Path
import runpy


if __name__ == "__main__":
    script = Path.home() / "projects" / "hermes-reader-fonts" / "fetch_fonts.py"
    runpy.run_path(str(script), run_name="__main__")
