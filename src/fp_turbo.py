#!/usr/bin/env python3

### Documentation largely taken from:
###
### 1. https://lazka.github.io/pgi-docs/Flatpak-1.0
### 2. https://flathub.org/api/v2/docs#/
###
### Classes AppStreamPackage and AppStreamSearcher extended from original by Tim Tim Lauridsen at:
###
### https://github.com/timlau/yumex-ng/blob/main/yumex/backend/flatpak/search.py

# Original GPL v3 Code Copyright:
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
# Copyright (C) 2024 Tim Lauridsen
#
# Modifications copyright notice
# Copyright (C) 2025 Thomas Crider
#
# Original code has been completely removed except
# AppStreamPackage and AppStreamSearcher classes
# which have been modified and extended.


import gi
gi.require_version("AppStream", "1.0")
gi.require_version("Flatpak", "1.0")

from gi.repository import Flatpak, GLib, Gio, AppStream
from pathlib import Path
import logging
from enum import IntEnum
import argparse
import requests
from urllib.parse import quote_plus, urlparse
import tempfile
import shutil
import os
import sys
import json
import time
import dbus
import platform

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class Match(IntEnum):
    NAME = 1
    ID = 2
    SUMMARY = 3
    NONE = 4

class AppStreamComponentKind(IntEnum):
    """AppStream Component Kind enumeration."""

    UNKNOWN = 0
    """Type invalid or not known."""

    GENERIC = 1
    """A generic (= without specialized type) component."""

    DESKTOP_APP = 2
    """An application with a .desktop-file."""

    CONSOLE_APP = 3
    """A console application."""

    WEB_APP = 4
    """A web application."""

    SERVICE = 5
    """A system service launched by the init system."""

    ADDON = 6
    """An extension of existing software, which does not run standalone."""

    RUNTIME = 7
    """An application runtime platform."""

    FONT = 8
    """A font."""

    CODEC = 9
    """A multimedia codec."""

    INPUT_METHOD = 10
    """An input-method provider."""

    OPERATING_SYSTEM = 11
    """A computer operating system."""

    FIRMWARE = 12
    """Firmware."""

    DRIVER = 13
    """A driver."""

    LOCALIZATION = 14
    """Software localization (usually l10n resources)."""

    REPOSITORY = 15
    """A remote software or data source."""

    ICON_THEME = 16
    """An icon theme following the XDG specification."""

APPSTREAM_TTL_SECONDS = 7 * 24 * 60 * 60  # 1 week

def _appstream_file_is_stale(remote: Flatpak.Remote) -> bool:
    appstream_file = Path(remote.get_appstream_dir().get_path()) / "appstream.xml.gz"
    try:
        mod_time = appstream_file.stat().st_mtime
    except FileNotFoundError:
        return True
    return (time.time() - mod_time) > APPSTREAM_TTL_SECONDS

class AppStreamPackage:
    def __init__(self, comp: AppStream.Component, remote: Flatpak.Remote) -> None:
        self.component: AppStream.Component = comp
        self.remote: Flatpak.Remote = remote
        self.repo_name: str = remote.get_name()
        bundle: AppStream.Bundle = comp.get_bundle(AppStream.BundleKind.FLATPAK)
        self.flatpak_bundle = bundle.get_id() if bundle else ""
        self.flatpak_app_id = self._extract_app_id(self.flatpak_bundle) or self._strip_desktop_suffix(comp.get_id())
        self.match = Match.NONE

        # Get icon and description
        self.icon_url = self._get_icon_url()
        self.icon_path_128 = self._get_icon_cache_path("128x128")
        self.icon_path_64 = self._get_icon_cache_path("64x64")
        self.icon_filename = self._get_icon_filename()
        self.description = self.component.get_description()
        self.screenshots = self.component.get_screenshots_all()

        # Get URLs from the component
        self.urls = self._get_urls()

        self.developer = self.component.get_developer().get_name()
        self.categories = self._get_categories()

    @staticmethod
    def _strip_desktop_suffix(s: str) -> str:
        return s[:-8] if s and s.endswith(".desktop") else s

    @staticmethod
    def _extract_app_id(bundle_id: str) -> str:
        """
        bundle_id might be:
          - 'com.vscodium.codium'
          - 'app/com.vscodium.codium/x86_64/stable'
        """
        if not bundle_id:
            return ""
        b = bundle_id.strip()
        if b.startswith("app/") or b.startswith("runtime/"):
            parts = b.split("/")
            if len(parts) >= 2:
                return parts[1]
        return b

    @property
    def id(self) -> str:
        return self.component.get_id()

    @property
    def name(self) -> str:
        return self.component.get_name()

    @property
    def summary(self) -> str:
        return self.component.get_summary()

    @property
    def version(self) -> str|None:
        releases = self.component.get_releases_plain()
        if releases:
            release = releases.index_safe(0)
            if release:
                version = release.get_version()
                return version
        return None

    @property
    def kind(self) -> str:
        k = self.component.get_kind()

        # GI may return an int-like enum or a plain int; normalize to int
        try:
            k_val = int(k)
        except Exception:
            # Fallback: try to parse a symbolic name from the string repr
            s = str(k)
            token = s.rsplit(".", 1)[-1].strip()
            return token if token else "UNKNOWN"

        # Map using your IntEnum (values match AppStream's)
        try:
            return AppStreamComponentKind(k_val).name.replace("_", " ").title()
        except ValueError:
            return "UNKNOWN"

    def _get_icon_url(self) -> str:
        """Get the remote icon URL from the component"""
        icons = self.component.get_icons()

        # Find the first REMOTE icon
        remote_icon = next((icon for icon in icons if icon.get_kind() == AppStream.IconKind.REMOTE), None)
        return remote_icon.get_url() if remote_icon else ""

    def _get_icon_filename(self) -> str:
        """Get the cached icon filename from the component"""
        icons = self.component.get_icons()

        # Find the first CACHED icon
        cached_icon = next((icon for icon in icons if icon.get_kind() == AppStream.IconKind.CACHED), None)
        return cached_icon.get_filename() if cached_icon else ""

    def _get_icon_cache_path(self, size: str) -> str:

        # Appstream icon cache path for the flatpak repo queried
        icon_cache_path = Path(self.remote.get_appstream_dir().get_path() + "/icons/flatpak/" + size + "/")
        return str(icon_cache_path)

    def _get_urls(self) -> dict:
        """Get URLs from the component"""
        urls = {
            'donation': self._get_url('donation'),
            'homepage': self._get_url('homepage'),
            'bugtracker': self._get_url('bugtracker')
        }
        return urls

    def _get_url(self, url_kind: str) -> str:
        """Helper method to get a specific URL type"""
        # Convert string to AppStream.UrlKind enum
        url_kind_enum = getattr(AppStream.UrlKind, url_kind.upper())
        url = self.component.get_url(url_kind_enum)
        if url:
            return url
        return ""

    def _get_categories(self) -> list:
        categories_fetch = self.component.get_categories()
        categories = []
        for category in categories_fetch:
            categories.append(category.lower())
        return categories

    def search(self, keyword: str) -> Match:
        """Search for keyword in package details"""
        if keyword in self.name.lower():
            return Match.NAME
        elif keyword in self.id.lower():
            return Match.ID
        elif keyword in self.summary.lower():
            return Match.SUMMARY
        else:
            return Match.NONE

    def __str__(self) -> str:
        return f"{self.name} - {self.summary} ({self.flatpak_bundle})"

    def _pretty_label(self, name: str) -> str:
        # Title-case, but keep common acronyms uppercased
        parts = name.replace("_", " ").split()
        acronyms = {"id", "ui", "api", "gpu", "cpu", "url"}
        return " ".join(p.upper() if p.lower() in acronyms else p.title() for p in parts)

    def get_details(self) -> dict:
        """Get all package details including icon and description"""
        return {
            "name": self.name,
            "id": getattr(self, "installed_app_id", None) or self.flatpak_app_id,
            "flatpak_app_id": self.flatpak_app_id,          # keep both for debugging
            "component_id": self.id,
            "kind": self.kind,
            "summary": self.summary,
            "description": self.description,
            "version": self.version,
            "icon_url": self.icon_url,
            "icon_path_128": self.icon_path_128,
            "icon_path_64": self.icon_path_64,
            "icon_filename": self.icon_filename,
            "urls": self.urls,
            "developer": self.developer,
            #"architectures": self.architectures,
            "categories": self.categories,
            "bundle_id": self.flatpak_bundle,
            "match_type": self._pretty_label(self.match.name),
            "match_type_raw": self.match.name,
            "repo": self.repo_name,
            "screenshots": self.screenshots,
            "component": self.component,
        }

