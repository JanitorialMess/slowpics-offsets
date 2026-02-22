import argparse
import os
import platform
import sys
from pathlib import Path

LOADER_CONTENT = """from slowpics_offsets import SlowPicsOffsetsPlugin

__all__ = ["SlowPicsOffsetsPlugin"]
"""

def get_default_plugin_dir() -> Path:
    system = platform.system()
    if system == "Windows":
        return Path(os.environ["APPDATA"]) / "vspreview" / "plugins"
    else:
        return Path.home() / ".config" / "vspreview" / "plugins"


def migrate_legacy_plugin_file(plugin_dir: Path) -> None:
    legacy_path = plugin_dir / "slowpics-offsets.ppy"
    if not legacy_path.exists():
        return

    migrated_path = legacy_path.with_name(f"{legacy_path.name}.old")
    idx = 1
    while migrated_path.exists():
        migrated_path = legacy_path.with_name(f"{legacy_path.name}.old.{idx}")
        idx += 1

    legacy_path.rename(migrated_path)
    print(f"Migrated legacy plugin: {legacy_path.name} -> {migrated_path.name}")


def main():
    parser = argparse.ArgumentParser(description="Install the SlowPics Offsets plugin loader for VSPreview.")
    parser.add_argument(
        "--path",
        type=Path,
        help="Custom path to VSPreview plugins directory. Defaults to standard OS locations.",
        default=None
    )
    args = parser.parse_args()

    target_dir = args.path if args.path else get_default_plugin_dir()

    # Clean install into specific subdirectory
    plugin_dir = target_dir / "slowpics-offsets"

    if not plugin_dir.exists():
        try:
            plugin_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            print(f"Error creating directory: {e}")
            sys.exit(1)

    loader_path = plugin_dir / "loader.ppy"

    try:
        migrate_legacy_plugin_file(plugin_dir)
    except Exception as e:
        print(f"Warning: Could not migrate legacy plugin file: {e}")

    try:
        print(f"Installing loader to: {loader_path}")
        with open(loader_path, "w", encoding="utf-8") as f:
            f.write(LOADER_CONTENT)
        print("Success! The plugin is now linked to VSPreview.")
    except Exception as e:
        print(f"Error writing file: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
