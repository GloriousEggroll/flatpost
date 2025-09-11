PYTHON_SITE_PACKAGES := $(shell python3 -c "import site; print(site.getsitepackages()[0].replace('lib64', 'lib'))")
TARGET_DIR := $(DESTDIR)$(PYTHON_SITE_PACKAGES)/flatpost
BIN_DIR := $(DESTDIR)/usr/bin
DESKTOP_DIR := $(DESTDIR)/usr/share/applications
MIME_DIR := $(DESTDIR)/usr/share/mime/packages
DATA_DIR := $(DESTDIR)/usr/share/flatpost
ICON_DIR := $(DESTDIR)/usr/share/icons/hicolor
LICENSE_DIR := $(DESTDIR)/usr/share/licenses/flatpost
VERSION := $(shell cat VERSION.txt)

.PHONY: all update-version install clean

all: update-version install

update-version:
	sed -i 's/^Version .*/Version $(VERSION)/' src/flatpost.py
install:
	@echo "Installing Python files to $(TARGET_DIR)"
	mkdir -p $(TARGET_DIR)
	install -m 644 src/fp_turbo.py $(TARGET_DIR)/fp_turbo.py

	@echo "Main executable file to $(BIN_DIR)"
	mkdir -p $(BIN_DIR)
	install -m 755 src/flatpost.py $(BIN_DIR)/flatpost

	@echo "Installing desktop file to $(DESKTOP_DIR)"
	mkdir -p $(DESKTOP_DIR)
	install -m 644 data/usr/share/applications/com.flatpost.flatpostapp.desktop $(DESKTOP_DIR)/com.flatpost.flatpostapp.desktop

	@echo "Installing MIME file to $(MIME_DIR)"
	mkdir -p $(MIME_DIR)
	install -m 644 data/usr/share/mime/packages/flatpost.xml $(MIME_DIR)/flatpost.xml

	@echo "Installing data files to $(DATA_DIR)"
	mkdir -p $(DATA_DIR)
	install -m 644 data/usr/share/flatpost/collections_data.json $(DATA_DIR)/collections_data.json

	@echo "Installing icon file to $(ICON_DIR)"
	mkdir -p $(ICON_DIR)/{1024x1024,64x64}/apps
	install -m 644 data/usr/share/icons/hicolor/64x64/apps/com.flatpost.flatpostapp.svg $(ICON_DIR)/64x64/apps/com.flatpost.flatpostapp.svg

	@echo "Installing license file to $(LICENSE_DIR)"
	mkdir -p $(LICENSE_DIR)
	install -m 644 data/usr/share/licenses/flatpost/LICENSE $(LICENSE_DIR)/LICENSE

clean:
	@echo "Cleaning up installed files"
	rm -rf $(TARGET_DIR)
	rm -f $(BIN_DIR)/flatpost
	rm -f $(DESKTOP_DIR)/com.flatpost.flatpostapp.desktop
	rm -f $(DATA_DIR)/collections_data.json
	rm -f $(ICON_DIR)/64x64/apps/com.flatpost.flatpostapp.svg
	rm -f $(LICENSE_DIR)/LICENSE