class AppstreamSearcher:
    """Flatpak AppStream Package seacher"""

    def __init__(self, refresh=False) -> None:
        self.remotes: dict[str, list[AppStreamPackage]] = {}
        self.refresh_progress = 0
        self.refresh = refresh

        # Define category groups and their titles
        self.category_groups = {
            'system': {
                'installed': 'Installed',
                'updates': 'Updates',
                'repositories': 'Repositories'
            },
            'collections': {
                'trending': 'Trending',
                'popular': 'Popular',
                'recently-added': 'New',
                'recently-updated': 'Updated'
            },
            'categories': {
                'office': 'Productivity',
                'graphics': 'Graphics & Photography',
                'audiovideo': 'Audio & Video',
                'education': 'Education',
                'network': 'Networking',
                'game': 'Games',
                'development': 'Developer Tools',
                'science': 'Science',
                'system': 'System',
                'utility': 'Utilities'
            }
        }

        self.subcategory_groups = {
            'audiovideo': {
                'audiovideoediting': 'Audio & Video Editing',
                'discburning': 'Disc Burning',
                'midi': 'Midi',
                'mixer': 'Mixer',
                'player': 'Player',
                'recorder': 'Recorder',
                'sequencer': 'Sequencer',
                'tuner': 'Tuner',
                'tv': 'TV'
            },
            'development': {
                'building': 'Building',
                'database': 'Database',
                'debugger': 'Debugger',
                'guidesigner': 'GUI Designer',
                'ide': 'IDE',
                'profiling': 'Profiling',
                'revisioncontrol': 'Revision Control',
                'translation': 'Translation',
                'webdevelopment': 'Web Development'
            },
            'game': {
                'actiongame': 'Action Games',
                'adventuregame': 'Adventure Games',
                'arcadegame': 'Arcade Games',
                'blocksgame': 'Blocks Games',
                'boardgame': 'Board Games',
                'cardgame': 'Card Games',
                'emulator': 'Emulators',
                'kidsgame': 'Kids\' Games',
                'logicgame': 'Logic Games',
                'roleplaying': 'Role Playing',
                'shooter': 'Shooter',
                'simulation': 'Simulation',
                'sportsgame': 'Sports Games',
                'strategygame': 'Strategy Games'
            },
            'graphics': {
                '2dgraphics': '2D Graphics',
                '3dgraphics': '3D Graphics',
                'ocr': 'OCR',
                'photography': 'Photography',
                'publishing': 'Publishing',
                'rastergraphics': 'Raster Graphics',
                'scanning': 'Scanning',
                'vectorgraphics': 'Vector Graphics',
                'viewer': 'Viewer'
            },
            'network': {
                'chat': 'Chat',
                'email': 'Email',
                'feed': 'Feed',
                'filetransfer': 'File Transfer',
                'hamradio': 'Ham Radio',
                'instantmessaging': 'Instant Messaging',
                'ircclient': 'IRC Client',
                'monitor': 'Monitor',
                'news': 'News',
                'p2p': 'P2P',
                'remoteaccess': 'Remote Access',
                'telephony': 'Telephony',
                'videoconference': 'Video Conference',
                'webbrowser': 'Web Browser',
                'webdevelopment': 'Web Development'
            },
            'office': {
                'calendar': 'Calendar',
                'chart': 'Chart',
                'contactmanagement': 'Contact Management',
                'database': 'Database',
                'dictionary': 'Dictionary',
                'email': 'Email',
                'finance': 'Finance',
                'presentation': 'Presentation',
                'projectmanagement': 'Project Management',
                'publishing': 'Publishing',
                'spreadsheet': 'Spreadsheet',
                'viewer': 'Viewer',
                'wordprocessor': 'Word Processor'
            },
            'system': {
                'emulator': 'Emulators',
                'filemanager': 'File Manager',
                'filesystem': 'Filesystem',
                'filetools': 'File Tools',
                'monitor': 'Monitor',
                'security': 'Security',
                'terminalemulator': 'Terminal Emulator'
            },
            'utility': {
                'accessibility': 'Accessibility',
                'archiving': 'Archiving',
                'calculator': 'Calculator',
                'clock': 'Clock',
                'compression': 'Compression',
                'filetools': 'File Tools',
                'telephonytools': 'Telephony Tools',
                'texteditor': 'Text Editor',
                'texttools': 'Text Tools'
            }
        }

    def add_installation(self, inst: Flatpak.Installation):
        """Add enabled flatpak repositories from Flatpak.Installation"""
        remotes = inst.list_remotes()
        for remote in remotes:
            if not remote.get_disabled():
                self.add_remote(remote, inst)

    def add_remote(self, remote: Flatpak.Remote, inst: Flatpak.Installation):
        """Add packages for a given Flatpak.Remote"""
        remote_name = remote.get_name()
        if remote_name not in self.remotes:
            self.remotes[remote_name] = self._load_appstream_metadata(remote, inst)

    def _load_appstream_metadata(self, remote: Flatpak.Remote, inst: Flatpak.Installation) -> list[AppStreamPackage]:
        packages = []
        metadata = AppStream.Metadata.new()
        metadata.set_format_style(AppStream.FormatStyle.CATALOG)

        appstream_file = Path(remote.get_appstream_dir().get_path()) / "appstream.xml.gz"

        # Only refresh if explicitly allowed AND stale/missing
        if self.refresh and _appstream_file_is_stale(remote):
            try:
                if remote.get_name() in ("flathub", "flathub-beta"):
                    remote.set_gpg_verify(True)
                    inst.modify_remote(remote, None)
                inst.update_appstream_full_sync(remote.get_name(), None, None, True)
            except GLib.Error as e:
                logger.error(f"Failed to update AppStream metadata: {str(e)}")

        # If file still missing, try once (optional, but keeps old behavior)
        if not appstream_file.exists():
            try:
                if remote.get_name() in ("flathub", "flathub-beta"):
                    remote.set_gpg_verify(True)
                    inst.modify_remote(remote, None)
                inst.update_appstream_full_sync(remote.get_name(), None, None, True)
            except GLib.Error as e:
                logger.error(f"Failed to update AppStream metadata: {str(e)}")

        if appstream_file.exists():
            metadata.parse_file(Gio.File.new_for_path(appstream_file.as_posix()), AppStream.FormatKind.XML)
            components: AppStream.ComponentBox = metadata.get_components()
            for i in range(components.get_size()):
                component = components.index_safe(i)
                packages.append(AppStreamPackage(component, remote))
            return packages

        logger.debug(f"AppStream file not found: {appstream_file}")
        return []


    def search_flatpak_repo(self, keyword: str, repo_name: str) -> list[AppStreamPackage]:
        search_results = []
        packages = self.remotes[repo_name]
        found = None

        def norm(s: str) -> str:
            s = (s or "").strip()
            if s.endswith(".desktop"):
                s = s[:-8]
            if s.startswith("app/") or s.startswith("runtime/"):
                parts = s.split("/")
                if len(parts) >= 2:
                    s = parts[1]
            return s.lower()

        key = norm(keyword)

        for package in packages:
            # 1) exact Flatpak app-id match (best)
            if key and norm(getattr(package, "flatpak_app_id", "")) == key:
                found = package
                break

            # 2) exact component id match (desktop-id etc)
            if key and norm(package.id) == key:
                found = package
                break

            # 3) exact name match
            if key and package.name and package.name.lower() == key:
                found = package
                break

            # 4) fallback fuzzy
            if key and key in str(package).lower():
                found = package
                break

        if found:
            search_results.append(found)
        return search_results



    def search_flatpak(self, keyword: str, repo_name=None) -> list[AppStreamPackage]:
        """Search packages matching a keyword"""
        search_results = []
        keyword = keyword

        if not repo_name:
            for remote_name in self.remotes.keys():
                search_results.extend(self.search_flatpak_repo(keyword, remote_name))
        else:
            if repo_name in self.remotes.keys():
                search_results.extend(self.search_flatpak_repo(keyword, repo_name))
        return search_results


    def get_all_apps(self, repo_name=None) -> list[AppStreamPackage]:
        """Get all available apps from specified or all repositories"""
        all_packages = []
        if repo_name:
            if repo_name in self.remotes:
                all_packages = self.remotes[repo_name]
        else:
            for remote_name in self.remotes.keys():
                all_packages.extend(self.remotes[remote_name])
        return all_packages

    def get_categories_summary(self, repo_name=None) -> dict:
        """Get a summary of all apps grouped by category"""
        apps = self.get_all_apps(repo_name)
        categories = {}

        for app in apps:
            for category in app.categories:
                # Normalize category names to match our groups
                normalized_category = category.lower()

                # Map category to its group title
                for group_name, categories_dict in self.category_groups.items():
                    if normalized_category in categories_dict:
                        display_category = categories_dict[normalized_category]
                        break
                else:
                    display_category = normalized_category.title()

                if display_category not in categories:
                    categories[display_category] = []
                categories[display_category].append(app)

        return categories

    def get_subcategories_summary(self, repo_name=None) -> list[tuple[str, str, list[AppStreamPackage]]]:
        """Get a summary of all apps grouped by category and subcategory."""
        apps = self.get_all_apps(repo_name)
        subcategories = []

        # Process each category and its subcategories
        for category, subcategories_dict in self.subcategory_groups.items():
            for subcategory, title in subcategories_dict.items():
                apps_in_subcategory = []
                for app in apps:
                    if category in app.categories and subcategory in app.categories:
                        apps_in_subcategory.append(app)
                if apps_in_subcategory:
                    subcategories.append((category, subcategory, apps_in_subcategory))

        return subcategories

    def get_installed_apps(self, system=False) -> list[tuple[str, str, str]]:
        """Get a list of all installed Flatpak applications with their repository source"""
        installed_refs = []

        installation = get_installation(system)

        def process_installed_refs(inst: Flatpak.Installation, system=False):
            for ref in inst.list_installed_refs():
                app_id = ref.get_name()
                remote_name = ref.get_origin()
                if system is False:
                    installed_refs.append((app_id, remote_name, "user"))
                else:
                    installed_refs.append((app_id, remote_name, "system"))

        # Process both system-wide and user installations
        process_installed_refs(installation, system)

        # Remove duplicates while maintaining order
        seen = set()
        unique_installed = [(ref, repo, repo_type) for ref, repo, repo_type in installed_refs
                        if not (ref in seen or seen.add(ref))]

        return unique_installed

    def check_updates(self, system=False) -> list[tuple[str, str, str]]:
        """Check for available updates for installed Flatpak applications"""
        updates = []

        installation = get_installation(system)

        def check_updates_for_install(inst: Flatpak.Installation, system=False):
            for ref in inst.list_installed_refs_for_update(None):
                app_id = ref.get_name()
                # Get remote name from the installation
                remote_name = ref.get_origin()
                if system is False:
                    updates.append((app_id, remote_name, "user"))
                else:
                    updates.append((app_id, remote_name, "system"))

        # Process both system-wide and user installations
        check_updates_for_install(installation, system)

        return updates

    def fetch_flathub_category_apps(self, category):
        """Fetch applications from Flathub API for the specified category."""
        try:
            # URL encode the category to handle special characters
            encoded_category = quote_plus(category)

            # Determine the base URL based on category type
            if category in self.category_groups['collections']:
                url = f"https://flathub.org/api/v2/collection/{encoded_category}"
            else:
                url = f"https://flathub.org/api/v2/collection/category/{encoded_category}"

            response = requests.get(url, timeout=10)

            if response.status_code == 200:
                data = response.json()

                # If this is a collections category, save it to our collections database
                if category in self.category_groups['collections']:
                    if not hasattr(self, 'collections_db'):
                        self.collections_db = []
                    self.collections_db.append({
                        'category': category,
                        'data': data
                    })

                return data
            else:
                print(f"Failed to fetch apps: Status code {response.status_code}")
                return None
        except requests.RequestException as e:
            print(f"Error fetching apps: {str(e)}")
            return None

    def save_collections_data(self, filename='collections_data.json'):
        """Save all collected collections data to a JSON file."""
        app_data_dir = Path.home() / ".local" / "share" / "flatpost"
        app_data_dir.mkdir(parents=True, exist_ok=True)
        json_path = app_data_dir / filename
        if not hasattr(self, 'collections_db') or not self.collections_db:
            return

        try:
            with open(json_path, 'w', encoding='utf-8') as f:
                json.dump(self.collections_db, f, indent=2, ensure_ascii=False)
        except IOError as e:
            print(f"Error saving collections data: {str(e)}")

    def update_collection_results(self, new_collection_results):
        """Update search results by replacing existing items and adding new ones."""
        # Create a set of existing app_ids for efficient lookup
        existing_app_ids = {app.id for app in self.collection_results}

        # Create a list to store the updated results
        updated_results = []

        # First add all existing results
        updated_results.extend(self.collection_results)

        # Add new results, replacing any existing ones
        for new_result in new_collection_results:
            app_id = new_result.id
            if app_id in existing_app_ids:
                # Replace existing result
                for i, existing in enumerate(updated_results):
                    if existing.id == app_id:
                        updated_results[i] = new_result
                        break
            else:
                # Add new result
                updated_results.append(new_result)

        self.collection_results = updated_results

    def fetch_flathub_subcategory_apps(self, category: str, subcategory: str) -> dict|None:
        """Fetch applications from Flathub API for the specified category and subcategory."""
        try:
            # URL encode the category and subcategory to handle special characters
            encoded_category = quote_plus(category)
            encoded_subcategory = quote_plus(subcategory)

            # Construct the API URL for subcategories
            url = f"https://flathub.org/api/v2/collection/category/{encoded_category}/subcategories?subcategory={encoded_subcategory}"

            response = requests.get(url, timeout=10)

            if response.status_code == 200:
                data = response.json()
                return data
            else:
                print(f"Failed to fetch apps: Status code {response.status_code}")
                return None
        except requests.RequestException as e:
            print(f"Error fetching apps: {str(e)}")
            return None

    def refresh_local(self, system=False):

        self._initialize_metadata()

        total_categories = sum(len(categories) for categories in self.category_groups.values())
        current_category = 0
        # Search for each app in local repositories
        searcher = get_reposearcher(system)
        search_result = []
        for group_name, categories in self.category_groups.items():
            # Process categories one at a time to keep GUI responsive
            for category, title in categories.items():
                self._process_system_category(searcher, category, system)
                # Update progress bar
                self.refresh_progress = (current_category / total_categories) * 100
        # make sure to reset these to empty before refreshing.
        return self.installed_results, self.updates_results


    def retrieve_metadata(self, system=False):
        """Retrieve and refresh metadata for Flatpak repositories."""
        self._initialize_metadata()

        if not check_internet():
            return self._handle_offline_mode(system)

        # Refresh AppStream + reload remotes
        searcher = get_reposearcher(system, True)

        # IMPORTANT: ensure self.search_flatpak uses refreshed metadata
        self.remotes = searcher.remotes

        self.all_apps = searcher.get_all_apps()
        return self._process_categories(searcher, system)


    def _initialize_metadata(self):
        """Initialize empty lists for metadata storage."""
        self.category_results = []
        self.collection_results = []
        self.installed_results = []
        self.updates_results = []
        self.all_apps = []

    def _handle_offline_mode(self, system=False):
        """Handle metadata retrieval when offline."""
        searcher = get_reposearcher(system)
        total_categories = sum(len(categories) for categories in self.category_groups.values())
        current_category = 0

        search_result = []
        for group_name, categories in self.category_groups.items():
            # Process categories one at a time to keep GUI responsive
            for category, title in categories.items():
                self._process_system_category(searcher, category)

        # Define paths
        app_data_dir = Path.home() / ".local" / "share" / "flatpost"
        system_data_dir = Path("/usr/share/flatpost")

        # Ensure local directory exists
        app_data_dir.mkdir(parents=True, exist_ok=True)

        # Define file paths
        json_path = app_data_dir / "collections_data.json"

        # Helper function to copy file if it doesn't exist locally
        def copy_if_missing(source_path, dest_path):
            try:
                shutil.copy(str(source_path), str(dest_path))
                logger.info(f"Copied {dest_path.name} to user directory")
            except IOError as e:
                logger.error(f"Failed to copy {dest_path.name}: {str(e)}")
                return False
            return True

        # Try to copy collections_data.json if needed
        if not json_path.exists() and not copy_if_missing(
            system_data_dir / "collections_data.json",
            json_path
        ):
            logger.error("Could not load or copy collections_data.json")
            return None, [], [], [], []

        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                collections_data = json.load(f)
                processed_data = self._process_offline_data(collections_data)

                return processed_data

        except (IOError, json.JSONDecodeError) as e:
            logger.error(f"Error loading offline data: {str(e)}")
            return None, [], [], [], []


    def _process_offline_data(self, collections_data):
        """Process cached collections data when offline."""
        for collection in collections_data:
            category = collection['category']
            if category in self.category_groups['collections']:
                apps = [app['app_id'] for app in collection['data'].get('hits', [])]
                for app_id in apps:
                    search_result = self.search_flatpak(app_id, 'flathub')
                    self.collection_results.extend(search_result)
        return self._get_current_results()

    def _process_categories(self, searcher, system=False):
        """Process categories and retrieve metadata."""
        total_categories = sum(len(categories) for categories in self.category_groups.values())
        current_category = 0

        for group_name, categories in self.category_groups.items():
            for category, title in categories.items():
                if category not in self.category_groups['system']:
                    self._process_category(searcher, category, current_category, total_categories, system)
                else:
                    self._process_system_category(searcher, category, system)
                current_category += 1
        self.save_collections_data()

        return self._get_current_results()

    def _process_category(self, searcher, category, current_category, total_categories, system=False):
        """Process a single category and retrieve its metadata."""

        if self._should_refresh():
            self._refresh_category_data(searcher, category)

        app_data_dir = Path.home() / ".local" / "share" / "flatpost"
        app_data_dir.mkdir(parents=True, exist_ok=True)
        json_path = app_data_dir / "collections_data.json"
        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                collections_data = json.load(f)
                self._update_from_collections(collections_data, category)
        except (IOError, json.JSONDecodeError) as e:
            pass

        self.refresh_progress = (current_category / total_categories) * 100

    def _update_from_collections(self, collections_data, category):
        """Update results from cached collections data."""
        for collection in collections_data:
            if collection['category'] == category:
                apps = [app['app_id'] for app in collection['data'].get('hits', [])]
                for app_id in apps:
                    search_result = self.search_flatpak(app_id, 'flathub')
                    self.collection_results.extend(search_result)

    def _should_refresh(self):
        """Check if category data needs refresh."""
        app_data_dir = Path.home() / ".local" / "share" / "flatpost"
        app_data_dir.mkdir(parents=True, exist_ok=True)
        json_path = app_data_dir / "collections_data.json"
        try:
            mod_time = os.path.getmtime(json_path)
            return (time.time() - mod_time) > 168 * 3600
        except OSError:
            return True

    def _refresh_category_data(self, searcher, category):
        """Refresh category data from Flathub API."""
        try:
            api_data = self.fetch_flathub_category_apps(category)
            if api_data:
                apps = api_data['hits']
                for app in apps:
                    app_id = app['app_id']
                    search_result = searcher.search_flatpak(app_id, 'flathub')
                    if category in self.category_groups['collections']:
                        self.update_collection_results(search_result)
                    else:
                        self.category_results.extend(search_result)
        except requests.RequestException as e:
            logger.error(f"Error refreshing category {category}: {str(e)}")

    def _process_system_category(self, searcher, category, system=False):
        if category == "installed":
            installed_apps = get_installation(system).list_installed_refs()
            for app in installed_apps:
                ref_str = app.format_ref()
                origin = app.get_origin()

                search_result = searcher.search_flatpak(ref_str, origin)
                if not search_result:
                    search_result = searcher.search_flatpak(app.get_name(), origin)

                for pkg in search_result:
                    # Pin to the real installed app-id
                    pkg.installed_app_id = app.get_name()
                    pkg.installed_ref = ref_str
                    pkg.installed_origin = origin

                self.installed_results.extend(search_result)

        elif category == "updates" and check_internet():
            updates = get_installation(system).list_installed_refs_for_update(None)
            for app in updates:
                ref_str = app.format_ref()
                search_result = searcher.search_flatpak(ref_str, app.get_origin())
                if not search_result:
                    search_result = searcher.search_flatpak(app.get_name(), app.get_origin())
                self.updates_results.extend(search_result)

    def _get_current_results(self):
        """Return current metadata results."""
        return (
            self.category_results,
            self.collection_results,
            self.installed_results,
            self.updates_results,
            self.all_apps
        )


