#!/bin/sh
# The Flatpak's command: `flatpak run io.github.priestc.Takeloom` opens the
# UI; with arguments, it's the takeloom CLI.
if [ $# -eq 0 ]; then
    set -- ui
fi
exec /app/python/bin/takeloom "$@"
