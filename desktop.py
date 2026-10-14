"""
Desktop launcher for Video Scraper.

Starts the Flask app on your own machine and opens it in your default browser.
This is the entry point PyInstaller packages into VideoScraper.exe.
"""

import os
import socket
import sys
import threading
import webbrowser


def base_dir() -> str:
    # PyInstaller unpacks bundled files (ffmpeg, ffprobe) into sys._MEIPASS
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


# Make a bundled ffmpeg visible to shutil.which() BEFORE app.py is imported
os.environ["PATH"] = base_dir() + os.pathsep + os.environ.get("PATH", "")

import app as scraper  # noqa: E402


def free_port(start: int = 5000) -> int:
    for port in range(start, start + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("No free port found")


def main():
    port = free_port(int(os.environ.get("PORT", "5000")))
    url = f"http://127.0.0.1:{port}"
    threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    print(f"Video Scraper is running at {url}")
    print("Close this window (or press Ctrl+C) to quit.")
    scraper.app.run(host="127.0.0.1", port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
