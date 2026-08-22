"""Run the Factsheet Engine AI webapp:  python -m webapp

First boot builds the searchable holdings cache (data/webapp.db) from the
parsed data under data/ — this can take a minute. Subsequent boots are fast.
"""

import sys

from .main import run

if __name__ == "__main__":
    sys.exit(run())