def _run_transaction_with_progress(transaction: Flatpak.Transaction, progress_cb=None):
    """
    progress_cb(overall_percent:int, status:str)  # called from the *calling thread*
    NOTE: caller should marshal to GTK thread via GLib.idle_add if needed.
    """
    state = {
        "total_ops": 1,
        "done_ops": 0,
        "current_progress": 0,
        "current_status": "",
        "current_progress_obj": None,
        "changed_handler_id": None,
    }

    def emit():
        if not progress_cb:
            return
        total = max(1, state["total_ops"])
        overall = int(((state["done_ops"] + (state["current_progress"] / 100.0)) / total) * 100)
        overall = max(0, min(100, overall))
        progress_cb(overall, state["current_status"])

    def on_ready(trans):
        # ready signal expects bool return (allow transaction to proceed)
        try:
            ops = trans.get_operations()
            state["total_ops"] = max(1, len(ops) if ops else 1)
        except Exception:
            state["total_ops"] = 1
        emit()
        return True

    def on_operation_done(trans, operation, commit, error):
        # operation-done fires after each op completes
        state["done_ops"] += 1
        state["current_progress"] = 0
        emit()

    def on_new_operation(trans, operation, progress: Flatpak.TransactionProgress):
        # Disconnect prior progress handler (if any)
        if state["current_progress_obj"] is not None and state["changed_handler_id"] is not None:
            try:
                state["current_progress_obj"].disconnect(state["changed_handler_id"])
            except Exception:
                pass

        state["current_progress_obj"] = progress
        state["current_progress"] = 0

        # Optional: make updates more frequent/smooth
        try:
            progress.set_update_frequency(200)  # ms
        except Exception:
            pass

        def on_progress_changed(p):
            try:
                state["current_progress"] = int(p.get_progress())  # 0..100
            except Exception:
                state["current_progress"] = 0
            try:
                state["current_status"] = p.get_status() or ""
            except Exception:
                state["current_status"] = ""
            emit()

        state["changed_handler_id"] = progress.connect("changed", on_progress_changed)

        # Emit immediately so UI updates as soon as we start an op
        on_progress_changed(progress)

    transaction.connect("ready", on_ready)
    transaction.connect("new-operation", on_new_operation)
    transaction.connect("operation-done", on_operation_done)

    transaction.run()


def install_flatpak(app, repo_name=None, system=False, progress_cb=None) -> tuple[bool, str]:
    if not repo_name:
        repo_name = "flathub"

    installation = get_installation(system)
    cancellable = Gio.Cancellable()
    transaction = Flatpak.Transaction.new_for_installation(installation, cancellable)

    def emit_progress(pct: int, status: str = ""):
        if progress_cb:
            try:
                progress_cb(int(pct), status or "")
            except Exception:
                pass

    def on_new_operation(tx, operation, progress):
        if not progress:
            return

        def on_changed(prog):
            try:
                pct = prog.get_progress()
            except Exception:
                pct = 0
            try:
                status = prog.get_status() or ""
            except Exception:
                status = ""
            emit_progress(pct, status)

        progress.connect("changed", on_changed)
        on_changed(progress)

    transaction.connect("new-operation", on_new_operation)

    # Determine preferred arch (Flatpak arch names are usually x86_64/aarch64)
    # platform.machine() returns x86_64/aarch64 on most systems.
    wanted_arch = platform.machine()

    wanted_name = getattr(app, "id", None) or getattr(app, "get_id", lambda: None)()  # be defensive
    if not wanted_name:
        return False, "App has no id."

    # If your AppStreamPackage has a preferred branch/ref in details, use it:
    details = getattr(app, "get_details", lambda: {})() or {}
    preferred_branch = details.get("branch")  # may be None
    # Many appstream entries don’t include branch; fall back to stable-first preference.
    branch_preference = [preferred_branch] if preferred_branch else []
    branch_preference += ["stable", "master", "beta", "test", "edge", "insiders"]

    try:
        remote_refs = installation.list_remote_refs_sync(repo_name)
    except GLib.Error as e:
        return False, f"Failed to list refs for {repo_name}: {e}"

    candidates = []
    for r in remote_refs:
        try:
            # Only apps
            if r.get_kind() != Flatpak.RefKind.APP:
                continue
            if r.get_name() != wanted_name:
                continue
            if r.get_arch() != wanted_arch:
                continue
            candidates.append(r)
        except Exception:
            continue

    if not candidates:
        return False, f"No available APP ref named {wanted_name} (arch {wanted_arch}) in {repo_name}."

    # Pick best branch
    def branch_rank(ref):
        b = ref.get_branch()
        try:
            return branch_preference.index(b)
        except ValueError:
            return 999

    best = sorted(candidates, key=branch_rank)[0]

    emit_progress(0, "Preparing…")

    try:
        transaction.add_install(repo_name, best.format_ref(), None)
        transaction.run(cancellable)
    except GLib.Error as e:
        return False, f"Installation failed: {e}"

    emit_progress(100, "Finishing…")
    return True, f"Successfully installed {best.format_ref()}"



def install_flatpakref(ref_file, system=False, progress_cb=None):
    installation = get_installation(system)

    # Read the flatpakref file
    try:
        with open(ref_file, 'rb') as f:
            repo_data = f.read()
    except IOError as e:
        return False, f"Failed to read flatpakref file: {str(e)}"

    repo_bytes = GLib.Bytes.new(repo_data)

    cancellable = Gio.Cancellable()
    transaction = Flatpak.Transaction.new_for_installation(installation, cancellable)  # :contentReference[oaicite:8]{index=8}

    def emit_progress(pct: int, status: str = ""):
        if progress_cb:
            try:
                progress_cb(int(pct), status or "")
            except Exception:
                pass

    def on_new_operation(tx, operation, progress):
        if not progress:
            return

        def on_changed(prog):
            try:
                pct = prog.get_progress()
            except Exception:
                pct = 0
            try:
                status = prog.get_status() or ""
            except Exception:
                status = ""
            emit_progress(pct, status)

        progress.connect("changed", on_changed)
        on_changed(progress)

    transaction.connect("new-operation", on_new_operation)  # :contentReference[oaicite:9]{index=9}

    transaction.add_install_flatpakref(repo_bytes)

    emit_progress(0, "Preparing…")
    try:
        transaction.run(cancellable)  # :contentReference[oaicite:10]{index=10}
    except GLib.Error as e:
        return False, f"Installation failed: {e}"

    emit_progress(100, "Finishing…")
    return True, f"Successfully installed {ref_file}"



def remove_flatpak(app: AppStreamPackage, system=False) -> tuple[bool, str]:
    """
    Remove a Flatpak package using transactions.

    Args:
        app (AppStreamPackage): The package to install.
        system (Optional[bool]): Whether to operate on user or system installation

    Returns:
        Tuple[bool, str]: (success, message)
    """

    # Get the appropriate installation based on user parameter
    installation = get_installation(system)
    installed = installation.list_installed_refs(None)
    # Create a new transaction for removal
    transaction = Flatpak.Transaction.new_for_installation(installation)
    match_found = None
    target = getattr(app, "flatpak_app_id", None) or app.id
    for installed_ref in installed:
        if installed_ref.get_name() == target:
            match_found = True
            # Add the install operation
            transaction.add_uninstall(installed_ref.format_ref())

    if not match_found:
        return False, f"No installed package named {app.id} found."
    try:
        transaction.run()
    except GLib.Error as e:
        return False, f"Failed to remove {app.id}: {e}"
    return True, f"Successfully removed {app.id}"

