#!/usr/bin/env bash
# run_cifar_seeds.sh - seeds 1 and 2 of float32 and DenseShift 2/3/4-bit (seed 0: run_cifar.sh), to see the spread.
set -u
cd "$(dirname "$0")"
for seed in 1 2; do
    for b in 0 2 3 4; do
        echo "=== $(date +%T) train_cifar.py --bits $b --seed $seed"
        /usr/bin/python3 train_cifar.py --bits $b --epochs 200 --seed $seed 2>&1 | grep -E "^\{|Error|error"
    done
done
echo "=== $(date +%T) done"
