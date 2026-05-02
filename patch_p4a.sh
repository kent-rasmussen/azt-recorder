#!/bin/bash
# Historical: this script used to sed-patch the upstream p4a kivy
# recipe to clear host PKG_CONFIG_* in get_recipe_env. The same fix
# now lives in the local kivy recipe override at
# /home/kentr/bin/raspy/buildozer_tweaks/recipes/kivy/__init__.py
# (PatchedKivyRecipe.get_recipe_env), which is more robust against
# upstream refactors.
#
# Kept as a no-op so existing build steps / CI scripts that invoke
# `bash patch_p4a.sh` keep working. Safe to delete in a future
# cleanup along with any references in build.sh / setup_from_nuke.sh.
echo "patch_p4a.sh is now a no-op; the local kivy recipe override"
echo "handles PKG_CONFIG clearing. See"
echo "  /home/kentr/bin/raspy/buildozer_tweaks/recipes/kivy/__init__.py"
exit 0
