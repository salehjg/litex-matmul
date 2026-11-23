# sw/gemm_host - host tools for GemmSoC, built per SoC build (CSR addresses come from that build's csr.h).
#   make BUILD_DIR=build/gemm_soc            -> $(BUILD_DIR)/sw/{ddr_dma_test,gemm_runner}
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

clean:
	rm -rf $(OUT)

.PHONY: all clean
