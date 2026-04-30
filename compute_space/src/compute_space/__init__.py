from pathlib import Path

# This file lives at compute_space/src/compute_space/__init__.py.
# COMPUTE_SPACE_PACKAGE_DIR is the `compute_space/` project root (parent of `src/`),
# not the inner package — many call sites use it as the cwd for pixi/uv commands.
COMPUTE_SPACE_PACKAGE_DIR = Path(__file__).resolve().parent.parent.parent
OPENHOST_PROJECT_DIR = COMPUTE_SPACE_PACKAGE_DIR.parent
