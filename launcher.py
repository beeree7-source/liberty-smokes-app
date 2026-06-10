import os
import sys

from streamlit.web import cli as stcli


def main() -> None:
    """Launch the bundled Streamlit app."""
    bundle_root = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    app_path = os.path.join(bundle_root, "app.py")

    sys.argv = [
        "streamlit",
        "run",
        app_path,
        "--global.developmentMode=false",
    ]
    raise SystemExit(stcli.main())


if __name__ == "__main__":
    main()
