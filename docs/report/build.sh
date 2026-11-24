#!/usr/bin/env bash
#
# build.sh - build the report PDF cleanly.
#   docs/report/build.sh            refresh data/ from results/ (if present), then build report.pdf
#   docs/report/build.sh --no-data  build from the committed data/*.csv only
#   docs/report/build.sh --clean    remove all LaTeX output (build/ and report.pdf)
#
# Output: docs/report/report.pdf (git-ignored). All intermediate files go to docs/report/build/ (git-ignored).
# Needs pdflatex, latexmk and bibtex (TeX Live: texlive-basic, -latex, -latexrecommended, -latexextra,
# -fontsrecommended, -binextra, -pictures for TikZ/pgfplots, -science for siunitx).
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
OUT="$HERE/build"

data=1
case "${1:-}" in
    --clean)   rm -rf "$OUT" "$HERE/report.pdf"; echo "cleaned"; exit 0 ;;
    --no-data) data=0 ;;
    "")        ;;
    *)         sed -n '3,9p' "$0"; exit 2 ;;
esac

for tool in pdflatex latexmk bibtex; do
    command -v "$tool" > /dev/null || { echo "missing $tool (install TeX Live, see the header of $0)"; exit 1; }
done

# Plot data: regenerate from the (git-ignored) DSE results when they are here; otherwise use the committed CSVs.
if [[ $data == 1 ]]; then
    py="$ROOT/.venv/bin/python"; [[ -x "$py" ]] || py=python3
    if compgen -G "$ROOT/results/*/runs.jsonl" > /dev/null; then
        echo "[report] data: $py tools/extract_data.py"
        "$py" "$HERE/tools/extract_data.py"
    else
        echo "[report] data: no results/ here, using the committed data/*.csv"
    fi
fi

mkdir -p "$OUT"
cd "$HERE"
echo "[report] latexmk -> build/report.pdf"
# -halt-on-error: stop at the first error; the full log stays in build/report.log.
if ! latexmk -pdf -outdir="$OUT" -interaction=nonstopmode -halt-on-error -file-line-error \
        -silent report.tex > "$OUT/latexmk.out" 2>&1; then
    echo "[report] FAILED. First errors (full log: $OUT/report.log):"
    grep -E -m 15 '^[^ ]+:[0-9]+:|^! ' "$OUT/report.log" 2>/dev/null || tail -30 "$OUT/latexmk.out"
    exit 1
fi
cp "$OUT/report.pdf" "$HERE/report.pdf"
warn=$(grep -c -E 'LaTeX Warning|Package .* Warning' "$OUT/report.log" || true)
pages=$(grep -oE 'Output written on .*\(([0-9]+) pages' "$OUT/report.log" | grep -oE '[0-9]+ pages' || true)
echo "[report] done: docs/report/report.pdf (${pages:-? pages}, $warn warnings in build/report.log)"
