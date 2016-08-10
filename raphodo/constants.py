# Copyright (C) 2007-2015 Damon Lynch <damonlynch@gmail.com>

# This file is part of Rapid Photo Downloader.
#
# Rapid Photo Downloader is free software: you can redistribute it and/or
# modify
# it under the terms of the GNU General Public License as published by
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
__copyright__ = "Copyright 2007-2016, Damon Lynch"

from enum import (Enum, IntEnum)
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QFont, QFontMetrics, QColor

PROGRAM_NAME = "Rapid Photo Downloader"
logfile_name = 'rapid-photo-downloader.log'


class ConflictResolution(IntEnum):
    skip = 1
    add_identifier = 2


class ErrorType(Enum):
    critical_error = 1
    serious_error = 2
    warning = 3


class PresetPrefType(Enum):
    preset_photo_subfolder = 1
    preset_video_subfolder = 2
    preset_photo_rename = 3
    preset_video_rename = 4


class PresetClass(Enum):
    builtin = 1
    custom = 2
    new_preset = 3
    remove_all = 4
    update_preset = 5
    edited = 6


class DownloadStatus(Enum):
    # going to try to download it
    download_pending = 1

    # downloaded successfully
    downloaded = 2

    # downloaded ok but there was a warning
    downloaded_with_warning = 3

    # downloaded ok, but the file was not backed up, or had a problem
    # (overwrite or duplicate)
    backup_problem = 4

    # has not yet been downloaded (but might be if the user chooses)
    not_downloaded = 5

    # tried to download but failed, and the backup failed or had an error
    download_and_backup_failed = 6

    # tried to download but failed
    download_failed = 7


Downloaded = (DownloadStatus.downloaded,
              DownloadStatus.downloaded_with_warning,
              DownloadStatus.backup_problem)

DownloadWarning = {DownloadStatus.downloaded_with_warning, DownloadStatus.backup_problem}
DownloadFailure = {DownloadStatus.download_and_backup_failed, DownloadStatus.download_failed}

DownloadUpdateMilliseconds = 1000
DownloadUpdateSeconds = DownloadUpdateMilliseconds / 1000
# How many seconds to delay showing the time remaining and download speed
ShowTimeAndSpeedDelay = 8.0


class ThumbnailCacheStatus(Enum):
    not_ready = 1
    orientation_unknown = 2
    ready = 3
    fdo_256_ready = 4
    generation_failed = 5


class ThumbnailCacheDiskStatus(Enum):
    found = 1
    not_found = 2
    failure = 3
    unknown = 4


class ThumbnailCacheOrigin(Enum):
    thumbnail_cache = 1
    fdo_cache = 2


class BackupLocationType(Enum):
    photos = 1
    videos = 2
    photos_and_videos = 3


class DestinationDisplayType(Enum):
    folder_only = 1
    usage_only = 2
    folders_and_usage = 3


class DestinationDisplayMousePos(Enum):
    normal = 1
    menu = 2

class DestinationDisplayTooltipState(Enum):
    menu = 1
    path = 2
    storage_space = 3

class DisplayingFilesOfType(Enum):
    photos = 1
    videos = 2
    photos_and_videos = 3


class DeviceType(Enum):
    camera = 1
    volume = 2
    path = 3


class DeviceState(Enum):
    pre_scan = 1
    scanning = 2
    idle = 3
    thumbnailing = 4
    downloading = 5
    finished = 6


class FileType(IntEnum):
    photo = 1
    video = 2


class FileExtension(Enum):
    raw = 1
    jpeg = 2
    other_photo = 3
    video = 4
    audio = 5
    unknown = 6


class FileSortPriority(IntEnum):
    high = 1
    low = 2


class RenameAndMoveStatus(Enum):
    download_started = 1
    download_completed = 2


class ThumbnailSize(IntEnum):
    width = 160
    height = 120


class ApplicationState(Enum):
    normal = 1
    exiting = 2


class Show(IntEnum):
    all = 1
    new_only = 2


class Sort(IntEnum):
    modification_time = 1
    checked_state = 2
    filename = 3
    extension = 4
    file_type = 5
    device = 6


Checked_Status = {
    Qt.Checked: 'checked',
    Qt.Unchecked: 'unchecked',
    Qt.PartiallyChecked: 'partially checked'
}


class Roles(IntEnum):
    previously_downloaded = Qt.UserRole
    extension = Qt.UserRole + 1
    download_status = Qt.UserRole + 2
    has_audio = Qt.UserRole + 3
    secondary_attribute = Qt.UserRole + 4
    path = Qt.UserRole + 5
    uri = Qt.UserRole + 6
    camera_memory_card = Qt.UserRole + 7
    scan_id = Qt.UserRole + 8
    device_details = Qt.UserRole + 9
    storage = Qt.UserRole + 10
    mtp = Qt.UserRole + 11
    is_camera = Qt.UserRole + 12
    sort_extension = Qt.UserRole + 13
    filename = Qt.UserRole + 14
    highlight = Qt.UserRole + 16
    folder_preview = Qt.UserRole + 17
    download_subfolder = Qt.UserRole + 18
    device_type = Qt.UserRole + 19
    download_statuses = Qt.UserRole + 20