def update_flatpak(app: AppStreamPackage, system=False) -> tuple[bool, str]:
    """
    Remove a Flatpak package using transactions.

    Args:
        app (AppStreamPackage): The package to install.
        system (Optional[bool]): Whether to operate on user or system installation

    Returns:
        Tuple[bool, str]: (success, message)
    """

    # Get the appropriate installation based on user parameter
    installation = get_installation(system)
    updates = installation.list_installed_refs_for_update(None)
    # Create a new transaction for removal
    transaction = Flatpak.Transaction.new_for_installation(installation)
    match_found = None
    target = getattr(app, "flatpak_app_id", None) or app.id
    for update in updates:
        if update.get_name() == target:
            match_found = True
            # Add the install operation
            transaction.add_update(update.format_ref())


    if not match_found:
        return False, f"No updateable package named {app.id} found."
    # Run the transaction
    try:
        transaction.run()
    except GLib.Error as e:
        return False, f"Failed to update {app.id}: {e}"
    return True, f"Successfully updated {app.id}"

def update_all_flatpaks(apps: list[AppStreamPackage], system=False) -> tuple[bool, str]:
    """
    Update multiple Flatpak packages using transactions.

    Args:
        apps (Union[List[AppStreamPackage], AppStreamPackage]): One or more packages to update
        system (Optional[bool]): Whether to operate on user or system installation

    Returns:
        Tuple[bool, List[str]]: (success, list of status messages)
    """

    installation = get_installation(system)
    updates = installation.list_installed_refs_for_update(None)
    # Create a new transaction for removal
    transaction = Flatpak.Transaction.new_for_installation(installation)
    for update in updates:
        transaction.add_update(update.format_ref())

    try:
        transaction.run()
        return True, "Successfully updated all packages"
    except GLib.Error as e:
        return False, f"Failed to update all packages: {str(e)}"

def get_installation(system=False):
    if system is False:
        installation = Flatpak.Installation.new_user()
    else:
        installation = Flatpak.Installation.new_system()
    return installation

def get_reposearcher(system=False, refresh=False):
    installation = get_installation(system)
    searcher = AppstreamSearcher(refresh)
    searcher.add_installation(installation)
    return searcher

def check_internet():
    try:
        requests.head('https://flathub.org', timeout=3)
        return True
    except requests.RequestException:
        return False

def repotoggle(repo, toggle=True, system=False):
    """
    Enable or disable a Flatpak repository

    Args:
        repo (str): Name of the repository to toggle
        enable (toggle): True to enable, False to disable

    Returns:
        tuple: (success, error_message)
    """

    if not repo:
        return False, "Repository name cannot be empty"

    installation = get_installation(system)

    try:
        remote = installation.get_remote_by_name(repo)
        if not remote:
            return False, f"Repository '{repo}' not found."

        remote.set_disabled(not toggle)

        # Modify the remote's disabled status
        success = installation.modify_remote(
            remote,
            None
        )
        if success:
            if toggle:
                message = f"Successfully enabled {repo}."
            else:
                message = f"Successfully disabled {repo}."
            return True, message

    except GLib.GError as e:
        return False, f"Failed to toggle repository: {str(e)}"

    return False, "Operation failed"

def repolist(system=False):
    installation = get_installation(system)
    repos = installation.list_remotes()
    return repos

def repodelete(repo, system=False):
    installation = get_installation(system)
    installation.remove_remote(repo)

def repoadd(repofile, system=False):
    """Add a new repository using a .flatpakrepo file"""
    # Get existing repositories
    installation = get_installation(system)
    existing_repos = installation.list_remotes()

    if not repofile.endswith('.flatpakrepo'):
        return False, "Repository file path or URL must end with .flatpakrepo extension."

    if repofile_is_url(repofile):
        try:
            local_path = download_repo(repofile)
            repofile = local_path
            print(f"\nRepository added successfully: {repofile}")
        except:
            return False, f"Repository file '{repofile}' could not be downloaded."

    if not os.path.exists(repofile):
        return False, f"Repository file '{repofile}' does not exist."

    # Get repository title from file name
    title = os.path.basename(repofile).replace('.flatpakrepo', '')

    # Check for duplicate title (case insensitive)
    existing_titles = [repo.get_name().casefold() for repo in existing_repos]

    if title.casefold() in existing_titles:
        return False, "A repository with this title already exists."

    if title == "flathub":
        title = "Flatpak Official Flathub"
    if title == "flathub-beta":
        title = "Flatpak Official Flathub (Beta)"

    # Read the repository file
    try:
        with open(repofile, 'rb') as f:
            repo_data = f.read()
    except IOError as e:
        return False, f"Failed to read repository file: {str(e)}"

    # Convert the data to GLib.Bytes
    repo_bytes = GLib.Bytes.new(repo_data)

    # Create a new remote from the repository file
    try:
        remote = Flatpak.Remote.new_from_file(title, repo_bytes)

        # Get URLs and normalize them by removing trailing slashes
        new_url = remote.get_url().rstrip('/')
        existing_urls = [repo.get_url().rstrip('/') for repo in existing_repos]

        # Check if URL already exists
        if new_url in existing_urls:
            return False, f"A repository with URL '{new_url}' already exists."
        user = "user"
        if system:
            user = "system"
        remote.set_gpg_verify(True)
        installation.add_remote(remote, True, None)
    except GLib.GError as e:
        return False, f"Failed to add repository: {str(e)}"
    return True, f"{remote.get_name()} repository successfully added for {user} installation."

def repofile_is_url(string):
    """Check if a string is a valid URL"""
    try:
        result = urlparse(string)
        return all([result.scheme, result.netloc])
    except:
        return False

def download_repo(url):
    """Download a repository file from URL to /tmp/"""
    try:
        # Create a deterministic filename based on the URL
        url_path = urlparse(url).path
        filename = os.path.basename(url_path) or 'repo'
        tmp_path = Path(tempfile.gettempdir()) / f"{filename}"

        # Download the file
        with requests.get(url, stream=True) as response:
            response.raise_for_status()

            # Write the file in chunks, overwriting if it exists
            with open(tmp_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)

        return str(tmp_path)
    except requests.RequestException as e:
        raise argparse.ArgumentTypeError(f"Failed to download repository file: {str(e)}")
    except IOError as e:
        raise argparse.ArgumentTypeError(f"Failed to save repository file: {str(e)}")

def get_metadata_path(app_id: str | None, override=False, system=False) -> str:
    metadata_path = ""
    if app_id:
        # Get the application's metadata file
        installation = get_installation(system)
        app_path = installation.get_current_installed_app(app_id).get_deploy_dir()
        if not app_path:
            print(f"Application {app_id} not found")
            return metadata_path
        metadata_path = app_path + "/metadata"
    elif override:
        if system:
            metadata_path = "/var/lib/flatpak/overrides/global"
            if not os.path.exists(metadata_path):
                os.makedirs(os.path.dirname(metadata_path), exist_ok=True)
                with open(metadata_path, 'w') as f:
                        pass
        else:
            home_dir = os.path.expanduser("~")
            metadata_path = f"{home_dir}/.local/share/flatpak/overrides/global"
            if not os.path.exists(metadata_path):
                os.makedirs(os.path.dirname(metadata_path), exist_ok=True)
                with open(metadata_path, 'w') as f:
                        pass
    if not os.path.exists(metadata_path):
        print(f"Metadata file not found for {app_id}")
        return metadata_path
    return metadata_path

def get_perm_key_file(app_id: str | None,  override=False, system=False) -> GLib.KeyFile:
    metadata_path = get_metadata_path(app_id, override, system)
    # Create a new KeyFile object
    key_file = GLib.KeyFile()

    # Read the existing metadata
    try:
        key_file.load_from_file(metadata_path, GLib.KeyFileFlags.NONE)
    except GLib.Error as e:
        print(f"Failed to read metadata file: {str(e)}")
        return None

    return key_file

def add_file_permissions(app_id: str, path: str, perm_type=None, system=False) -> tuple[bool, str]:
    """
    Add filesystem permissions to a Flatpak application.

    Args:
        app_id (str): The ID of the Flatpak application
        path (str): The path to grant access to. Can be:
            - "home" for home directory access
            - "/path/to/directory" for custom directory access
        perm_type (str): The type of permissions to remove (e.g. "filesystems", "persistent") default is "filesystems"
        system (bool): Whether to modify system-wide or user installation

    Returns:
        tuple[bool, str]: (success, message)
    """
    try:
        key_file = get_perm_key_file(app_id, False, system)
        perm_type = perm_type or "filesystems"
        # Handle special case for home directory
        if path.lower() == "host":
            filesystem_path = "host"
        elif path.lower() == "host-os":
            filesystem_path = "host-os"
        elif path.lower() == "host-etc":
            filesystem_path = "host-etc"
        elif path.lower() == "home":
            filesystem_path = "home"
        else:
            # Ensure path do not ends with a trailing slash
            filesystem_path = path.rstrip('/')

            # Validate absolute paths start with /
            if filesystem_path.startswith('/'):
                filesystem_path = '/' + filesystem_path.lstrip('/')

        if not key_file.has_group("Context"):
            key_file.set_string("Context", perm_type, "")

        context_keys = key_file.get_keys("Context")
        if perm_type not in str(context_keys):
            key_file.set_string("Context", perm_type, "")

        existing_paths = key_file.get_string("Context", perm_type)
        if existing_paths is None or existing_paths == "":
            # If no filesystems exist, set the exact path provided
            key_file.set_string("Context", perm_type, filesystem_path)
        else:
            existing_paths_list = existing_paths.split(';')
            normalized_new_path = os.path.abspath(filesystem_path.rstrip('/'))
            normalized_existing_paths = [os.path.abspath(p.rstrip('/')) for p in existing_paths_list]

            if normalized_new_path not in normalized_existing_paths:
                # Add new path with proper separator
                separator = ';' if existing_paths.endswith(';') else ';'
                key_file.set_string("Context", perm_type,
                                    existing_paths + separator + filesystem_path)

        # Write the modified metadata back
        try:
            key_file.save_to_file(get_metadata_path(app_id, False, system))
        except GLib.Error as e:
            return False, f"Failed to save metadata file: {str(e)}"

        return True, f"Successfully granted access to {path} for {app_id}"

    except GLib.Error as e:
        return False, f"Failed to modify permissions: {str(e)}"


def remove_file_permissions(app_id: str, path: str, perm_type=None, system=False) -> tuple[bool, str]:
    """
    Remove filesystem permissions from a Flatpak application.

    Args:
        app_id (str): The ID of the Flatpak application
        path (str): The path to revoke access to. Can be:
            - "home" for home directory access
            - "/path/to/directory" for custom directory access
        perm_type (str): The type of permissions to remove (e.g. "filesystems", "persistent") default is "filesystems"
        system (bool): Whether to modify system-wide or user installation

    Returns:
        tuple[bool, str]: (success, message)
    """
    try:
        key_file = get_perm_key_file(app_id, False, system)
        perm_type = perm_type or "filesystems"

        # Handle special case for home directory
        if path.lower() == "host":
            filesystem_path = "host"
        elif path.lower() == "host-os":
            filesystem_path = "host-os"
        elif path.lower() == "host-etc":
            filesystem_path = "host-etc"
        elif path.lower() == "home":
            filesystem_path = "home"
        else:
            # Ensure path do not ends with a trailing slash
            filesystem_path = path.rstrip('/')

        # Get existing filesystem paths
        existing_paths = key_file.get_string("Context", perm_type)

        if existing_paths is None:
            return True, f"No filesystem permissions to remove for {app_id}"

        # Split existing paths and normalize them for comparison
        existing_paths_list = existing_paths.split(';')
        normalized_new_path = os.path.abspath(filesystem_path.rstrip('/'))
        normalized_existing_paths = [os.path.abspath(p.rstrip('/')) for p in existing_paths_list]

        # Only remove if the path exists
        if normalized_new_path not in normalized_existing_paths:
            return True, f"No permission found for {path} in {app_id}"

        # Remove the path from the existing paths
        filtered_paths_list = [p for p in existing_paths_list
                             if os.path.abspath(p.rstrip('/')) != normalized_new_path]

        # Join remaining paths back together
        new_permissions = ";".join(filtered_paths_list)
        if new_permissions:
        # Save changes
            key_file.set_string("Context", perm_type, new_permissions)
        else:
            key_file.remove_key("Context", perm_type)

        # Write the modified metadata back
        try:
            key_file.save_to_file(get_metadata_path(app_id, False, system))
        except GLib.Error as e:
            return False, f"Failed to save metadata file: {str(e)}"

        return True, f"Successfully removed access to {path} for {app_id}"

    except GLib.Error as e:
        return False, f"Failed to modify permissions: {str(e)}"

