[app]
title = A-Z+T Recorder
package.name = azt_recorder
package.domain = org.atoznback
source.dir = .
source.include_exts = py,png,jpg,kv,atlas,lift,ttf,gz,mo,po
# azt_images/ is 320 MB — too large to bundle in the APK (causes OOM on device).
# Images are accessed from external storage via lift.py's bundled_images_dir logic.
source.exclude_dirs = azt_images,env,.venv,venv,__pycache__
#version = 1.3.1
version.regex = __version__ = ['"](.*)['"]
version.filename = %(source.dir)s/main.py

requirements = python3,kivy==2.3.1,pillow,sounddevice,soundfile,numpy,certifi,filetype,typing_extensions

orientation = portrait
fullscreen = 0

android.permissions = INTERNET,RECORD_AUDIO,READ_EXTERNAL_STORAGE,WRITE_EXTERNAL_STORAGE,MANAGE_EXTERNAL_STORAGE,CAMERA,org.atoznback.AZT_COLLAB_ACCESS

# AZT collab ContentProvider lives in the standalone server APK
# (org.atoznback.aztcollab). The recorder is a pure peer: it
# <uses-permission>s the suite signature permission (declared above
# in android.permissions) and <queries> the server APK so
# PackageManager.queryContentProviders can see it on Android 11+.
# Do NOT declare <permission> or <provider> here — the server APK
# owns both. The Java provider class also ships in the server APK,
# so no android.add_src is needed.
#
# `manifest_extras.xml` is a symlink to
# ../azt-collab/android/manifest_extras_peer.xml — the canonical
# peer manifest extras shared by all suite peer apps. See
# azt-collab/CLAUDE.md sister-app integration section.
#
# Note: key is `extra_manifest_xml` (buildozer silently ignores
# `manifest_extra_xml`). Value must be a file path; inline XML is not
# accepted (buildozer does open(value).read()).
android.extra_manifest_xml = %(source.dir)s/manifest_extras.xml
#android.api = 33
android.minapi = 26
android.archs = arm64-v8a, armeabi-v7a
#This avoids creating an aab, but also turns off signing (you need to self-sign)
android.release_artifact = apk
android.signing.keystore = /home/kentr/bin/azt-suite.keystore                                                     
android.signing.key_alias = azt                                              

p4a.branch = master
p4a.hook = /home/kentr/bin/raspy/buildozer_tweaks/p4a_hook.py
p4a.local_recipes = /home/kentr/bin/raspy/buildozer_tweaks/recipes
android.api = 36
#p4a.develop:
android.ndk = 29 
#p4a.master:
#android.ndk = 27
#NDK r27's cmake flags don't work with cmake 3.31 — IN_LIST requires cmake policy CMP0057. NDK r28 fixed this (but broke something else):
#android.ndk = 28
p4a.sign = True
#android.release_armeabi_v7a = True

# Allow reading files from external storage (shared LIFT files)
android.allow_backup = False

# Legacy icon (pre-API 26 fallback)
icon.filename = %(source.dir)s/icons/icon_dark.png
# Adaptive icon layers (API 26+)
icon.adaptive_foreground.filename = %(source.dir)s/icons/icon_dark.png
icon.adaptive_background.filename = %(source.dir)s/icons/icon_bg.png

presplash.filename = %(source.dir)s/presplash.png
android.presplash_color = #1a1612

[buildozer]
log_level = 2
warn_on_root = 1
build_dir = /home/kentr/bin/AZT/.buildozer
