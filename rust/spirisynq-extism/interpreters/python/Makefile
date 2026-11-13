# Builds the `python` loader's interpreter, MicroPython for WASI, with MicroPython's own make
# rules. ../build.sh runs it with MPY_SRC (the MicroPython sources), BUILD (a scratch directory),
# OUT (where the module goes), CC, and SYNQ_CFLAGS and SYNQ_LDFLAGS.

include $(MPY_SRC)/py/mkenv.mk

QSTR_DEFS = qstrdefsport.h
MICROPY_ROM_TEXT_COMPRESSION = 0

include $(TOP)/py/py.mk
include $(TOP)/extmod/extmod.mk

INC += -I. -I$(TOP) -I$(BUILD)
CFLAGS += -std=c99 -Os -DNDEBUG $(INC) $(SYNQ_CFLAGS)

SRC_C = runner.c
SRC_QSTR += $(SRC_C)
OBJ = $(PY_O) $(addprefix $(BUILD)/, $(SRC_C:.c=.o))

all: $(OUT)/python.raw.wasm

$(OUT)/python.raw.wasm: $(OBJ)
	$(Q)$(CC) $(CFLAGS) -o $@ $^ $(SYNQ_LDFLAGS)

include $(TOP)/py/mkrules.mk