def list_file_perms(app_id: str, system=False) -> tuple[bool, dict[str, list[str]]]|tuple[bool, dict[str, list[str]]]:
    """
    List filesystem permissions for a Flatpak application.

    Args:
        app_id (str): The ID of the Flatpak application
        system (bool): Whether to check system-wide or user installation

    Returns:
        tuple[bool, dict[str, list[str]]]: (success, permissions_dict)
            permissions_dict contains:
                - 'paths': list of filesystem paths
                - 'special_paths': list of special paths (home, host, etc.)
    """
    try:
        key_file = get_perm_key_file(app_id, False, system)

        # Initialize result dictionary
        result = {
            "paths": [],
            "special_paths": []
        }

        # Get existing filesystem paths
        existing_paths = key_file.get_string("Context", "filesystems")
        if existing_paths:
            # Split and clean the paths
            paths_list = [p.strip() for p in existing_paths.split(';')]

            # Separate special paths from regular ones
            for path in paths_list:
                if path in ["home", "host", "host-os", "host-etc"]:
                    result["special_paths"].append(path)
                else:
                    result["paths"].append(path)

        return True, result
    except GLib.Error:
        return False, {"paths": [], "special_paths": []}


def list_other_perm_toggles(app_id: str, perm_type: str, system=False) -> tuple[bool, dict[str, list[str]]]|tuple[bool, dict[str, list[str]]]:
    """
    List other permission toggles within "Context" for a Flatpak application.

    Args:
        app_id (str): The ID of the Flatpak application
        perm_type (str): The type of permissions to list (e.g. "shared", "sockets", "devices", "features", "persistent")
        system (bool): Whether to check system-wide or user installation

    Returns:
        tuple[bool, dict[str, list[str]]]: (success, permissions_dict)
            permissions_dict contains:
                - 'paths': list of filesystem paths
    """
    try:
        key_file = get_perm_key_file(app_id, False, system)

        # Initialize result dictionary
        result = {
            "paths": []
        }

        # Get existing filesystem paths
        existing_paths = key_file.get_string("Context", perm_type)
        if existing_paths:
            # Split, clean, and filter out empty paths
            paths_list = [p.strip() for p in existing_paths.split(';') if p.strip()]

            # Add filtered paths to result
            result["paths"] = paths_list

        return True, result
    except GLib.Error:
        return False, {"paths": []}

    # Get existing filesystem paths
    existing_paths = key_file.get_string("Context", perm_type)
    if existing_paths:
        # Split, clean, and filter out empty paths
        paths_list = [p.strip() for p in existing_paths.split(';') if p.strip()]

        # Add filtered paths to result
        result["paths"] = paths_list


def toggle_other_perms(app_id: str, perm_type: str, option: str, enable: bool, system=False) -> tuple[bool, str]:
    """
    Toggle a specific permission option for a Flatpak application.

    Args:
        app_id (str): The ID of the Flatpak application
        perm_type (str): The type of permissions (shared, sockets, devices, features)
        option (str): The specific permission to toggle
        enable (bool): Whether to enable or disable the permission
        system (bool): Whether to check system-wide or user installation

    Returns:
        bool: True if successful, False if operation failed
    """
    # Get the KeyFile object
    key_file = get_perm_key_file(app_id, False, system)

    if not key_file:
        return False, f"Failed to get permissions for {app_id}"

    try:
        perms_list = []
        # Get all keys in the Context section
        # Check if Context section exists
        if not key_file.has_group("Context"):
            key_file.set_string("Context", perm_type, "")

        # Now get the keys
        context_keys = key_file.get_keys("Context")

        # Check if perm_type exists in the section
        if perm_type not in str(context_keys):
            # Create the key with an empty string
            key_file.set_string("Context", perm_type, "")

        # Get the existing permissions
        existing_perms = key_file.get_string("Context", perm_type)

        if existing_perms:
            # Split into individual permissions
            perms_list = [perm.strip() for perm in existing_perms.split(';') if perm.strip()]

        # Toggle permission
        if enable:
            if option not in perms_list:
                perms_list.append(option)
        else:
            if option in perms_list:
                perms_list.remove(option)

        # Join back with semicolons
        new_perms = ";".join(perms_list)

        # Save changes
        if new_perms:
            key_file.set_string("Context", perm_type, new_perms)
        else:
            key_file.remove_key("Context", perm_type)
        key_file.save_to_file(get_metadata_path(app_id, False, system))

        return True, f"Successfully {'enabled' if enable else 'disabled'} {option} for {app_id}"

    except GLib.Error:
        return False, f"Failed to toggle {option} for {app_id}"


def list_other_perm_values(app_id: str, perm_type: str, system=False) -> tuple[bool, dict[str, list[str]]]:
    """
    List all permission values for a specified type from a Flatpak application's configuration.

    Args:
        app_id (str): The ID of the Flatpak application
        perm_type (str): The type of permissions to list (e.g. "environment", "session_bus", "system_bus")
        system (bool): Whether to check system-wide or user installation

    Returns:
        tuple[bool, dict[str, list[str]]]: (success, env_vars_dict)
            env_vars_dict contains:
                - 'paths': list of environment variables
    """
    try:
        key_file = get_perm_key_file(app_id, False, system)

        # Initialize result dictionary
        result = {
            "paths": []
        }

        match perm_type.lower():
            case "environment":
                perm_type = "Environment"
            case "session_bus":
                perm_type = "Session Bus Policy"
            case "system_bus":
                perm_type = "System Bus Policy"
            case _:
                return False, {"paths": []}

        # Check if section exists using has_group()
        if key_file.has_group(perm_type):
            # Get all keys in the section
            keys = key_file.get_keys(perm_type)

            # Convert ResultTuple to list of individual keys
            keys = list(keys[0]) if hasattr(keys, '__iter__') else []

            # Get each value and add to paths list
            for key in keys:
                value = key_file.get_string(perm_type, key)
                if value:
                    result["paths"].append(f"{key}={value}")

        return True, result
    except GLib.Error as e:
        print(f"GLib.Error: {e}")
        return False, {"paths": []}
    except Exception as e:
        print(f"Other error: {e}")
        return False, {"paths": []}

def add_permission_value(app_id: str, perm_type: str, value: str, system=False) -> tuple[bool, str]:
    """
    Add a permission value to a Flatpak application's configuration.

    Args:
        app_id (str): The ID of the Flatpak application
        perm_type (str): The type of permissions (e.g. "environment", "session_bus", "system_bus")
        value (str): The complete permission value to add (e.g. "XCURSOR_PATH=/run/host/user-share/icons:/run/host/share/icons")
        system (bool): Whether to modify system-wide or user installation

    Returns:
        tuple[bool, str]: (success, message)
    """
    try:
        key_file = get_perm_key_file(app_id, False, system)

        # Convert perm_type to the correct format
        match perm_type.lower():
            case "environment":
                perm_type = "Environment"
            case "session_bus":
                perm_type = "Session Bus Policy"
            case "system_bus":
                perm_type = "System Bus Policy"
            case _:
                return False, "Invalid permission type"

        # Split the value into key and actual value
        parts = value.split('=', 1)
        if len(parts) != 2:
            return False, "Value must be in format 'key=value'"

        key, val = parts

        if perm_type in ['Session Bus Policy', 'System Bus Policy']:
            if val not in ['talk', 'own']:
                return False, "Value must be in format 'key=value' with value as 'talk' or 'own'"

        # Set the value
        key_file.set_string(perm_type, key, val)

        # Save the changes
        key_file.save_to_file(get_metadata_path(app_id, False, system))

        return True, f"Successfully added {value} to {perm_type} section"
    except GLib.Error as e:
        return False, f"Error adding permission: {str(e)}"

def remove_permission_value(app_id: str, perm_type: str, value: str, system=False) -> tuple[bool, str]:
    """
    Remove a permission value from a Flatpak application's configuration.

    Args:
        app_id (str): The ID of the Flatpak application
        perm_type (str): The type of permissions (e.g. "environment", "session_bus", "system_bus")
        value (str): The complete permission value to remove (e.g. "XCURSOR_PATH=/run/host/user-share/icons:/run/host/share/icons")
        system (bool): Whether to modify system-wide or user installation

    Returns:
        tuple[bool, str]: (success, message)
    """
    try:
        key_file = get_perm_key_file(app_id, False, system)

        # Convert perm_type to the correct format
        match perm_type.lower():
            case "environment":
                perm_type = "Environment"
            case "session_bus":
                perm_type = "Session Bus Policy"
            case "system_bus":
                perm_type = "System Bus Policy"
            case _:
                return False, "Invalid permission type"

        # Split the value into key and actual value
        parts = value.split('=', 1)
        if len(parts) != 2:
            return False, "Value must be in format 'key=value'"

        key, val = parts
        # Check if section exists
        if not key_file.has_group(perm_type):
            return False, f"Section {perm_type} does not exist"

        # Remove the value
        key_file.remove_key(perm_type, key)

        # Save the changes
        key_file.save_to_file(get_metadata_path(app_id, False, system))

        return True, f"Successfully removed {value} from {perm_type} section"
    except GLib.Error as e:
        return False, f"Error removing permission: {str(e)}"

def global_add_file_permissions(path: str, perm_type=None, override=True, system=False) -> tuple[bool, str]:
    """
    Add filesystem permissions to all Flatpak applications globally.

    Args:
        path (str): The path to grant access to. Can be:
            - "home" for home directory access
            - "/path/to/directory" for custom directory access
        perm_type (str): The type of permissions to remove (e.g. "filesystems", "persistent") default is "filesystems"
        override (bool): Whether to use global metadata file instead of per-app.
        system (bool): Whether to modify system-wide or user installation

    Returns:
        tuple[bool, str]: (success, message)
    """

    try:
        key_file = get_perm_key_file(None, override, system)
        perm_type = perm_type or "filesystems"
        # Handle special case for home directory
        if path.lower() == "host":
            filesystem_path = "host"
        elif path.lower() == "host-os":
            filesystem_path = "host-os"
        elif path.lower() == "host-etc":
            filesystem_path = "host-etc"
        elif path.lower() == "home":
            filesystem_path = "home"
        else:
            # Ensure path do not ends with a trailing slash
            filesystem_path = path.rstrip('/')

            # Validate absolute paths start with /
            if filesystem_path.startswith('/'):
                filesystem_path = '/' + filesystem_path.lstrip('/')

        if not key_file.has_group("Context"):
            key_file.set_string("Context", perm_type, "")

        context_keys = key_file.get_keys("Context")
        if perm_type not in str(context_keys):
            key_file.set_string("Context", perm_type, "")

        existing_paths = key_file.get_string("Context", perm_type)
        if existing_paths is None or existing_paths == "":
            # If no filesystems exist, set the exact path provided
            key_file.set_string("Context", perm_type, filesystem_path)
        else:
            existing_paths_list = existing_paths.split(';')
            normalized_new_path = os.path.abspath(filesystem_path.rstrip('/'))
            normalized_existing_paths = [os.path.abspath(p.rstrip('/')) for p in existing_paths_list]

            if normalized_new_path not in normalized_existing_paths:
                # Add new path with proper separator
                separator = ';' if existing_paths.endswith(';') else ';'
                key_file.set_string("Context", perm_type,
                                    existing_paths + separator + filesystem_path)

        # Write the modified metadata back
        try:
            key_file.save_to_file(get_metadata_path(None, override, system))
        except GLib.Error as e:
            return False, f"Failed to save metadata file: {str(e)}"

        return True, f"Successfully granted access to {path} globally"

    except GLib.Error as e:
        return False, f"Failed to modify permissions: {str(e)}"