class ExtractionTask(Enum):
    undetermined = 1
    bypass = 2
    load_file_directly = 3
    load_file_and_exif_directly = 4
    load_file_directly_metadata_from_secondary = 5
    load_from_bytes = 6
    load_from_bytes_metadata_from_temp_extract = 7
    load_from_exif = 8
    extract_from_file = 9
    extract_from_file_and_load_metadata = 10
    load_from_exif_buffer = 11


class ExtractionProcessing(Enum):
    resize = 1
    orient = 2
    strip_bars_photo = 3
    strip_bars_video = 4
    add_film_strip = 5


# Approach device uses to store timestamps
# i.e. whether assumes are located in utc timezone or local
class DeviceTimestampTZ(Enum):
    undetermined = 1
    unknown = 2
    is_utc = 3
    is_local = 4


class CameraErrorCode(Enum):
    inaccessible = 1
    locked = 2


class ViewRowType(Enum):
    header = 1
    content = 2


class Align(Enum):
    top = 1
    bottom = 2


class NameGenerationType(Enum):
    photo_name = 1
    video_name = 2
    photo_subfolder = 3
    video_subfolder = 4


class CustomColors(Enum):
    color1 = '#7a9c38'  # green
    color2 = '#cb493f'  # red
    color3 = '#d17109'  # orange
    color4 = '#4D8CDC'  # blue
    color5 = '#5f6bfe'  # purple
    color6 = '#6d7e90'  # greyish
    color7 = '#ffff00'  # bright yellow


PaleGray = '#d7d6d5'
DarkGray = '#35322f'
MediumGray = '#5d5b59'
DoubleDarkGray = '#1e1b18'

ExtensionColorDict = {
    FileExtension.raw: CustomColors.color1,
    FileExtension.video: CustomColors.color2,
    FileExtension.jpeg: CustomColors.color4,
    FileExtension.other_photo: CustomColors.color5,
}


def extensionColor(ext_type: FileExtension) -> QColor:
    try:
        return QColor(ExtensionColorDict[ext_type].value)
    except KeyError:
        return QColor(0, 0, 0)


FileTypeColorDict = {
    FileType.photo: CustomColors.color1,
    FileType.video: CustomColors.color2
}


def fileTypeColor(file_type: FileType) -> QColor:
    try:
        return QColor(FileTypeColorDict[file_type].value)
    except KeyError:
        return QColor(CustomColors.color3.value)


# Position of preference values in file renaming and subfolder generation editor:
class PrefPosition(Enum):
    on_left = 1
    at = 2
    on_left_and_at = 3
    positioned_in = 4
    not_here = 5

# Values in minutes:
proximity_time_steps = [5, 10, 15, 30, 45, 60, 90, 120, 180, 240, 480, 960, 1440]


class TemporalProximityState(Enum):
    empty = 1
    pending = 2
    generating = 3
    regenerate = 4
    generated = 5
    ctime_rebuild = 6
    ctime_rebuild_proceed = 7


ThumbnailBackgroundName = MediumGray
EmptyViewHeight = 20

DeviceDisplayPadding = 6
DeviceShadingIntensity = 104

# How many steps with which to highlight thumbnail cells
FadeSteps = 20
FadeMilliseconds = 700


def minPanelWidth() -> int:
    """
    Minimum width of panels on left and right side of main window.

    Derived from standard font size.

    :return: size in pixels
    """

    return int(QFontMetrics(QFont()).height() * 13.5)


def minFileSystemViewHeight() -> int:
    """
    Minimum height of file system views on left and right side of main window.

    Derived from standard font size.

    :return: size in pixels
    """

    return QFontMetrics(QFont()).height() * 7


class Desktop(Enum):
    gnome = 1
    unity = 2
    cinnamon = 3
    kde = 4
    xfce = 5
    mate = 6
    lxde = 7
    unknown = 10


orientation_offset = dict(
    arw=106,
    cr2=126,
    dcr=7684,
    dng=144,
    mef=144,
    mrw=152580,
    nef=144,
    nrw=94,
    orf=132,
    pef=118,
    raf=208,
    raw=742404,
    rw2=1004548,
    sr2=82,
    srw=46
)

datetime_offset = dict(
    arw=1540,
    cr2=1028,
    dng=119812,
    mef=772,
    mrw=152580,
    nef=14340,
    nrw=1540,
    orf=6660,
    pef=836,
    raf=1796,
    raw=964,
    rw2=3844,
    sr2=836,
    srw=508,
    mts=5000,
    mp4=50000,
    avi=50000,
    mov=250000,
)
datetime_offset['3gp'] = 5000

thumbnail_offset = dict(
    jpg=100000,
    jpeg=100000,
    dng=100000,
    avi=500000,
    mod=500000,
    mov=2000000,
    mp4=2000000,
    mts=600000,
    m2t=600000,
    mpg=500000,
    mpeg=500000,
    tod=500000,
)


class RememberThisMessage(Enum):
    remember_choice = 1
    do_not_ask_again = 2