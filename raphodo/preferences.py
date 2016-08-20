#!/usr/bin/env python3

# Copyright (C) 2011-2016 Damon Lynch <damonlynch@gmail.com>

# This file is part of Rapid Photo Downloader.
#
# Rapid Photo Downloader is free software: you can redistribute it and/or
# modify it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Rapid Photo Downloader is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Rapid Photo Downloader.  If not,
# see <http://www.gnu.org/licenses/>.

__author__ = 'Damon Lynch'
__copyright__ = "Copyright 2011-2016, Damon Lynch"

import logging
import re
import os
import pkg_resources
import datetime
from typing import List, Tuple, Optional

from PyQt5.QtCore import QSettings

from gettext import gettext as _

from raphodo.storage import (xdg_photos_directory, xdg_videos_directory, xdg_photos_identifier,
                             xdg_videos_identifier)
from raphodo.generatenameconfig import *
import raphodo.constants as constants
from raphodo.utilities import available_cpu_count
import raphodo.__about__


class ScanPreferences:
    r"""
    Handle user preferences while scanning devices like memory cards,
    cameras or the filesystem. Pickled and passed between processes.

    Sets data attribute valid to True if ignored paths are valid. An ignored
    path is always assumed to be valid unless regular expressions are used.
    If regular expressions are used, then it is valid only if a valid
    regular expression can be compiled from each line.

    >>> no_ignored_paths = ScanPreferences([])
    >>> no_ignored_paths.valid
    True

    >>> some_paths = ScanPreferences(['.Trash', '.thumbnails'])
    >>> some_paths.valid
    True

    >>> some_re_paths = ScanPreferences(['.Trash', '\.[tT]humbnails'], True)
    >>> some_re_paths.valid
    True

    >>> some_more_re_paths = ScanPreferences(['.Trash', '\.[tThumbnails'], True)
    >>> some_more_re_paths.valid
    False
    """

    def __init__(self, ignored_paths, use_regular_expressions=False):
        """
        :type ignored_paths: List[str]
        :type use_regular_expressions: bool
        """

        self.ignored_paths = ignored_paths
        self.use_regular_expressions = use_regular_expressions

        if ignored_paths and use_regular_expressions:
            self.valid = self._check_and_compile_re()
        else:
            self.re_pattern = None
            self.valid = True

    def scan_this_path(self, path: str) -> bool:
        """
        Returns true if the path should be included in the scan.
        Assumes path is a full path

        :return: True|False

        """
        if not self.ignored_paths:
            return True
        if not self.use_regular_expressions:
            return not path.endswith(tuple(self.ignored_paths))
        return not self.re_pattern.match(path)

    def _check_and_compile_re(self) -> bool:
        """
        Take the ignored paths and attempt to compile a regular expression
        out of them. Checks line by line.

        :return: True if there were no problems creating the regular
        expression pattern
        """

        assert self.use_regular_expressions

        error_encountered = False
        pattern = ''
        for path in self.ignored_paths:
            # check path for validity
            try:
                re.match(path, '')
                pattern += '.*{}s$|'.format(path)
            except re.error:
                logging.error("Ignoring malformed regular expression: {}".format(path))
                error_encountered = True

        if pattern:
            pattern = pattern[:-1]

            try:
                self.re_pattern = re.compile(pattern)
            except re.error:
                logging.error('This regular expression is invalid: {}'.format(pattern))
                self.re_pattern = None
                error_encountered = True

        logging.debug("Ignored paths regular expression pattern: {}".format(pattern))

        return not error_encountered