def global_remove_file_permissions(path: str, perm_type=None, override=True, system=False) -> tuple[bool, str]:
    """
    Remove filesystem permissions from all Flatpak applications globally.

    Args:
        path (str): The path to revoke access to. Can be:
            - "home" for home directory access
            - "/path/to/directory" for custom directory access
        perm_type (str): The type of permissions to remove (e.g. "filesystems", "persistent") default is "filesystems"
        override (bool): Whether to use global metadata file instead of per-app.
        system (bool): Whether to modify system-wide or user installation

    Returns:
        tuple[bool, str]: (success, message)
    """
    try:
        key_file = get_perm_key_file(None, override, system)
        perm_type = perm_type or "filesystems"
        # Handle special case for home directory
        if path.lower() == "host":
            filesystem_path = "host"
        elif path.lower() == "host-os":
            filesystem_path = "host-os"
        elif path.lower() == "host-etc":
            filesystem_path = "host-etc"
        elif path.lower() == "home":
            filesystem_path = "home"
        else:
            # Ensure path do not ends with a trailing slash
            filesystem_path = path.rstrip('/')

        # Get existing filesystem paths
        existing_paths = key_file.get_string("Context", perm_type)

        if existing_paths is None:
            return True, "No filesystem permissions to remove globally"

        # Split existing paths and normalize them for comparison
        existing_paths_list = existing_paths.split(';')
        normalized_new_path = os.path.abspath(filesystem_path.rstrip('/'))
        normalized_existing_paths = [os.path.abspath(p.rstrip('/')) for p in existing_paths_list]

        # Only remove if the path exists
        if normalized_new_path not in normalized_existing_paths:
            return True, f"No permission found for {path} globally"

        # Remove the path from the existing paths
        filtered_paths_list = [p for p in existing_paths_list
                             if os.path.abspath(p.rstrip('/')) != normalized_new_path]

        # Join remaining paths back together
        new_permissions = ";".join(filtered_paths_list)
        if new_permissions:
        # Save changes
            key_file.set_string("Context", perm_type, new_permissions)
        else:
            key_file.remove_key("Context", perm_type)

        # Write the modified metadata back
        try:
            key_file.save_to_file(get_metadata_path(None, override, system))
        except GLib.Error as e:
            return False, f"Failed to save metadata file: {str(e)}"

        return True, f"Successfully removed access to {path} globally"

    except GLib.Error as e:
        return False, f"Failed to modify permissions: {str(e)}"

def global_list_file_perms(override=True, system=False) -> tuple[bool, dict[str, list[str]]]|tuple[bool, dict[str, list[str]]]:
    """
    List filesystem permissions for all Flatpak applications globally.

    Args:
        override (bool): Whether to use global metadata file instead of per-app.
        system (bool): Whether to check system-wide or user installation

    Returns:
        tuple[bool, dict[str, list[str]]]: (success, permissions_dict)
            permissions_dict contains:
                - 'paths': list of filesystem paths
                - 'special_paths': list of special paths (home, host, etc.)
    """
    try:
        key_file = get_perm_key_file(None, override, system)

        # Initialize result dictionary
        result = {
            "paths": [],
            "special_paths": []
        }

        # Get existing filesystem paths
        existing_paths = key_file.get_string("Context", "filesystems")
        if existing_paths:
            # Split and clean the paths
            paths_list = [p.strip() for p in existing_paths.split(';')]

            # Separate special paths from regular ones
            for path in paths_list:
                if path in ["home", "host", "host-os", "host-etc"]:
                    result["special_paths"].append(path)
                else:
                    result["paths"].append(path)

        return True, result
    except GLib.Error:
        return False, {"paths": [], "special_paths": []}


def global_list_other_perm_toggles(perm_type: str, override=True, system=False) -> tuple[bool, dict[str, list[str]]]|tuple[bool, dict[str, list[str]]]:
    """
    List other permission toggles within "Context" for all Flatpak applications globally.

    Args:
        perm_type (str): The type of permissions to list (e.g. "shared", "sockets", "devices", "features", "persistent")
        override (bool): Whether to use global metadata file instead of per-app.
        system (bool): Whether to check system-wide or user installation

    Returns:
        tuple[bool, dict[str, list[str]]]: (success, permissions_dict)
            permissions_dict contains:
                - 'paths': list of filesystem paths
    """
    try:
        key_file = get_perm_key_file(None, override, system)

        # Initialize result dictionary
        result = {
            "paths": []
        }

        # Get existing filesystem paths
        existing_paths = key_file.get_string("Context", perm_type)
        if existing_paths:
            # Split, clean, and filter out empty paths
            paths_list = [p.strip() for p in existing_paths.split(';') if p.strip()]

            # Add filtered paths to result
            result["paths"] = paths_list

        return True, result
    except GLib.Error:
        return False, {"paths": []}

    # Get existing filesystem paths
    existing_paths = key_file.get_string("Context", perm_type)
    if existing_paths:
        # Split, clean, and filter out empty paths
        paths_list = [p.strip() for p in existing_paths.split(';') if p.strip()]

        # Add filtered paths to result
        result["paths"] = paths_list


def global_toggle_other_perms(perm_type: str, option: str, enable: bool, override=True, system=False) -> tuple[bool, str]:
    """
    Toggle a specific permission option for all Flatpak applications globally.

    Args:
        perm_type (str): The type of permissions (shared, sockets, devices, features)
        option (str): The specific permission to toggle
        enable (bool): Whether to enable or disable the permission
        override (bool): Whether to use global metadata file instead of per-app.
        system (bool): Whether to check system-wide or user installation

    Returns:
        bool: True if successful, False if operation failed
    """
    # Get the KeyFile object
    key_file = get_perm_key_file(None, override, system)

    if not key_file:
        return False, "Failed to get permissions globally"

    try:
        perms_list = []
        # Get all keys in the Context section
        # Check if Context section exists
        if not key_file.has_group("Context"):
            key_file.set_string("Context", perm_type, "")

        # Now get the keys
        context_keys = key_file.get_keys("Context")

        # Check if perm_type exists in the section
        if perm_type not in str(context_keys):
            # Create the key with an empty string
            key_file.set_string("Context", perm_type, "")

        # Get the existing permissions
        existing_perms = key_file.get_string("Context", perm_type)

        if existing_perms:
            # Split into individual permissions
            perms_list = [perm.strip() for perm in existing_perms.split(';') if perm.strip()]

        # Toggle permission
        if enable:
            if option not in perms_list:
                perms_list.append(option)
        else:
            if option in perms_list:
                perms_list.remove(option)

        # Join back with semicolons
        new_perms = ";".join(perms_list)

        # Save changes
        if new_perms:
            key_file.set_string("Context", perm_type, new_perms)
        else:
            key_file.remove_key("Context", perm_type)
        key_file.save_to_file(get_metadata_path(None, override, system))

        return True, f"Successfully {'enabled' if enable else 'disabled'} {option} globally"

    except GLib.Error:
        return False, f"Failed to toggle {option} globally"


def global_list_other_perm_values(perm_type: str, override=True, system=False) -> tuple[bool, dict[str, list[str]]]:
    """
    List all permission values for a specified type for all Flatpak applications globally.

    Args:
        perm_type (str): The type of permissions to list (e.g. "environment", "session_bus", "system_bus")
        override (bool): Whether to use global metadata file instead of per-app.
        system (bool): Whether to check system-wide or user installation

    Returns:
        tuple[bool, dict[str, list[str]]]: (success, env_vars_dict)
            env_vars_dict contains:
                - 'paths': list of environment variables
    """
    try:
        key_file = get_perm_key_file(None, override, system)

        # Initialize result dictionary
        result = {
            "paths": []
        }

        match perm_type.lower():
            case "environment":
                perm_type = "Environment"
            case "session_bus":
                perm_type = "Session Bus Policy"
            case "system_bus":
                perm_type = "System Bus Policy"
            case _:
                return False, {"paths": []}

        # Check if section exists using has_group()
        if key_file.has_group(perm_type):
            # Get all keys in the section
            keys = key_file.get_keys(perm_type)

            # Convert ResultTuple to list of individual keys
            keys = list(keys[0]) if hasattr(keys, '__iter__') else []

            # Get each value and add to paths list
            for key in keys:
                value = key_file.get_string(perm_type, key)
                if value:
                    result["paths"].append(f"{key}={value}")

        return True, result
    except GLib.Error as e:
        print(f"GLib.Error: {e}")
        return False, {"paths": []}
    except Exception as e:
        print(f"Other error: {e}")
        return False, {"paths": []}

def global_add_permission_value(perm_type: str, value: str, override=True, system=False) -> tuple[bool, str]:
    """
    Add a permission value to all Flatpak applications globally.

    Args:
        perm_type (str): The type of permissions (e.g. "environment", "session_bus", "system_bus")
        value (str): The complete permission value to add (e.g. "XCURSOR_PATH=/run/host/user-share/icons:/run/host/share/icons")
        override (bool): Whether to use global metadata file instead of per-app.
        system (bool): Whether to modify system-wide or user installation

    Returns:
        tuple[bool, str]: (success, message)
    """
    try:
        key_file = get_perm_key_file(None, override, system)

        # Convert perm_type to the correct format
        match perm_type.lower():
            case "environment":
                perm_type = "Environment"
            case "session_bus":
                perm_type = "Session Bus Policy"
            case "system_bus":
                perm_type = "System Bus Policy"
            case _:
                return False, "Invalid permission type"

        # Split the value into key and actual value
        parts = value.split('=', 1)
        if len(parts) != 2:
            return False, "Value must be in format 'key=value'"

        key, val = parts

        if perm_type in ['Session Bus Policy', 'System Bus Policy']:
            if val not in ['talk', 'own']:
                return False, "Value must be in format 'key=value' with value as 'talk' or 'own'"

        # Set the value
        key_file.set_string(perm_type, key, val)

        # Save the changes
        key_file.save_to_file(get_metadata_path(None, override, system))

        return True, f"Successfully added {value} to {perm_type} section"
    except GLib.Error as e:
        return False, f"Error adding permission: {str(e)}"


def global_remove_permission_value(perm_type: str, value: str, override=True, system=False) -> tuple[bool, str]:
    """
    Remove a permission value from all Flatpak applications globally.

    Args:
        perm_type (str): The type of permissions (e.g. "environment", "session_bus", "system_bus")
        value (str): The complete permission value to remove (e.g. "XCURSOR_PATH=/run/host/user-share/icons:/run/host/share/icons")
        override (bool): Whether to use global metadata file instead of per-app.
        system (bool): Whether to modify system-wide or user installation

    Returns:
        tuple[bool, str]: (success, message)
    """
    try:
        key_file = get_perm_key_file(None, override, system)

        # Convert perm_type to the correct format
        match perm_type.lower():
            case "environment":
                perm_type = "Environment"
            case "session_bus":
                perm_type = "Session Bus Policy"
            case "system_bus":
                perm_type = "System Bus Policy"
            case _:
                return False, "Invalid permission type"

        # Split the value into key and actual value
        parts = value.split('=', 1)
        if len(parts) != 2:
            return False, "Value must be in format 'key=value'"

        key, val = parts
        # Check if section exists
        if not key_file.has_group(perm_type):
            return False, f"Section {perm_type} does not exist"

        # Remove the value
        key_file.remove_key(perm_type, key)

        # Save the changes
        key_file.save_to_file(get_metadata_path(None, override, system))

        return True, f"Successfully removed {value} from {perm_type} section"
    except GLib.Error as e:
        return False, f"Error removing permission: {str(e)}"

def portal_get_permission_store():
    bus = dbus.SessionBus()
    portal_service = bus.get_object("org.freedesktop.impl.portal.PermissionStore", "/org/freedesktop/impl/portal/PermissionStore")
    permission_store = dbus.Interface(portal_service, "org.freedesktop.impl.portal.PermissionStore")
    return permission_store

