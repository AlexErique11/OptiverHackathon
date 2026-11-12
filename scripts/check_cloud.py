"""Inspect runtime and import availability without connecting or exposing credentials."""
import importlib.util
import sys


def main() -> int:
    print(f"Python executable: {sys.executable}")
    print(f"Python version: {sys.version.split()[0]}")
    if sys.version_info < (3, 10):
        print("Python 3.10 or newer is required.")
        return 1
    spec = importlib.util.find_spec("optibook")
    if spec is None:
        print("Optibook is unavailable in this interpreter.")
        print("In Optiver cloud, select /opt/venv/bin/python. Local offline tests need no client.")
        return 1
    print(f"Optibook package: {spec.origin}")
    print("Cloud dependency check passed. No exchange connection or orders were made.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
