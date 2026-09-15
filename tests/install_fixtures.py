"""Copy the fixture images into ComfyUI's input/ folder so LoadImage can resolve them.

Run inside ComfyUI's Python environment:

    python tests/install_fixtures.py
"""

from __future__ import annotations

import glob
import os
import shutil
import sys

NODE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE_DIR = os.path.join(NODE_DIR, "tests", "fixtures")
COMFY_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(NODE_DIR)))

if COMFY_ROOT not in sys.path:
    sys.path.insert(0, COMFY_ROOT)


def main() -> int:
    try:
        import folder_paths
    except ImportError:
        print("Could not import ComfyUI's folder_paths.")
        print(f"Expected ComfyUI root at: {COMFY_ROOT}")
        print("Run this script with ComfyUI's Python environment.")
        return 1

    destination = folder_paths.get_input_directory()
    os.makedirs(destination, exist_ok=True)
    images = sorted(glob.glob(os.path.join(FIXTURE_DIR, "*.png")))
    if not images:
        print(f"No fixture images found in {FIXTURE_DIR}")
        return 1

    for source in images:
        target = os.path.join(destination, os.path.basename(source))
        shutil.copyfile(source, target)
        print(f"copied {os.path.basename(source)} -> {target}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
