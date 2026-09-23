"""Entry script for PyInstaller (it can't run a package's __main__ with relative imports)."""

import sys

from gmailcheck.cli import main

if __name__ == "__main__":
    sys.exit(main())
