#!/usr/bin/env bash
# run_cifar.sh - the CIFAR-10 ResNet-20 runs of docs/notes3 Part A2, one after another (GPU; ~1.5 h in total).
# Logs and results in build/ml/cifar/<run>/; summary table: ml/denseshift/summarize.py.
set -u
cd "$(dirname "$0")"
EPOCHS=${EPOCHS:-200}
runs=("--bits 0" "--bits 2" "--bits 3" "--bits 4" "--bits 3 --no-rescale" "--bits 3 --init kaiming")
for r in "${runs[@]}"; do
    echo "=== $(date +%T) train_cifar.py $r --epochs $EPOCHS"
    /usr/bin/python3 train_cifar.py $r --epochs "$EPOCHS" 2>&1 | grep -E "^\{|ep +(1|50|100|150|200) |Error|error" 
done
echo "=== $(date +%T) done"
