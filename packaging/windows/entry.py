"""PyInstaller entry point.

PyInstaller runs scripts as ``__main__`` without a package context, so this file
only imports absolute names and hands control to the desktop launcher.
"""
from wot.desktop import run

if __name__ == "__main__":
    raise SystemExit(run())
