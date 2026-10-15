"""
Android launcher (Kivy + python-for-android).

Runs the Flask app inside the APK and opens it in the phone's browser at
http://127.0.0.1:5000. Chrome handles the downloads, so files land in your normal
Downloads folder. Keep this app open (or in recents) while downloading.
"""

import os
import socket
import sys
import tempfile
import threading

# Pure-Python dependencies are bundled in ./vendor by the build workflow
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

PORT = 5000
URL = f"http://127.0.0.1:{PORT}"


def setup_android_env():
    """Point temp files at app storage and give Python a CA bundle (Android has none)."""
    try:
        from android.storage import app_storage_path
    except ImportError:  # not running on Android
        return
    tmp = os.path.join(app_storage_path(), "tmp")
    os.makedirs(tmp, exist_ok=True)
    os.environ["TMPDIR"] = tmp
    tempfile.tempdir = tmp
    try:
        import certifi
        os.environ["SSL_CERT_FILE"] = certifi.where()
        os.environ["REQUESTS_CA_BUNDLE"] = certifi.where()
    except ImportError:
        pass


setup_android_env()

from kivy.app import App  # noqa: E402
from kivy.clock import Clock  # noqa: E402
from kivy.uix.boxlayout import BoxLayout  # noqa: E402
from kivy.uix.button import Button  # noqa: E402
from kivy.uix.label import Label  # noqa: E402


def run_server():
    import app as scraper
    scraper.app.run(host="127.0.0.1", port=PORT, debug=False, threaded=True, use_reloader=False)


def open_in_browser():
    try:
        from jnius import autoclass
        PythonActivity = autoclass("org.kivy.android.PythonActivity")
        Intent = autoclass("android.content.Intent")
        Uri = autoclass("android.net.Uri")
        intent = Intent(Intent.ACTION_VIEW, Uri.parse(URL))
        PythonActivity.mActivity.startActivity(intent)
    except Exception as exc:  # noqa: BLE001
        print("Could not open the browser:", exc)


class ScraperApp(App):
    title = "Video Scraper"

    def build(self):
        self.opened = False
        layout = BoxLayout(orientation="vertical", padding=32, spacing=24)
        self.status = Label(text="Starting server...", halign="center", valign="middle")
        self.status.bind(size=lambda w, s: setattr(w, "text_size", s))
        button = Button(text="Open Video Scraper", size_hint_y=None, height=140,
                        on_release=lambda *_: open_in_browser())
        layout.add_widget(self.status)
        layout.add_widget(button)
        threading.Thread(target=run_server, daemon=True).start()
        Clock.schedule_once(self.check_server, 1)
        return layout

    def check_server(self, _dt):
        try:
            socket.create_connection(("127.0.0.1", PORT), timeout=0.5).close()
        except OSError:
            Clock.schedule_once(self.check_server, 1)
            return
        self.status.text = (
            f"Running at {URL}\n\nTap the button to open it in your browser.\n"
            "Keep this app open (or in recents) while downloading."
        )
        if not self.opened:
            self.opened = True
            open_in_browser()

    def on_pause(self):
        return True  # keep the server thread alive when you switch to the browser


if __name__ == "__main__":
    ScraperApp().run()
