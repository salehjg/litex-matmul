#!/usr/bin/env bash
# run_cifar_more.sh - after run_cifar_seeds.sh: ablations with seeds 1 and 2, and 4-bit with 400 epochs (is 4-bit
# below 3-bit because 8 binary variables per weight train slower?).
set -u
cd "$(dirname "$0")"
until grep -q "=== .* done" ../../build/ml/run_cifar_seeds.log 2>/dev/null; do sleep 30; done
for seed in 1 2; do
    for r in "--bits 3 --no-rescale" "--bits 3 --init kaiming"; do
        echo "=== $(date +%T) train_cifar.py $r --seed $seed"
        /usr/bin/python3 train_cifar.py $r --epochs 200 --seed $seed 2>&1 | grep -E "^\{|Error|error"
    done
done
for b in 3 4; do
    echo "=== $(date +%T) train_cifar.py --bits $b --epochs 400"
    /usr/bin/python3 train_cifar.py --bits $b --epochs 400 2>&1 | grep -E "^\{|Error|error"
done
echo "=== $(date +%T) done"
