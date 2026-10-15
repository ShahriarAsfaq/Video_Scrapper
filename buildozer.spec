[app]
title = Video Scraper
package.name = videoscraper
package.domain = org.videoscraper

source.dir = .
source.include_exts = py,pem
source.exclude_patterns = desktop.py
source.exclude_dirs = .github, venv, __pycache__, dist, build, bin, .buildozer

version = 0.1

# Pure-Python packages (flask, requests, yt-dlp, ...) are NOT listed here: the CI workflow bundles
# them into ./vendor, which sidesteps python-for-android's pip step.
requirements = python3,kivy,pyjnius,android,openssl

orientation = portrait
fullscreen = 0

android.permissions = INTERNET
android.api = 33
android.minapi = 24
# add armeabi-v7a (comma separated) for very old 32-bit phones
android.archs = arm64-v8a
android.accept_sdk_license = True
android.allow_backup = False

[buildozer]
log_level = 2
warn_on_root = 1
