#!/usr/bin/env python3
import argparse
import os
import sys
from pathlib import Path
from urllib.parse import urljoin, urlsplit

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    import django
    from django.template.loader import render_to_string
except ModuleNotFoundError as exc:
    venv_python = PROJECT_ROOT / ".venv" / "bin" / "python"
    if exc.name == "django" and venv_python.exists() and Path(sys.executable) != venv_python:
        os.execv(str(venv_python), [str(venv_python), __file__, *sys.argv[1:]])
    raise


def _absolute_root(url: str) -> str:
    parsed = urlsplit(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Render the Word add-in XML manifest for a fixed public base URL.")
    parser.add_argument("--base-url", required=True, help="Public HTTPS base URL, for example https://legal-editor.onrender.com")
    parser.add_argument("--output", help="Optional output file path. Defaults to stdout.")
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/") + "/"
    if not base_url.startswith("https://"):
        raise SystemExit("--base-url must start with https://")

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    django.setup()

    taskpane_url = urljoin(base_url, "word-addin/taskpane/")
    commands_url = urljoin(base_url, "word-addin/commands/")
    icon_url = urljoin(base_url, "static/word-addin/icon.png")
    rendered = render_to_string(
        "editor/word_addin_manifest.xml",
        {
            "app_domain": _absolute_root(taskpane_url),
            "commands_url": commands_url,
            "taskpane_url": taskpane_url,
            "icon_url": icon_url,
        },
    )

    if args.output:
        output_path = Path(args.output)
        output_path.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