def portal_set_app_permissions(portal: str, app_id: str, status_str: str):

    portal_id = ""
    # This is done separately incase user types "notification" instead of "notifications"
    if portal.lower() in "notifications":
        portal = "notifications"

    status = "no"
    if status_str in ["yes", "true", "1", "enable"]:
        status = "yes"

    match portal.lower():
        case "background":
            portal_id = "background"
        case "notifications":
            portal_id = "notification"
        case "microphone":
            portal = "devices"
            portal_id = "microphone"
        case "speakers":
            portal = "devices"
            portal_id = "speakers"
        case "camera":
            portal = "devices"
            portal_id = "camera"
        case "location":
            portal_id = "location"
    try:
        permission_store = portal_get_permission_store()
        permission_store.SetPermission(
            portal,   # Category (string)
            False,             # Permission status (boolean: False means 'no')
            portal_id,    # Permission type (string)
            app_id, # App ID (string)
            [dbus.String(status)] # Array of permissions (string array)
        )
        return True, f"Permission set to {status} for {app_id} in {portal_id} portal"
    except:
        return False, f"Failed to set permission for {app_id} in {portal_id} portal"

def portal_get_app_permissions(app_id: str):
    permissions = portal_lookup_all()
    if not permissions:
        return False, f"Permission not found for {app_id} in any portal"

    # Store results for each portal where we find the app
    app_permissions = {}

    # Iterate through all portal entries
    for portal_id, permission_data in permissions:
        # Extract the DBus Dictionary containing app permissions
        dbus_dict = permission_data[0]  # First element contains the dictionary

        # Check if our target app_id exists in the dictionary
        if app_id in dbus_dict:
            # Get the array of values for this app
            value_array = dbus_dict[app_id]

            # Return the first string value (typically 'yes' or 'no')
            if len(value_array) > 0:
                app_permissions[portal_id] = str(value_array[0])

    # Format and return the results
    if app_permissions:
        return True, app_permissions

    return False, f"No permissions found for {app_id} in any portal"


def portal_lookup(portal: str):
    try:
        portal_id = ""
        # This is done separately incase user types "notification" instead of "notifications"
        if portal.lower() in "notifications":
            portal = "notifications"

        match portal.lower():
            case "background":
                portal_id = "background"
            case "notifications":
                portal_id = "notification"
            case "microphone":
                portal = "devices"
                portal_id = "microphone"
            case "speakers":
                portal = "devices"
                portal_id = "speakers"
            case "camera":
                portal = "devices"
                portal_id = "camera"
            case "location":
                portal_id = "location"

        permission_store = portal_get_permission_store()
        permissions = permission_store.Lookup(
            portal,   # Category (string)
            portal_id    # Permission type (string)
        )
        if permissions:
            return permissions
    except dbus.exceptions.DBusException:
        # We don't care if a lookup fails, that just means no options were set for the portal
        return []

def portal_lookup_all():
    portal_permissions = []
    # This is done separately incase user types "notification" instead of "notifications"

    portal_names = ["background", "notifications", "microphone", "speakers", "camera", "location"]
    for portal in portal_names:
        try:
            permissions = portal_lookup(
                portal   # Category (string)
            )
            if permissions:
                portal_permissions.append((portal, permissions))
        except dbus.exceptions.DBusException:
            # We don't care if a lookup fails, that just means no options were set for the portal
            return []
    return portal_permissions

def screenshot_details(screenshot):
    # Try to get the image with required parameters
    try:
        # get_image() requires 4 arguments: width, height, scale, device_scale
        image = screenshot.get_image(800, 600, 1.0)
        return image
    except Exception as e:
        print(f"Error getting image: {e}")

def main():
    parser = argparse.ArgumentParser(description='Search Flatpak packages')
    parser.add_argument('--id', help='Application ID to search for')
    parser.add_argument('--repo', help='Filter results to specific repository')
    parser.add_argument('--list-all', action='store_true', help='List all available apps')
    parser.add_argument('--categories', action='store_true', help='Show apps grouped by category')
    parser.add_argument('--subcategories', action='store_true',
                       help='Show apps grouped by subcategory')
    parser.add_argument('--list-installed', action='store_true',
                       help='List all installed Flatpak applications')
    parser.add_argument('--check-updates', action='store_true',
                       help='Check for available updates')
    parser.add_argument('--list-repos', action='store_true',
                       help='List all configured Flatpak repositories')
    parser.add_argument('--add-repo', type=str, metavar='REPO_FILE',
                       help='Add a new repository from a .flatpakrepo file')
    parser.add_argument('--remove-repo', type=str, metavar='REPO_NAME',
                       help='Remove a Flatpak repository')
    parser.add_argument('--toggle-repo', type=str,
                       metavar=('ENABLE/DISABLE'),
                       help='Enable or disable a repository')
    parser.add_argument('--install', type=str, metavar='APP_ID',
                       help='Install a Flatpak package')
    parser.add_argument('--remove', type=str, metavar='APP_ID',
                       help='Remove a Flatpak package')
    parser.add_argument('--update', type=str, metavar='APP_ID',
                       help='Update a Flatpak package')
    parser.add_argument('--update-all', action='store_true',
                       help='Update all Flatpak packages')
    parser.add_argument('--system', action='store_true', help='Install as system instead of user')
    parser.add_argument('--refresh', action='store_true', help='Install as system instead of user')
    parser.add_argument('--refresh-local', action='store_true', help='Install as system instead of user')
    parser.add_argument('--add-file-perms', type=str, metavar='PATH',
                        help='Add file permissions to an app (e.g. any defaults: host, host-os, host-etc, home, or "/path/to/directory" for custom paths)')
    parser.add_argument('--remove-file-perms', type=str, metavar='PATH',
                        help='Remove file permissions from an app (e.g. any defaults: host, host-os, host-etc, home, or "/path/to/directory" for custom paths)')
    parser.add_argument('--list-file-perms', action='store_true',
                       help='List configured file permissions for an app')
    parser.add_argument('--list-other-perm-toggles', type=str, metavar='PERM_NAME',
                       help='List configured other permission toggles for an app (e.g. "shared", "sockets", "devices", "features")')
    parser.add_argument('--toggle-other-perms', type=str, metavar=('ENABLE/DISABLE'),
                        help='Toggle other permissions on/off (True/False)')
    parser.add_argument('--perm-type', type=str,
                        help='Type of permission to toggle (shared, sockets, devices, features, persistent)')
    parser.add_argument('--perm-option', type=str,
                        help='Specific permission option to toggle (e.g. network, ipc)')
    parser.add_argument('--list-other-perm-values', type=str, metavar='PERM_NAME',
                       help='List configured other permission group values for an app (e.g. "environment", "session_bus", "system_bus")')
    parser.add_argument('--add-other-perm-values', type=str, metavar='TYPE',
                        help='Add a permission value (e.g. "environment", "session_bus", "system_bus")')
    parser.add_argument('--remove-other-perm-values', type=str, metavar='TYPE',
                        help='Remove a permission value (e.g. "environment", "session_bus", "system_bus")')
    parser.add_argument('--perm-value', type=str, metavar='VALUE',
                        help='The complete permission value to add or remove (e.g. "XCURSOR_PATH=/run/host/user-share/icons:/run/host/share/icons")')
    parser.add_argument('--override', action='store_true', help='Set global permission override instead of per-application')
    parser.add_argument('--global-add-file-perms', type=str, metavar='PATH',
                        help='Add file permissions to an app (e.g. any defaults: host, host-os, host-etc, home, or "/path/to/directory" for custom paths)')
    parser.add_argument('--global-remove-file-perms', type=str, metavar='PATH',
                        help='Remove file permissions from an app (e.g. any defaults: host, host-os, host-etc, home, or "/path/to/directory" for custom paths)')
    parser.add_argument('--global-list-file-perms', action='store_true',
                       help='List configured file permissions for an app')
    parser.add_argument('--global-list-other-perm-toggles', type=str, metavar='PERM_NAME',
                       help='List configured other permission toggles for an app (e.g. "shared", "sockets", "devices", "features")')
    parser.add_argument('--global-toggle-other-perms', type=str, metavar=('ENABLE/DISABLE'),
                        help='Toggle other permissions on/off (True/False)')
    parser.add_argument('--global-list-other-perm-values', type=str, metavar='PERM_NAME',
                       help='List configured other permission group values for an app (e.g. "environment", "session_bus", "system_bus")')
    parser.add_argument('--global-add-other-perm-values', type=str, metavar='TYPE',
                        help='Add a permission value (e.g. "environment", "session_bus", "system_bus")')
    parser.add_argument('--global-remove-other-perm-values', type=str, metavar='TYPE',
                        help='Remove a permission value (e.g. "environment", "session_bus", "system_bus")')
    parser.add_argument('--get-app-portal-permissions', action='store_true',
                        help='Check specified portal permissions  (e.g. "background", "notifications", "microphone", "speakers", "camera", "location") for a specified application ID.')
    parser.add_argument('--get-portal-permissions',  type=str, metavar='TYPE',
                        help='List all current portal permissions for all applications')
    parser.add_argument('--get-all-portal-permissions', action='store_true',
                        help='List all current portal permissions for all applications')
    parser.add_argument('--set-app-portal-permissions', type=str, metavar='TYPE',
                        help='Set specified portal permissions  (e.g. "background", "notifications", "microphone", "speakers", "camera", "location") yes/no for a specified application ID.')
    parser.add_argument('--portal-perm-value', type=str, metavar='TYPE',
                        help='Set specified portal permissions value (yes/no) for a specified application ID.')

    args = parser.parse_args()

    # Handle repository operations
    if args.toggle_repo:
        handle_repo_toggle(args)
        return

    if args.list_repos:
        handle_list_repos(args)
        return

    if args.add_repo:
        handle_add_repo(args)
        return

    if args.remove_repo:
        handle_remove_repo(args)
        return

    # Handle package operations
    searcher = get_reposearcher(args.system)

    if args.install:
        handle_install(args, searcher)
        return

    if args.remove:
        handle_remove(args, searcher)
        return

    if args.update:
        handle_update(args, searcher)
        return

    if args.update_all:
        handle_update_all(args, searcher)
        return

    # Handle information operations
    if args.list_installed:
        handle_list_installed(args, searcher)
        return

    if args.check_updates:
        handle_check_updates(args, searcher)
        return

    if args.list_all:
        handle_list_all(args, searcher)
        return

    if args.categories:
        handle_categories(args, searcher)
        return

    if args.subcategories:
        handle_subcategories(args, searcher)
        return

    if args.id:
        if args.add_file_perms:
            handle_add_file_perms(args, searcher)
            return
        if args.remove_file_perms:
            handle_remove_file_perms(args, searcher)
            return
        if args.list_file_perms:
            handle_list_file_perms(args, searcher)
            return
        if args.list_other_perm_toggles:
            handle_list_other_perm_toggles(args, searcher)
            return
        if args.list_other_perm_values:
            handle_list_other_perm_values(args, searcher)
            return
        if args.toggle_other_perms:
            handle_toggle_other_perms(args, searcher)
            return
        if args.add_other_perm_values:
            handle_add_other_perm_values(args, searcher)
            return
        if args.remove_other_perm_values:
            handle_remove_other_perm_values(args, searcher)
            return
        if args.get_app_portal_permissions:
            handle_get_app_portal_permissions(args, searcher)
            return
        if args.set_app_portal_permissions:
            handle_set_app_portal_permissions(args, searcher)
            return
        else:
            handle_search(args, searcher)
        return

    if args.override:
        if args.global_add_file_perms:
            handle_global_add_file_perms(args, searcher)
            return
        if args.global_remove_file_perms:
            handle_global_remove_file_perms(args, searcher)
            return
        if args.global_list_file_perms:
            handle_global_list_file_perms(args, searcher)
            return
        if args.global_list_other_perm_toggles:
            handle_global_list_other_perm_toggles(args, searcher)
            return
        if args.global_list_other_perm_values:
            handle_global_list_other_perm_values(args, searcher)
            return
        if args.global_toggle_other_perms:
            handle_global_toggle_other_perms(args, searcher)
            return
        if args.global_add_other_perm_values:
            handle_global_add_other_perm_values(args, searcher)
            return
        if args.global_remove_other_perm_values:
            handle_global_remove_other_perm_values(args, searcher)
            return
        else:
            print("Missing options. Use -h for help.")

    if args.get_all_portal_permissions:
        result = portal_lookup_all()
        if result:
            print("\nPortal Permissions:")
            print("-" * 50)
            for portal_id, permissions in result:
                print(f"{portal_id}: {permissions}")
        else:
            print("No app permissions found set for any portals")
        return

    if args.get_portal_permissions:
        result = portal_lookup(args.get_portal_permissions)
        if result:
            print("\nPortal Permissions:")
            print("-" * 50)
            for permissions in result:
                print(f"{args.get_portal_permissions}: {permissions}")
        else:
            print(f"No app permissions found for {args.get_portal_permissions} portal")
        return

    print("Missing options. Use -h for help.")

