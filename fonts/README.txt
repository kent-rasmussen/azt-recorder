Place Charis SIL font files here:

  CharisSIL-Regular.ttf     (required)
  CharisSIL-Bold.ttf        (optional, falls back to Regular)
  CharisSIL-Italic.ttf      (optional, falls back to Regular)
  CharisSIL-BoldItalic.ttf  (optional, falls back to Bold)

Download from: https://software.sil.org/charis/
  → CharisSIL-6.200.zip → extract the four .ttf files here

On Debian/Ubuntu you can also install system-wide:
  sudo apt install fonts-sil-charis
  (the app will find them automatically in /usr/share/fonts)

On Android: place the .ttf files in the fonts/ directory alongside main.py
before building with Buildozer — they will be bundled into the APK.
