[app]
title = Record My Wordlist
package.name = liftrecorder
package.domain = org.liftrecorder
source.dir = .
source.include_exts = py,png,jpg,kv,atlas,lift,ttf,gz
# azt_images/ is 320 MB — too large to bundle in the APK (causes OOM on device).
# Images are accessed from external storage via lift.py's bundled_images_dir logic.
source.exclude_dirs = azt_images,env,.venv,venv,__pycache__
#version = 1.3.1
version.regex = __version__ = ['"](.*)['"]
version.filename = %(source.dir)s/main.py

requirements = python3,kivy==2.3.1,pillow,sounddevice,soundfile,numpy,dulwich,certifi

orientation = portrait
fullscreen = 0

android.permissions = INTERNET,RECORD_AUDIO,READ_EXTERNAL_STORAGE,WRITE_EXTERNAL_STORAGE,MANAGE_EXTERNAL_STORAGE
#android.api = 33
android.minapi = 26
android.archs = arm64-v8a, armeabi-v7a
p4a.branch = develop
android.api = 36
android.ndk = 29

# Allow reading files from external storage (shared LIFT files)
android.allow_backup = False

# Legacy icon (pre-API 26 fallback)
icon.filename = %(source.dir)s/icons/icon.png
# Adaptive icon layers (API 26+)
icon.adaptive_foreground.filename = %(source.dir)s/icons/icon_dark.png
icon.adaptive_background.filename = %(source.dir)s/icons/icon_bg.png

presplash.filename = %(source.dir)s/icons/icon.png
android.presplash_color = #1a1612

[buildozer]
log_level = 2
warn_on_root = 1
