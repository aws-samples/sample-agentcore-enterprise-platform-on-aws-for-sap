#!/usr/bin/env bash
#
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Render a diagram HTML to a looping GIF + a full-resolution still PNG.
#
#   ./render.sh reference.html [outdir]
#
# The window size is measured, not configured: the page is probed once in an
# oversized window, the ink bounding box is read back, and the real render uses
# the window that gives equal margins on all four sides. That only works because
# the page has a fixed px body width and so never reflows.
#
# Env knobs:  FRAMES=30  SCALE=3  FPS=12  GIF_DIV=2  STILL_T=0.42  CHROME=<path>
set -euo pipefail

HTML=${1:?usage: render.sh <file.html> [outdir]}
[[ -f $HTML ]] || { echo "no such file: $HTML" >&2; exit 1; }
HTML=$(cd "$(dirname "$HTML")" && pwd)/$(basename "$HTML")
OUT=${2:-$(dirname "$HTML")}; mkdir -p "$OUT"
NAME=$(basename "$HTML" .html)

FRAMES=${FRAMES:-30}     # one sampled position each; more = smoother, bigger
SCALE=${SCALE:-3}        # supersample factor — 3 is where text stops softening
FPS=${FPS:-12}
GIF_DIV=${GIF_DIV:-2}    # raster ÷ this = GIF width. 2 is the sharpest ratio.
STILL_T=${STILL_T:-0.42}
PROBE=${PROBE:-2400}     # probe window, must exceed the content in both axes

# ── chrome-headless-shell: whatever this machine happens to have ─────────────
if [[ -z ${CHROME:-} ]]; then
  CHROME=$(ls -d "$HOME"/Library/Caches/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell 2>/dev/null | tail -1 || true)
fi
[[ -z ${CHROME:-} ]] && CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
[[ -x $CHROME ]] || { echo "no chrome found — set CHROME=<path to chrome-headless-shell>" >&2; exit 1; }
command -v ffmpeg >/dev/null || { echo "ffmpeg not on PATH" >&2; exit 1; }

shot () {  # shot <dsf> <w> <h> <outfile> <query>
  "$CHROME" --headless --disable-gpu --hide-scrollbars \
    --force-device-scale-factor="$1" --window-size="$2,$3" \
    --screenshot="$4" --virtual-time-budget=2200 "file://$HTML$5" >/dev/null 2>&1
}

TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT

# ── 1. measure the ink box, so the margins are even and nothing is clipped ───
shot 1 "$PROBE" "$PROBE" "$TMP/probe.png" ""
read -r WIN_W WIN_H < <(
  ffmpeg -v error -i "$TMP/probe.png" -f rawvideo -pix_fmt gray - \
  | python3 -c "
import sys
P = $PROBE
d = sys.stdin.buffer.read()
rows = [y for y in range(P) if min(d[y*P:(y+1)*P]) < 245]
if not rows: sys.exit('page rendered blank')
top, bot = rows[0], rows[-1]
cols = [x for x in range(P) if min(d[y*P+x] for y in range(top, bot+1)) < 245]
left, right = cols[0], cols[-1]
if bot >= P-1 or right >= P-1:
    sys.exit(f'content fills the {P}px probe — raise PROBE')
# equal bottom-to-top and right-to-left, by construction
print(right + 1 + left, bot + 1 + top)
")
echo "measured window ${WIN_W}x${WIN_H} → raster $((WIN_W*SCALE))x$((WIN_H*SCALE))"

# ── 2. one frame per sampled loop position ───────────────────────────────────
for i in $(seq 0 $((FRAMES-1))); do
  t=$(python3 -c "print(f'{$i/$FRAMES:.4f}')")
  shot "$SCALE" "$WIN_W" "$WIN_H" "$(printf '%s/f%03d.png' "$TMP" "$i")" "?t=$t"
done
shot "$SCALE" "$WIN_W" "$WIN_H" "$OUT/$NAME.png" "?t=$STILL_T"

# ── 3. encode. stats_mode=full because the static diagram is most of the
#       pixels, so the 256 colours should serve its text, not the comets.
#       dither=none because flat UI fills band horribly under dithering.
GIF_W=$(( (WIN_W * SCALE / GIF_DIV) / 2 * 2 ))
ffmpeg -v error -y -framerate "$FPS" -i "$TMP/f%03d.png" -filter_complex \
  "scale=$GIF_W:-1:flags=lanczos,split[a][b];\
[a]palettegen=max_colors=256:stats_mode=full[p];\
[b][p]paletteuse=dither=none:diff_mode=rectangle" \
  -loop 0 "$OUT/$NAME.gif"

du -h "$OUT/$NAME.gif" "$OUT/$NAME.png"
echo "GIFs do not animate in macOS Preview — open in a browser to check."
