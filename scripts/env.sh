# env.sh - sourced by the other scripts: Python venv, Vivado, RISC-V GCC, paths and helpers.
# Tool locations come from config.sh (written by setup.litex.du-pcb.py; edit it if a tool moves).
if [ -n "${ZSH_VERSION:-}" ]; then _env_self="$(eval 'echo "${(%):-%x}"')"; else _env_self="${BASH_SOURCE[0]}"; fi
ROOT="$(cd "$(dirname "$_env_self")/.." && pwd)"
[ -f "$ROOT/config.sh" ] && . "$ROOT/config.sh"
. "$ROOT/.venv/bin/activate"
if ! command -v vivado > /dev/null 2>&1 && [ -f "${VIVADO_SETTINGS:-}" ]; then
    _flags=$-; set +u                     # Xilinx settings scripts use unset variables.
    . "$VIVADO_SETTINGS"
    case $_flags in *u*) set -u ;; esac
fi
if ! command -v riscv64-unknown-elf-gcc > /dev/null 2>&1 && [ -d "${RISCV_BIN:-}" ]; then
    export PATH="$RISCV_BIN:$PATH"        # Any riscv64-unknown-elf-gcc builds the LiteX BIOS.
fi
TARGET="$ROOT/boards/dupcb/target.py"
BUILD="$ROOT/build/dupcb"
# PCI address of the card (empty if not enumerated). LitePCIe uses Xilinx vendor ID 10ee.
pcie_bdf() { lspci -D -d 10ee: | head -n1 | cut -d' ' -f1; }
# Path of the card's BAR0 file, after making sure Linux has the device enabled (needs sudo).
pcie_bar() {
    local bdf dev
    bdf="$(pcie_bdf)"
    [[ -n "$bdf" ]] || { echo "Card not on the PCIe bus (try scripts/pcie_rescan.sh)." >&2; return 1; }
    dev="/sys/bus/pci/devices/$bdf"
    sudo -v   # The LiteX tools run "sudo chmod" on the BAR file.
    # Linux leaves a driverless device disabled; only root may enable it.
    [[ "$(cat "$dev/enable")" != 0 ]] || echo 1 | sudo tee "$dev/enable" > /dev/null
    echo "$dev/resource0"
}
# Start litex_server on BAR0 in the background (stopped when the script exits).
# litex_term and litex_cli then talk to it on localhost:1234.
start_server() {
    local bar pid
    bar="$(pcie_bar)" || exit 1
    mkdir -p "$BUILD"
    litex_server --pcie --pcie-bar "$bar" > "$BUILD/litex_server.log" 2>&1 &
    pid=$!
    trap "kill $pid 2> /dev/null" EXIT
    # Wait until it listens on port 1234; if it dies, show why.
    for _ in $(seq 50); do
        kill -0 $pid 2> /dev/null || { echo "litex_server failed:"; cat "$BUILD/litex_server.log"; exit 1; }
        ss -ltn "sport = :1234" | grep -q LISTEN && return
        sleep 0.1
    done
    echo "litex_server did not start, see $BUILD/litex_server.log"; exit 1
}
