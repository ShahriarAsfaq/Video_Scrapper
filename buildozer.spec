[app]
title = Video Scraper
package.name = videoscraper
package.domain = org.videoscraper

source.dir = .
source.include_exts = py
source.exclude_patterns = desktop.py
source.exclude_dirs = .github, venv, __pycache__, dist, build, bin, .buildozer

version = 0.1

# python-for-android installs with --no-deps, so every dependency is listed explicitly
requirements = python3,kivy,pyjnius,android,openssl,flask,werkzeug,jinja2,markupsafe,itsdangerous,click,blinker,requests,urllib3,idna,charset-normalizer,certifi,beautifulsoup4,soupsieve,typing_extensions,yt-dlp

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