class DownloadsTodayTracker:
    """
    Handles tracking the number of successful downloads undertaken
    during any one day.

    When a day starts is flexible. See for more details:
    http://damonlynch.net/rapid/documentation/#renameoptions
    """

    def __init__(self, downloads_today: List[str], day_start: str) -> None:
        """

        :param downloads_today: list[str,str] containing date and the
         number of downloads today e.g. ['2015-08-15', '25']
        :param day_start: the time the day starts, e.g. "03:00"
         indicates the day starts at 3 a.m.
        """
        self.day_start = day_start
        self.downloads_today = downloads_today

    def get_or_reset_downloads_today(self) -> int:
        """
        Primary method to get the Downloads Today value, because it
        resets the value if no downloads have already occurred on the
        day of the download.
        :return: the number of successful downloads that have occurred
        today
        """
        v = self.get_downloads_today()
        if v <= 0:
            self.reset_downloads_today()
            # -1 was returned in the Gtk+ version of Rapid Photo Downloader -
            # why?
            v = 0
        return v

    def get_downloads_today(self) -> int:
        """
        :return the preference value for the number of successful
        downloads performed today. If value is less than zero,
        the date has changed since the value was last updated.
        """

        hour, minute = self.get_day_start()
        try:
            adjusted_today = datetime.datetime.strptime(
                "%s %s:%s" % (self.downloads_today[0], hour, minute),
                "%Y-%m-%d %H:%M")
        except:
            logging.critical(
                "Failed to calculate date adjustment. Download today values "
                "appear to be corrupted: %s %s:%s",
                self.downloads_today[0], hour, minute)
            adjusted_today = None

        now = datetime.datetime.today()

        if adjusted_today is None:
            return -1

        if now < adjusted_today:
            try:
                return int(self.downloads_today[1])
            except ValueError:
                logging.error(
                    "Invalid Downloads Today value. Resetting value to zero.")
                self.reset_downloads_today()
                return 0
        else:
            return -1

    def get_day_start(self) -> Tuple[int, int]:
        try:
            t1, t2 = self.day_start.split(":")
            return int(t1), int(t2)
        except ValueError:
            logging.error(
                "'Start of day' preference value %s is corrupted. Resetting "
                "to midnight",
                self.day_start)
            self.day_start = "0:0"
            return 0, 0

    def increment_downloads_today(self) -> bool:
        """
        :return: True if day changed
        """
        v = self.get_downloads_today()
        if v >= 0:
            self.set_downloads_today(self.downloads_today[0], v + 1)
            return False
        else:
            self.reset_downloads_today(1)
            return True

    def reset_downloads_today(self, value: int=0) -> None:
        now = datetime.datetime.today()
        hour, minute = self.get_day_start()
        t = datetime.time(hour, minute)
        if now.time() < t:
            date = today()
        else:
            d = datetime.datetime.today() + datetime.timedelta(days=1)
            date = d.strftime(('%Y-%m-%d'))

        self.set_downloads_today(date, value)

    def set_downloads_today(self, date: str, value: int=0) -> None:
        self.downloads_today = [date, str(value)]

    def set_day_start(self, hour: int, minute: int) -> None:
        self.day_start = "%s:%s" % (hour, minute)

    def log_vals(self) -> None:
        logging.info("Date %s Value %s Day start %s", self.downloads_today[0],
                     self.downloads_today[1], self.day_start)


def today():
    return datetime.date.today().strftime('%Y-%m-%d')

