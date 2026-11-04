#!/usr/bin/env python3
"""8500 launcher; isolate changes from the protected8600 modules."""
import importlib.util
from pathlib import Path

if __name__ == '__main__':
    path=Path(__file__).resolve().parent/'analyst'/'terminal8500.py'
    spec=importlib.util.spec_from_file_location('terminal8500_runtime',path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent/'.env',override=False)
    import sys
    sys.path.insert(0,str(path.parent))
    from terminal8500_runtime import install
    install(module)
    module.main()
