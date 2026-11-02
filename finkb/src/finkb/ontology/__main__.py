"""``python -m finkb.ontology`` 入口。"""

from __future__ import annotations

import sys

from finkb.ontology.cli import main

if __name__ == "__main__":
    sys.exit(main())