class Preferences:
    program_defaults = dict(program_version='')
    rename_defaults = dict(photo_download_folder=xdg_photos_directory(),
                           video_download_folder=xdg_videos_directory(),
                           photo_subfolder=DEFAULT_SUBFOLDER_PREFS,
                           video_subfolder=DEFAULT_VIDEO_SUBFOLDER_PREFS,
                           photo_rename=DEFAULT_PHOTO_RENAME_PREFS,
                           video_rename=DEFAULT_VIDEO_RENAME_PREFS,
                           # following two extension values introduced in 0.9.0a4:
                           photo_extension=LOWERCASE,
                           video_extension=LOWERCASE,
                           day_start="03:00",
                           downloads_today=[today(), '0'],
                           stored_sequence_no=0,
                           strip_characters=True,
                           synchronize_raw_jpg=False,
                           job_codes=[''],
                           remember_job_code=True,
                           ignore_mdatatime_for_mtp_dng=True,
                          )
    timeline_defaults = dict(proximity_seconds=3600)
    display_defaults = dict(detailed_time_remaining=False,
                            warn_downloading_all=True)
    device_defaults = dict(only_external_mounts=True,
                           device_autodetection=True,
                           this_computer_source = False,
                           this_computer_path='',
                           device_without_dcim_autodetection=False,
                           volume_whitelist=[''],
                           volume_blacklist=[''],
                           camera_blacklist=[''],
                           ignored_paths=['.Trash', '.thumbnails'],
                           use_re_ignored_paths=False
                          )
    backup_defaults = dict(backup_files=False,
                           backup_device_autodetection=True,
                           photo_backup_identifier=xdg_photos_identifier(),
                           video_backup_identifier=xdg_videos_identifier(),
                           backup_photo_location=os.path.expanduser('~'),
                           backup_video_location=os.path.expanduser('~'),
                          )
    automation_defaults = dict(auto_download_at_startup=False,
                               auto_download_upon_device_insertion=False,
                               auto_unmount=False,
                               auto_exit=False,
                               auto_exit_force=False,
                               move=False,
                               verify_file=False
                              )
    performance_defaults = dict(generate_thumbnails=True,
                                use_thumbnail_cache=True,
                                save_fdo_thumbnails=True,
                                max_cpu_cores=max(available_cpu_count(physical_only=True), 2)
                                )
    error_defaults = dict(conflict_resolution=int(constants.ConflictResolution.skip),
                          backup_duplicate_overwrite=False)


    def __init__(self) -> None:
        # To avoid infinite recursions arising from the use of __setattr__,
        # manually assign class values to the class dict
        self.__dict__['settings'] = QSettings("Rapid Photo Downloader", "Rapid Photo Downloader")
        self.__dict__['valid'] = True

        # These next two values must be kept in sync
        dicts = (self.program_defaults, self.rename_defaults,
                 self.timeline_defaults, self.display_defaults,
                 self.device_defaults,
                 self.backup_defaults, self.automation_defaults,
                 self.performance_defaults, self.error_defaults)
        group_names = ('Program', 'Rename', 'Timeline', 'Display', 'Device', 'Backup',
                       'Automation', 'Performance', 'ErrorHandling')
        assert len(dicts) == len(group_names)

        # Create quick lookup table for types of each value, including the
        # special case of lists, which use the type of what they contain.
        # While we're at it also merge the dictionaries into one dictionary
        # of default values.
        self.__dict__['types'] = {}
        self.__dict__['defaults'] = {}
        for d in dicts:
            for key, value in d.items():
                if isinstance(value, list):
                    t = type(value[0])
                else:
                    t = type(value)
                self.types[key] = t
                self.defaults[key] = value
        # Create quick lookup table of the group each key is in
        self.__dict__['groups'] = {}
        for idx, d in enumerate(dicts):
            for key in d:
                self.groups[key] = group_names[idx]

    def __getitem__(self, key):
        group = self.groups.get(key, 'General')
        self.settings.beginGroup(group)
        v = self.settings.value(key, self.defaults[key], self.types[key])
        self.settings.endGroup()
        return v

    def __getattr__(self, key):
        return self[key]

    def __setitem__(self, key, value):
        group = self.groups.get(key, 'General')
        self.settings.beginGroup(group)
        self.settings.setValue(key, value)
        self.settings.endGroup()

    def __setattr__(self, key, value):
        self[key] = value

    def sync(self):
        self.settings.sync()

    def get_preset(self, preset_type: constants.PresetPrefType) -> Tuple[List[str], List[str]]:
        """
        Returns the custom presets for the particular type.

        :param preset_type: one of photo subfolder, video subfolder, photo
         rename, or video rename
        :return: Tuple of list of present names and list of pref lists. Each
         item in the first list corresponds with the item of the same index in the
         second list.
        """

        preset_pref_lists = []
        preset_names = []

        self.settings.beginGroup('Presets')

        preset = preset_type.name
        size = self.settings.beginReadArray(preset)
        for i in range(size):
            self.settings.setArrayIndex(i)
            preset_names.append(self.settings.value('name', type=str))
            preset_pref_lists.append(self.settings.value('pref_list', type=str))
        self.settings.endArray()

        self.settings.endGroup()

        return preset_names, preset_pref_lists

    def set_preset(self, preset_type: constants.PresetPrefType,
                   preset_names: List[str],
                   preset_pref_lists: List[str]) -> None:
        """
        Saves a list of custom presets in the user's preferences.

        If the list of preset names is empty, the preference value will be cleared.

        :param preset_type: one of photo subfolder, video subfolder, photo
         rename, or video rename
        :param preset_names: list of names for each pref list
        :param preset_pref_lists: the list of pref lists
        """

        self.settings.beginGroup('Presets')

        preset = preset_type.name

        if not preset_names:
            self.settings.remove(preset)
        else:
            self.settings.beginWriteArray(preset)
            for i in range(len(preset_names)):
                self.settings.setArrayIndex(i)
                self.settings.setValue('name', preset_names[i])
                self.settings.setValue('pref_list', preset_pref_lists[i])
            self.settings.endArray()

        self.settings.endGroup()

    def get_proximity(self) -> int:
        """
        Validates preference value proxmity_seconds against standard list.

        Given the user could enter any old value into the preferences, need to validate it.
        The validation technique is to match whatever value is in the preferences with the
        closest value we need, which is found in the list of int proximity_time_steps.

        For the algorithm, see:
        http://stackoverflow.com/questions/12141150/from-list-of-integers-get-number-closest-to-a
        -given-value
        No need to use bisect list, as our list is tiny, and using min has the advantage
        of getting the closest value.

        Note: we store the value in seconds, but use it in minutes, just in case a user one day
        makes a compelling case to be able to specify a proximity value less than 1 minute.

        :return: closest valid value in minutes
        """

        minutes = self.proximity_seconds // 60
        return min(constants.proximity_time_steps, key=lambda x:abs(x - minutes))

    def set_proximity(self, minutes: int) -> None:
        self.proximity_seconds = minutes * 60

    def _pref_list_uses_component(self, pref_list, pref_component, offset: int=1) -> bool:
        for i in range(0, len(pref_list), 3):
            if pref_list[i+offset] == pref_component:
                return True
        return False

    def any_pref_uses_stored_sequence_no(self) -> bool:
        """
        :return True if any of the pref lists contain a stored sequence no
        """
        for pref_list in self.get_pref_lists():
            if self._pref_list_uses_component(pref_list, STORED_SEQ_NUMBER):
                return True
        return False

    def any_pref_uses_session_sequence_no(self) -> bool:
        """
        :return True if any of the pref lists contain a session sequence no
        """
        for pref_list in self.get_pref_lists():
            if self._pref_list_uses_component(pref_list, SESSION_SEQ_NUMBER):
                return True
        return False

    def any_pref_uses_sequence_letter_value(self) -> bool:
        """
        :return True if any of the pref lists contain a sequence letter
        """
        for pref_list in self.get_pref_lists():
            if self._pref_list_uses_component(pref_list, SEQUENCE_LETTER):
                return True
        return False

    def check_prefs_for_validity(self) -> Tuple[bool, str]:
        """
        Checks photo & video rename, and subfolder generation
        preferences ensure they follow name generation rules. Moreover,
        subfolder name specifications must not:
        1. start with a separator
        2. end with a separator
        3. have two separators in a row

        :return: tuple with two values: (1) bool and error message if
         prefs are invalid (else empy string)
        """

        msg = ''
        valid = True
        tests = ((self.photo_rename, DICT_IMAGE_RENAME_L0),
                 (self.video_rename, DICT_VIDEO_RENAME_L0),
                 (self.photo_subfolder, DICT_SUBFOLDER_L0),
                 (self.video_subfolder, DICT_VIDEO_SUBFOLDER_L0))

        # test file renaming
        for pref, pref_defn in tests[:2]:
            try:
                check_pref_valid(pref_defn, pref)
            except PrefError as e:
                valid = False
                msg += e.msg + "\n"

        # test subfolder generation
        for pref, pref_defn in tests[2:]:
            try:
                check_pref_valid(pref_defn, pref)

                L1s = [pref[i] for i in range(0, len(pref), 3)]

                if L1s[0] == SEPARATOR:
                    raise PrefValueKeyComboError(_(
                        "Subfolder preferences should not start with a %s") % os.sep)
                elif L1s[-1] == SEPARATOR:
                    raise PrefValueKeyComboError(_(
                        "Subfolder preferences should not end with a %s") % os.sep)
                else:
                    for i in range(len(L1s) - 1):
                        if L1s[i] == SEPARATOR and L1s[i + 1] == SEPARATOR:
                            raise PrefValueKeyComboError(_(
                                "Subfolder preferences should not contain "
                                "two %s one after the other") % os.sep)

            except PrefError as e:
                valid = False
                msg += e.msg + "\n"

        return (valid, msg)

    def must_synchronize_raw_jpg(self) -> bool:
        """
        :return: True if synchronize_raw_jpg is True and photo
        renaming uses sequence values
        """
        if self.synchronize_raw_jpg:
            for s in LIST_SEQUENCE_L1:
                if self._pref_list_uses_component(self.photo_rename, s, 1):
                    return True
        return False

    def format_pref_list_for_pretty_print(self, pref_list) -> str:
        """
        :return: string useful for printing the preferences
        """

        v = ''
        for i in range(0, len(pref_list), 3):
            if (pref_list[i+1] or pref_list[i+2]):
                c = ':'
            else:
                c = ''
            s = "%s%s " % (pref_list[i], c)

            if pref_list[i+1]:
                s = "%s%s" % (s, pref_list[i+1])
            if pref_list[i+2]:
                s = "%s (%s)" % (s, pref_list[i+2])
            v += s + "\n"
        return v

    def get_pref_lists(self) -> Tuple[List[str], List[str], List[str], List[str]]:
        """
        :return: a tuple of the photo & video rename and subfolder
         generation preferences
        """
        return (self.photo_rename, self.photo_subfolder, self.video_rename, self.video_subfolder)

    def pref_uses_job_code(self, pref_list: List[str]):
        """ Returns True if the particular preferences contains a job code"""
        for i in range(0, len(pref_list), 3):
            if pref_list[i] == JOB_CODE:
                return True
        return False

    def any_pref_uses_job_code(self) -> bool:
        """ Returns True if any of the preferences contain a job code"""
        for pref_list in self.get_pref_lists():
            if self.pref_uses_job_code(pref_list):
                return True
        return False

    def most_recent_job_code(self, missing: Optional[str]=None) -> str:
        """
        Get the most recent Job Code used (which is assumed to be at the top).
        :param missing: If there is no Job Code, and return this default value
        :return: most recent job code, or missing, or if not found, ''
        """

        if len(self.job_codes) > 0:
            value = self.job_codes[0]
            return value or missing or ''
        elif missing is not None:
            return missing
        else:
            return ''

    def photo_subfolder_index(self, preset_pref_lists: List[List[str]]) -> int:
        """
        Matches the photo pref list with program subfolder generation
        defaults and the user's presets.

        :return: -1 if no match (i.e. custom), or the index into
         PHOTO_SUBFOLDER_MENU_DEFAULTS + photo subfolder presets if it matches
        """

        subfolders = PHOTO_SUBFOLDER_MENU_DEFAULTS_CONV + tuple(preset_pref_lists)
        try:
            return subfolders.index(self.photo_subfolder)
        except ValueError:
            return -1

    def video_subfolder_index(self, preset_pref_lists: List[List[str]]) -> int:
        """
        Matches the photo pref list with program subfolder generation
        defaults and the user's presets.

        :return: -1 if no match (i.e. custom), or the index into
         VIDEO_SUBFOLDER_MENU_DEFAULTS + video subfolder presets if it matches
        """

        subfolders = VIDEO_SUBFOLDER_MENU_DEFAULTS_CONV + tuple(preset_pref_lists)
        try:
            return subfolders.index(self.video_subfolder)
        except ValueError:
            return -1

    def add_list_value(self, key, value) -> None:
        """
        Add value to pref list if it doesn't already exist.

        An empty list contains only one item: ['']

        :param key: the preference key
        :param value: the value to add
        """

        if len(self[key]) == 1 and self[key][0] == '':
            self[key] = [value]
        elif value not in self[key]:
            # Must assign the value like this, otherwise the preference value
            # will not be updated:
            self[key] = self[key] + [value]

    def del_list_value(self, key, value) -> None:
        """
        Remove a value from the pref list indicated by key.

        Exceptions are not caught.

        An empty list contains only one item: ['']

        :param key: the preference key
        :param value: the value to add
        """

        # Must remove the value like this, otherwise the preference value
        # will not be updated:
        l = self[key]
        l.remove(value)
        self[key] = l

        if len(self[key]) == 0:
            self[key] = ['']

    def reset(self) -> None:
        """
        Reset all program preferences to their default settings
        """
        self.settings.clear()
        self.program_version = raphodo.__about__.__version__

    def upgrade_prefs(self, previous_version) -> None:
        """
        Upgrade the user's preferences if needed.

        :param previous_version: previous version as returned by pkg_resources.parse_version
        """

        photo_video_rename_change = pkg_resources.parse_version('0.9.0a4')
        if previous_version < photo_video_rename_change:
            for key in ('photo_rename', 'video_rename'):
                pref_list, case = upgrade_pre090a4_rename_pref(self[key])
                if pref_list != self[key]:
                    self[key] = pref_list
                    logging.info("Upgraded %s preference value", key.replace('_', ' '))
                if case is not None:
                    if key == 'photo_rename':
                        self.photo_extension = case
                    else:
                        self.video_extension = case

        v090a5 = pkg_resources.parse_version('0.9.0a5')
        if previous_version < v090a5:
            # Versions prior to 0.9.0a5 incorrectly set the conflict resolution value
            # when importing preferences from 0.4.11 or earlier
            try:
                value = self.conflict_resolution
            except TypeError:
                self.settings.endGroup()
                default = self.defaults['conflict_resolution']
                default_name = constants.ConflictResolution(default).name
                logging.warning('Resetting Conflict Resolution preference value to %s',
                                default_name)
                self.conflict_resolution = default
            # destinationButtonPressed is no longer used by 0.9.0a5
            self.settings.beginGroup("MainWindow")
            key = 'destinationButtonPressed'
            try:
                if self.settings.contains(key):
                    logging.debug("Removing preference value %s", key)
                    self.settings.remove(key)
            except:
                logging.warning("Unknown error removing %s preference value", key)
            self.settings.endGroup()


def match_pref_list(pref_lists: List[List[str]], user_pref_list: List[str]) -> int:
    try:
        return pref_lists.index(user_pref_list)
    except ValueError:
        return -1