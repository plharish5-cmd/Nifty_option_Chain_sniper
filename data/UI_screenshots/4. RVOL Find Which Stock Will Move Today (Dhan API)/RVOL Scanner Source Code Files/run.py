# -*- coding: utf-8 -*-
"""RVOL Scanner launcher."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rvolscan.app import main
if __name__ == "__main__":
    sys.exit(main())
