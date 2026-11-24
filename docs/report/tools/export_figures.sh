#!/usr/bin/env bash
#
# export_figures.sh - render report plots/figures to PNG for the README (docs/images/<name>.png).
#   docs/report/tools/export_figures.sh                 the default set (the images the README shows)
#   docs/report/tools/export_figures.sh plots/clock ... any plots/<x> or figures/<x>
# Each one is typeset alone at the report's text width with preamble.tex and the committed data/*.csv, cropped,
# and rasterised on white (readable in dark mode). Needs pdflatex, pdfcrop and pdftoppm (poppler).
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # docs/report
OUT="$HERE/../images"
TMP="$HERE/build/export"   # not build/figures: latexmk -outdir=build would pick the wrappers up as figures/*.tex
DPI=${DPI:-180}
names=("$@")
[[ ${#names[@]} -gt 0 ]] || names=(figures/system plots/progress plots/roofline plots/clock plots/e2e)

for tool in pdflatex pdfcrop pdftoppm; do
    command -v "$tool" > /dev/null || { echo "missing $tool"; exit 1; }
done
mkdir -p "$OUT" "$TMP"
cd "$HERE"
for n in "${names[@]}"; do
    n=${n%.tex}
    [[ -f "$n.tex" ]] || { echo "no such file: docs/report/$n.tex"; exit 1; }
    base=$(basename "$n")
    case "$n" in
        figures/*) body="\\resizebox{\\linewidth}{!}{\\input{$n}}" ;;
        *)         body="\\input{$n}" ;;
    esac
    cat > "$TMP/$base.tex" <<TEX
\\documentclass{article}
\\input{preamble}
\\pagestyle{empty}
\\begin{document}
\\noindent\\begin{minipage}{\\textwidth}\\centering
$body
\\end{minipage}
\\end{document}
TEX
    : > "$TMP/$base.out"   # hyperref's bookmark file from an earlier run
    if ! pdflatex -interaction=nonstopmode -halt-on-error -output-directory="$TMP" "$TMP/$base.tex" \
            > "$TMP/$base.stdout" 2>&1; then
        echo "FAILED: $n (log: $TMP/$base.log)"; grep -m 5 '^!' "$TMP/$base.log" || true; exit 1
    fi
    pdfcrop --margins 10 "$TMP/$base.pdf" "$TMP/$base-crop.pdf" > /dev/null
    pdftoppm -png -r "$DPI" -singlefile "$TMP/$base-crop.pdf" "$OUT/$base"
    echo "docs/images/$base.png"
done
