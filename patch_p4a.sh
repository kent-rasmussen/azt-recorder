#!/bin/bash
# Patch the p4a Kivy recipe to suppress host pkg-config during cross-compilation.
# Re-run this after any `buildozer android clean`.

RECIPE=".buildozer/android/platform/python-for-android/pythonforandroid/recipes/kivy/__init__.py"

if [ ! -f "$RECIPE" ]; then
    echo "Recipe not found: $RECIPE"
    echo "Run 'buildozer android debug' once first to download p4a, then re-run this script."
    exit 1
fi

if grep -q "PKG_CONFIG.*=.*false" "$RECIPE"; then
    echo "Patch already applied."
    exit 0
fi

sed -i "/def get_recipe_env(self, arch, \*\*kwargs):/,/env\['CFLAGS'\]/ {
    /env\['CFLAGS'\]/i\\
        # Prevent host pkg-config from leaking x86_64 system headers\\
        # into the cross-compilation (e.g. /usr/include/x86_64-linux-gnu)\\
        env['PKG_CONFIG'] = 'false'\\
        env['PKG_CONFIG_PATH'] = ''\\
        env['PKG_CONFIG_LIBDIR'] = ''
}" "$RECIPE"

if grep -q "PKG_CONFIG.*=.*false" "$RECIPE"; then
    echo "Patch applied successfully."
else
    echo "Patch failed — please apply manually."
    exit 1
fi
