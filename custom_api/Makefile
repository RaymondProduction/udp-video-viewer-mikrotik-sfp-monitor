# Makefile для збірки custom_api
#
# На хості (кросс-компіляція для armv7l):
#   make
#   або з явним тулчейном:
#   make CROSS=arm-linux-gnueabihf-
#
# Статичне зв'язування (якщо musl/uclibc):
#   make STATIC=1
#
# На самій камері (якщо є gcc):
#   make HOST=1

TARGET  = custom_api
SRC     = custom_api.c

# --- тулчейн -------------------------------------------------------
ifdef HOST
CC      = gcc
else
CROSS  ?= arm-linux-gnueabihf-
CC      = $(CROSS)gcc
endif

# --- флаги ----------------------------------------------------------
CFLAGS  = -O2 -Wall -Wextra -std=c99 -D_POSIX_C_SOURCE=200809L

ifdef STATIC
LDFLAGS = -static
else
LDFLAGS =
endif

# --- цілі -----------------------------------------------------------
all: $(TARGET)

$(TARGET): $(SRC)
	$(CC) $(CFLAGS) -o $@ $< $(LDFLAGS)
	@echo "Built: $@  ($(shell file $@ | cut -d, -f1 | cut -d: -f2))"

strip: $(TARGET)
	$(CROSS)strip --strip-all $(TARGET)

clean:
	rm -f $(TARGET)

# --- зручна ціль: зібрати і скопіювати на камеру ------------------
# Використання: make deploy CAM=root@192.168.1.10
deploy: $(TARGET) strip
	scp $(TARGET) $(CAM):/usr/bin/$(TARGET)
	scp S51custom_api $(CAM):/etc/init.d/S51custom_api
	ssh $(CAM) 'chmod +x /etc/init.d/S51custom_api && /etc/init.d/S51custom_api start'

.PHONY: all strip clean deploy
