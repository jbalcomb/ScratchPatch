# PawNTFS — portable core, tools and tests.
#   make            build library and tools
#   make check      unit tests + image tests (image tests need root, FUSE and ntfs-3g)
#   make SAN=1 ...  build with AddressSanitizer + UBSan
#   make fuzz       mutate images and parse them under the sanitizers

CC      ?= cc
BUILD   ?= build
CFLAGS  ?= -O2 -g
CFLAGS  += -std=c11 -Wall -Wextra -Wpedantic -Wshadow -Wstrict-prototypes -Werror \
           -fno-strict-aliasing -Iinclude
ifeq ($(SAN),1)
BUILD   := $(BUILD)-san
CFLAGS  += -fsanitize=address,undefined -fno-sanitize-recover=all -fno-omit-frame-pointer
LDFLAGS += -fsanitize=address,undefined
endif

CORE_SRC  := $(wildcard src/core/*.c)
CORE_OBJ  := $(CORE_SRC:%.c=$(BUILD)/%.o)
TOOL_OBJ  := $(BUILD)/src/posix/bdev_file.o $(BUILD)/tools/util.o
LIB       := $(BUILD)/libpawntfs.a

all: $(LIB) $(BUILD)/ntfsdump

$(BUILD)/%.o: %.c $(wildcard include/pawntfs/*.h src/core/*.h src/posix/*.h tools/*.h)
	@mkdir -p $(dir $@)
	$(CC) $(CFLAGS) -c $< -o $@

$(LIB): $(CORE_OBJ)
	$(AR) rcs $@ $^

$(BUILD)/ntfsdump: $(BUILD)/tools/ntfsdump.o $(TOOL_OBJ) $(LIB)
	$(CC) $(LDFLAGS) $^ -o $@

$(BUILD)/test_unit: $(BUILD)/tests/test_unit.o $(LIB)
	$(CC) $(LDFLAGS) $^ -o $@

$(BUILD)/fuzz_image: $(BUILD)/tests/fuzz_image.o $(LIB)
	$(CC) $(LDFLAGS) $^ -o $@

check: $(BUILD)/test_unit $(BUILD)/ntfsdump
	$(BUILD)/test_unit
	tests/image_tests.sh $(BUILD)

fuzz:
	$(MAKE) SAN=1 $(BUILD)-san/fuzz_image $(BUILD)-san/ntfsdump
	tests/image_tests.sh $(BUILD)-san --images-only
	$(BUILD)-san/fuzz_image $(BUILD)-san/images/*.img

clean:
	rm -rf build build-san

.PHONY: all check fuzz clean
