# sw/gemm_host - host tools for GemmSoC, built per SoC build (CSR addresses come from that build's csr.h).
#   make BUILD_DIR=build/gemm_soc            -> $(BUILD_DIR)/sw/{ddr_dma_test,gemm_runner}
#   make test                                -> unit tests of src/numeric.hpp (no build dir or card needed)
# gemm_runner works only on builds with the HLS kernel (gemm_* CSRs); on others it reports that and exits.
# The build's driver must be built first (scripts/driver.sh build), it provides liblitepcie.a.

ROOT      := $(abspath $(dir $(lastword $(MAKEFILE_LIST)))/../..)
BUILD_DIR ?= build/gemm_soc
B         := $(abspath $(if $(filter /%,$(BUILD_DIR)),$(BUILD_DIR),$(ROOT)/$(BUILD_DIR)))
DRV       := $(B)/driver
OUT       := $(B)/sw
SRC       := $(ROOT)/sw/gemm_host

CXX      ?= g++
CXXFLAGS ?= -O2 -g -Wall -Wextra -std=c++17
CPPFLAGS += -I$(SRC)/src -I$(DRV)/kernel -I$(DRV)/user/liblitepcie
LIBLITEPCIE := $(DRV)/user/liblitepcie/liblitepcie.a

LIB_SRCS := $(SRC)/src/card.cpp $(SRC)/src/ddr_dma.cpp $(SRC)/src/gemm_kernel.cpp
LIB_OBJS := $(patsubst $(SRC)/src/%.cpp,$(OUT)/obj/%.o,$(LIB_SRCS))
TOOLS    := ddr_dma_test gemm_runner

all: $(addprefix $(OUT)/,$(TOOLS))

$(LIBLITEPCIE):
	@echo "missing $@: run 'BUILD_DIR=$(BUILD_DIR) scripts/driver.sh build' first" && false

$(OUT)/obj/%.o: $(SRC)/src/%.cpp $(wildcard $(SRC)/src/*.hpp) $(DRV)/kernel/csr.h
	@mkdir -p $(dir $@)
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) -c -o $@ $<

$(OUT)/%: $(SRC)/tools/%.cpp $(LIB_OBJS) $(LIBLITEPCIE)
	@mkdir -p $(dir $@)
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) -o $@ $< $(LIB_OBJS) $(LIBLITEPCIE)

# Unit tests: no csr.h, no card. The Vitis headers (path from config.sh) add an ap_int/ap_fixed cross-check.
TEST_OUT      := $(SRC)/tests/build
VITIS_INCLUDE ?= $(shell sed -n 's|^VIVADO_SETTINGS="\(.*\)/Vivado/settings64.sh"|\1/Vitis/include|p' $(ROOT)/config.sh 2>/dev/null)
TEST_INC      := -I$(SRC)/src $(if $(wildcard $(VITIS_INCLUDE)/ap_fixed.h),-isystem $(VITIS_INCLUDE))

test: $(TEST_OUT)/test_numeric
	$(TEST_OUT)/test_numeric

$(TEST_OUT)/test_numeric: $(SRC)/tests/test_numeric.cpp $(SRC)/src/numeric.hpp
	@mkdir -p $(dir $@)
	$(CXX) $(CXXFLAGS) $(TEST_INC) -o $@ $<

clean:
	rm -rf $(OUT)

.PHONY: all clean test