def handle_repo_toggle(args):
    repo_name = args.repo
    if not repo_name:
        print("Error: must specify a repo.")
        sys.exit(1)

    get_status = args.toggle_repo.lower() in ['true', 'enable']
    try:
        success, message = repotoggle(repo_name, get_status, args.system)
        print(f"{message}")
    except GLib.Error as e:
        print(f"{str(e)}")

def handle_list_repos(args):
    repos = repolist(args.system)
    print("\nConfigured Repositories:")
    for repo in repos:
        print(f"- {repo.get_name()} ({repo.get_url()})")

def handle_add_repo(args):
    try:
        success, message = repoadd(args.add_repo, args.system)
        print(f"{message}")
    except GLib.Error as e:
        print(f"{str(e)}")

def handle_remove_repo(args):
    repodelete(args.remove_repo, args.system)
    print(f"\nRepository removed successfully: {args.remove_repo}")

def handle_install(args, searcher):
    if args.install.endswith('.flatpakref'):
        try:
            success, message = install_flatpakref(args.install, args.system)
            result_message = f"{message}"
        except GLib.Error as e:
            result_message = f"Installation of {args.install} failed: {str(e)}"
        print(result_message)
    else:
        packagelist = searcher.search_flatpak(args.install, args.repo)
        result_message = ""
        for package in packagelist:
            try:
                success, message = install_flatpak(package, args.repo, args.system)
                result_message = f"{message}"
                break
            except GLib.Error as e:
                result_message = f"Installation of {args.install} failed: {str(e)}"
                pass
        print(result_message)

def handle_remove(args, searcher):
    packagelist = searcher.search_flatpak(args.remove, args.repo)
    result_message = ""
    for package in packagelist:
        try:
            success, message = remove_flatpak(package, args.system)
            result_message = f"{message}"
            break
        except GLib.Error as e:
            result_message = f"Removal of {args.remove} failed: {str(e)}"
            pass
    print(result_message)

def handle_update(args, searcher):
    packagelist = searcher.search_flatpak(args.update)
    result_message = ""
    for package in packagelist:
        try:
            success, message = update_flatpak(package, args.system)
            result_message = f"{message}"
            break
        except GLib.Error as e:
            result_message = f"Update of {args.update} failed: {str(e)}"
            pass
    print(result_message)

def handle_update_all(args, searcher):
    packagelist = searcher.search_flatpak(args.update)
    result_message = ""
    for package in packagelist:
        try:
            success, message = update_all_flatpaks(package, args.system)
            result_message = f"{message}"
            break
        except GLib.Error as e:
            result_message = f"Unable to apply updates: {str(e)}"
            pass
    print(result_message)

def handle_list_installed(args, searcher):
    installed_apps = searcher.get_installed_apps(args.system)
    print(f"\nInstalled Flatpak Applications ({len(installed_apps)}):")
    for app_id, repo_name, repo_type in installed_apps:
        print(f"{app_id} (Repository: {repo_name}, Installation: {repo_type})")

def handle_check_updates(args, searcher):
    updates = searcher.check_updates(args.system)
    print(f"\nAvailable Updates ({len(updates)}):")
    for app_id, repo_name, repo_type in updates:
        print(f"{app_id} (Repository: {repo_name}, Installation: {repo_type})")

def handle_list_all(args, searcher):
    apps = searcher.get_all_apps(args.repo)
    for app in apps:
        details = app.get_details()
        print(f"Name: {details['name']}")
        print(f"Categories: {', '.join(details['categories'])}")
        print("-" * 50)

def handle_categories(args, searcher):
    categories = searcher.get_categories_summary(args.repo)
    for category, apps in categories.items():
        print(f"\n{category.upper()}:")
        for app in apps:
            print(f"  - {app.name} ({app.id})")

def handle_subcategories(args, searcher):
    """Handle showing apps grouped by subcategory."""
    subcategories = searcher.get_subcategories_summary(args.repo)
    for category, subcategory, apps in subcategories:
        print(f"\n{category.upper()} > {subcategory.upper()}:")
        for app in apps:
            print(f"  - {app.name} ({app.id})")

def handle_add_file_perms(args, searcher):
    try:
        success, message = add_file_permissions(args.id, args.add_file_perms, args.perm_type, args.system)
        print(f"{message}")
    except GLib.Error as e:
        print(f"{str(e)}")

def handle_remove_file_perms(args, searcher):
    try:
        success, message = remove_file_permissions(args.id, args.remove_file_perms, args.perm_type, args.system)
        print(f"{message}")
    except GLib.Error as e:
        print(f"{str(e)}")

def handle_list_file_perms(args, searcher):
    try:
        success, message = list_file_perms(args.id, args.system)
        print(f"{message}")
    except GLib.Error as e:
        print(f"{str(e)}")

def handle_list_other_perm_toggles(args, searcher):
    try:
        success, message = list_other_perm_toggles(args.id, args.list_other_perm_toggles, args.system)
        print(f"{message}")
    except GLib.Error as e:
        print(f"{str(e)}")

def handle_toggle_other_perms(args, searcher):
    if not args.perm_type:
        print("Error: must specify --perm-type")
        return
    if not args.perm_option:
        print("Error: must specify --perm-option")
        return
    get_status = args.toggle_other_perms.lower() in ['true', 'enable']
    try:
        success, message = toggle_other_perms(args.id, args.perm_type, args.perm_option, get_status, args.system)
        print(f"{message}")
    except GLib.Error as e:
        print(f"{str(e)}")

def handle_list_other_perm_values(args, searcher):
    try:
        success, message = list_other_perm_values(args.id, args.list_other_perm_values, args.system)
        print(f"{message}")
    except GLib.Error as e:
        print(f"{str(e)}")

def handle_add_other_perm_values(args, searcher):
    if not args.id:
        print("Error: must specify --id")
        return

    if not args.add_other_perm_values:
        print("Error: must specify which perm value")
        return

    if not args.perm_value:
        print("Error: must specify --perm-value")
        return
    try:
        success, message = add_permission_value(args.id, args.add_other_perm_values, args.perm_value, args.system)
        print(message)
    except GLib.Error as e:
        print(f"{str(e)}")

def handle_remove_other_perm_values(args, searcher):
    if not args.id:
        print("Error: must specify --id")
        return

    if not args.remove_other_perm_values:
        print("Error: must specify which perm value")
        return

    if not args.perm_value:
        print("Error: must specify --perm-value")
        return

    try:
        success, message = remove_permission_value(args.id, args.remove_other_perm_values, args.perm_value, args.system)
        print(message)
    except GLib.Error as e:
        print(f"{str(e)}")

def handle_global_add_file_perms(args, searcher):
    try:
        success, message = global_add_file_permissions(args.global_add_file_perms, True, args.system)
        print(f"{message}")
    except GLib.Error as e:
        print(f"{str(e)}")

def handle_global_remove_file_perms(args, searcher):
    try:
        success, message = global_remove_file_permissions(args.global_remove_file_perms, True, args.system)
        print(f"{message}")
    except GLib.Error as e:
        print(f"{str(e)}")

def handle_global_list_file_perms(args, searcher):
    try:
        success, message = global_list_file_perms(True, args.system)
        print(f"{message}")
    except GLib.Error as e:
        print(f"{str(e)}")

def handle_global_list_other_perm_toggles(args, searcher):
    try:
        success, message = global_list_other_perm_toggles(args.global_list_other_perm_toggles, True, args.system)
        print(f"{message}")
    except GLib.Error as e:
        print(f"{str(e)}")

def handle_global_toggle_other_perms(args, searcher):
    if not args.perm_type:
        print("Error: must specify --perm-type")
        return
    if not args.perm_option:
        print("Error: must specify --perm-option")
        return
    get_status = args.global_toggle_other_perms.lower() in ['true', 'enable']
    try:
        success, message = global_toggle_other_perms(args.perm_type, args.perm_option, get_status, True, args.system)
        print(f"{message}")
    except GLib.Error as e:
        print(f"{str(e)}")

def handle_global_list_other_perm_values(args, searcher):
    try:
        success, message = global_list_other_perm_values(args.global_list_other_perm_values, True, args.system)
        print(f"{message}")
    except GLib.Error as e:
        print(f"{str(e)}")

def handle_global_add_other_perm_values(args, searcher):
    if not args.global_add_other_perm_values:
        print("Error: must specify which perm value")
        return
    if not args.perm_value:
        print("Error: must specify --perm-value")
        return
    try:
        success, message = global_add_permission_value(args.global_add_other_perm_values, args.perm_value, True, args.system)
        print(message)
    except GLib.Error as e:
        print(f"{str(e)}")

def handle_global_remove_other_perm_values(args, searcher):
    if not args.global_remove_other_perm_values:
        print("Error: must specify which perm value")
        return

    if not args.perm_value:
        print("Error: must specify --perm-value")
        return
    try:
        success, message = global_remove_permission_value(args.global_remove_other_perm_values, args.perm_value, True, args.system)
        print(message)
    except GLib.Error as e:
        print(f"{str(e)}")

def handle_set_app_portal_permissions(args, searcher):
    if not args.id:
        print("Error: must specify --id")
        return
    if not args.set_app_portal_permissions:
        print("Error: must specify which portal")
        return
    if not args.portal_perm_value:
        print("Error: must specify --portal-perm-value")
        return
    if args.portal_perm_value.lower() in ['true', 'enable', 'yes', '1']:
        status_str = "yes"
    else:
        status_str = "no"
    try:
        success, message = portal_set_app_permissions(args.set_app_portal_permissions, args.id, status_str)
        print(f"{message}")
    except dbus.exceptions.DBusException as e:
        print(f"{str(e)}")

def handle_get_app_portal_permissions(args, searcher):
    if not args.id:
        print("Error: must specify --id")
        return
    try:
        success, message = portal_get_app_permissions(args.id)
        print(f"{message}")
    except dbus.exceptions.DBusException as e:
        print(f"{str(e)}")


def handle_search(args, searcher):
    if args.repo:
        search_results = searcher.search_flatpak(args.id, args.repo)
    else:
        search_results = searcher.search_flatpak(args.id)

    if search_results:
        for package in search_results:
            details = package.get_details()
            print(f"Name: {details['name']}")
            print(f"ID: {details['id']}")
            print(f"Kind: {details['kind']}")
            print(f"Summary: {details['summary']}")
            print(f"Description: {details['description']}")
            print(f"Version: {details['version']}")
            print(f"Icon URL: {details['icon_url']}")
            print(f"Icon PATH 128x128: {details['icon_path_128']}")
            print(f"Icon PATH 64x64: {details['icon_path_64']}")
            print(f"Icon FILE: {details['icon_filename']}")
            print(f"Developer: {details['developer']}")
            print(f"Categories: {details['categories']}")
            urls = details['urls']
            print(f"Donation URL: {urls['donation']}")
            print(f"Homepage URL: {urls['homepage']}")
            print(f"Bug Tracker URL: {urls['bugtracker']}")
            print(f"Bundle ID: {details['bundle_id']}")
            print(f"Match Type: {details['match_type']}")
            print(f"Repo: {details['repo']}")
            print("Screenshots:")
            for i, screenshot in enumerate(details['screenshots'], 1):
                print(f"\nScreenshot #{i}:")
                image = screenshot_details(screenshot)
                if image:
                    # Get image properties using the correct methods
                    print("\nImage Properties:")
                    print(f"URL: {image.get_url()}")
                    print(f"Width: {image.get_width()}")
                    print(f"Height: {image.get_height()}")
                    print(f"Scale: {image.get_scale()}")
                    print(f"Locale: {image.get_locale()}")

            print("-" * 50)

if __name__ == "__main__":
    main()
