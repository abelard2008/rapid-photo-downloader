# Copyright (C) 2015-2016 Damon Lynch <damonlynch@gmail.com>

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
__copyright__ = "Copyright 2015-2016, Damon Lynch"

import pickle
import os
import sys
import datetime
from collections import (namedtuple, defaultdict, deque)
from operator import attrgetter
import subprocess
import shlex
import logging
from timeit import timeit
from typing import Optional, Dict, List, Set, Tuple, Sequence

from gettext import gettext as _

import arrow.arrow
from dateutil.tz import tzlocal
from colour import Color

from PyQt5.QtCore import (QAbstractListModel, QModelIndex, Qt, pyqtSignal, QSize, QRect, QEvent,
                          QPoint, QMargins, QSortFilterProxyModel, QItemSelectionModel,
                          QAbstractItemModel, pyqtSlot, QItemSelection, QTimeLine)
from PyQt5.QtWidgets import (QListView, QStyledItemDelegate, QStyleOptionViewItem, QApplication,
                             QStyle, QStyleOptionButton, QMenu, QWidget, QAbstractItemView)
from PyQt5.QtGui import (QPixmap, QImage, QPainter, QColor, QBrush, QFontMetrics,
                         QGuiApplication, QPen, QMouseEvent, QFont)

from raphodo.rpdfile import RPDFile, FileTypeCounter
from raphodo.interprocess import (PublishPullPipelineManager, GenerateThumbnailsArguments, Device,
                          GenerateThumbnailsResults)
from raphodo.constants import (DownloadStatus, Downloaded, FileType, FileExtension, ThumbnailSize,
                               ThumbnailCacheStatus, Roles, DeviceType, CustomColors, Show, Sort,
                               ThumbnailBackgroundName, Desktop, DeviceState, extensionColor,
                               FadeSteps, FadeMilliseconds, PaleGray, DarkGray)
from raphodo.storage import get_program_cache_directory, get_desktop
from raphodo.utilities import (CacheDirs, make_internationalized_list, format_size_for_user, runs)
from raphodo.thumbnailer import Thumbnailer
from raphodo.rpdsql import ThumbnailRowsSQL, ThumbnailRow
from raphodo.viewutils import ThumbnailDataForProximity


class DownloadTypes:
    def __init__(self):
        self.photos = False
        self.videos = False


DownloadFiles = namedtuple('DownloadFiles', ['files', 'download_types',
                                             'download_stats',
                                             'camera_access_needed'])


class DownloadStats:
    def __init__(self):
        self.no_photos = 0
        self.no_videos = 0
        self.photos_size_in_bytes = 0
        self.videos_size_in_bytes = 0
        self.post_download_thumb_generation = 0


class ThumbnailManager(PublishPullPipelineManager):
    message = pyqtSignal(RPDFile, QPixmap)
    cacheDirs = pyqtSignal(int, CacheDirs)

    def __init__(self, logging_port: int) -> None:
        super().__init__(logging_port=logging_port)
        self._process_name = 'Thumbnail Manager'
        self._process_to_run = 'thumbnail.py'
        self._worker_id = 0

    def process_sink_data(self) -> None:
        data = pickle.loads(self.content) # type: GenerateThumbnailsResults
        if data.rpd_file is not None:
            thumbnail = QImage.fromData(data.thumbnail_bytes)
            thumbnail = QPixmap.fromImage(thumbnail)
            self.message.emit(data.rpd_file, thumbnail)
        else:
            assert data.cache_dirs is not None
            self.cacheDirs.emit(data.scan_id, data.cache_dirs)

    def get_worker_id(self) -> int:
        self._worker_id += 1
        return self._worker_id


class ThumbnailListModel(QAbstractListModel):
    def __init__(self, parent, logging_port: int, log_gphoto2: bool) -> None:
        super().__init__(parent)
        self.rapidApp = parent

        self.thumbnailer_ready = False
        self.thumbnailer_generation_queue = []

        # Sorting and filtering GUI defaults
        self.sort_by = Sort.modification_time
        self.sort_order = Qt.AscendingOrder
        self.show = Show.all

        self.initialize()

        no_workers = parent.prefs.max_cpu_cores
        self.thumbnailmq = Thumbnailer(parent=parent, no_workers=no_workers,
               logging_port=logging_port, log_gphoto2=log_gphoto2)
        self.thumbnailmq.ready.connect(self.thumbnailerReady)
        self.thumbnailmq.thumbnailReceived.connect(self.thumbnailReceived)

        self.thumbnailmq.cacheDirs.connect(self.cacheDirsReceived)

        # dict of scan_pids that are having thumbnails generated
        # value is the thumbnail process id
        # this is needed when terminating thumbnailing early such as when
        # user clicks download before the thumbnailing is finished
        self.generating_thumbnails = {}

    def initialize(self) -> None:
        # uid: QPixmap
        self.thumbnails = {}  # type: Dict[bytes, QPixmap]

        self.add_buffer = deque()
        self.buffer_length = 10

        # Proximity filtering
        self.proximity_col1 = []  #  type: List[int, ...]
        self.proximity_col2 = []  #  type: List[int, ...]

        # scan_id
        self.removed_devices = set()  # type: Set[int]

        # Files are hidden when the combo box "Show" in the main window is set to
        # "New" instead of the default "All".

        # uid: RPDFile
        self.rpd_files = {}  # type: Dict[bytes, RPDFile]

        # In memory database to hold all thumbnail rows
        self.tsql = ThumbnailRowsSQL()

        # Rows used to render the thumbnail view - contains query result of the DB
        # Each list element corresponds to a row in the thumbnail view such that
        # index 0 in the list is row 0 in the view
        # [(uid, marked)]
        self.rows = []  # type: List[Tuple[bytes, bool]]
        # {uid: row}
        self.uid_to_row = {}  # type: Dict[bytes, int]

        self.photo_icon = QPixmap(':/photo.png')
        self.video_icon = QPixmap(':/video.png')

        self.total_thumbs_to_generate = 0
        self.thumbnails_generated = 0
        self.no_thumbnails_by_scan = defaultdict(int)

        # Highlight thumbnails when from particular device when there is more than one device
        # Thumbnails to highlight by uid
        self.currently_highlighting_scan_id = None  # type: Optional[int]
        self._resetHighlightingValues()
        self.highlighting_timeline = QTimeLine(FadeMilliseconds // 2)
        self.highlighting_timeline.setCurveShape(QTimeLine.SineCurve)
        self.highlighting_timeline.frameChanged.connect(self.doHighlightDeviceThumbs)
        self.highlighting_timeline.finished.connect(self.highlightPhaseFinished)
        self.highlighting_timeline_max = FadeSteps
        self.highlighting_timeline_mint = 0
        self.highlighting_timeline.setFrameRange(self.highlighting_timeline_mint,
                                                 self.highlighting_timeline_max)
        self.highlight_value = 0

        self._resetRememberSelection()

    def logState(self) -> None:
        logging.debug("-- Thumbnail Model --")
        if not self.thumbnailer_ready:
            logging.debug("Thumbnailer not yet ready")
        else:
            db_length = self.tsql.get_count()
            if len(self.thumbnails) != db_length or db_length != len(self.rpd_files):
                logging.error("Conflicting values: %s thumbnails; %s database rows; %s rpd_files",
                              len(self.thumbnails), db_length, len(self.rpd_files))
            else:
                logging.debug("%s thumbnails (%s marked)",
                              db_length, self.tsql.get_count(marked=True))

            logging.debug("%s not downloaded; %s downloaded; %s previously downloaded",
                          self.tsql.get_count(downloaded=False),
                          self.tsql.get_count(downloaded=True),
                          self.tsql.get_count(previously_downloaded=True))

            if self.total_thumbs_to_generate:
                logging.debug("%s to be generated; %s generated", self.total_thumbs_to_generate,
                              self.thumbnails_generated)

            scan_ids = self.tsql.get_all_devices()
            active_devices = ', '.join(self.rapidApp.devices[scan_id].display_name
                                       for scan_id in scan_ids
                                       if scan_id not in self.removed_devices)
            if len(self.removed_devices):
                logging.debug("Active devices: %s (%s removed)",
                              active_devices, len(self.removed_devices))
            else:
                logging.debug("Active devices: %s", active_devices)
            if len(scan_ids) != len(self.rapidApp.devices):
                logging.error("Conflicting number of devices: %s devices in database, and %s "
                              "devices in rapidApp devices",
                              len(scan_ids), len(self.rapidApp.devices))

    def validateModelConsistency(self):
        logging.debug("Validating thumbnail model consistency...")

        for idx, row in enumerate(self.rows):
            uid = row[0]
            if self.rpd_files.get(uid) is None:
                raise KeyError('Missing key in rpd files at row {}'.format(idx))
            if self.thumbnails.get(uid) is None:
                raise KeyError('Missing key in thumbnails at row {}'.format(idx))

        [self.tsql.validate_uid(uid=row[0]) for row in self.rows]
        for uid, row in self.uid_to_row.items():
            assert self.rows[row][0] == uid
        for uid in self.tsql.get_uids():
            assert uid in self.rpd_files
            assert uid in self.thumbnails
        logging.debug("...thumbnail model looks okay")

    def refresh(self, suppress_signal=False, rememberSelection=False):

        if not suppress_signal:
            self.layoutAboutToBeChanged.emit()

        if rememberSelection:
            self.rememberSelection()

        self.rows = self.tsql.get_view(sort_by=self.sort_by, sort_order=self.sort_order,
                                       show=self.show, proximity_col1=self.proximity_col1,
                                       proximity_col2=self.proximity_col2)
        self.uid_to_row = {row[0]: idx for idx, row in enumerate(self.rows)}

        if not suppress_signal:
            self.layoutChanged.emit()

        if rememberSelection:
            self.reselect()

    def rowCount(self, parent: QModelIndex=QModelIndex()) -> int:
        return len(self.rows)

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid():
            return None

        row = index.row()
        if row >= len(self.rows) or row < 0:
            return None

        uid = self.rows[row][0]
        rpd_file = self.rpd_files[uid]  # type: RPDFile

        if role == Qt.DisplayRole:
            # This is never displayed, but is used for filtering!
            return rpd_file.modification_time
        elif role == Roles.highlight:
            if rpd_file.scan_id == self.currently_highlighting_scan_id:
                return self.highlight_value
            else:
                return 0
        elif role == Qt.DecorationRole:
            return self.thumbnails[uid]
        elif role == Qt.CheckStateRole:
            if self.rows[row][1]:
                return Qt.Checked
            else:
                return Qt.Unchecked
        elif role == Roles.sort_extension:
            return rpd_file.extension
        elif role == Roles.filename:
            return rpd_file.name
        elif role == Roles.previously_downloaded:
            return rpd_file.previously_downloaded()
        elif role == Roles.extension:
            return rpd_file.extension, rpd_file.extension_type
        elif role == Roles.download_status:
            return rpd_file.status
        elif role == Roles.has_audio:
            return rpd_file.has_audio()
        elif role == Roles.secondary_attribute:
            if rpd_file.xmp_file_full_name:
                return 'XMP'
            else:
                return None
        elif role== Roles.path:
            if rpd_file.status in Downloaded:
                return rpd_file.download_full_file_name
            else:
                return rpd_file.full_file_name
        elif role == Roles.uri:
            return rpd_file.get_uri(desktop_environment=True)
        elif role == Roles.camera_memory_card:
            return rpd_file.camera_memory_card_identifiers
        elif role == Roles.mtp:
            return rpd_file.is_mtp_device
        elif role == Roles.scan_id:
            return rpd_file.scan_id
        elif role == Roles.is_camera:
            return rpd_file.from_camera
        elif role == Qt.ToolTipRole:
            devices = self.rapidApp.devices
            if len(devices) > 1:
                device_name = devices[rpd_file.scan_id].display_name
            else:
                device_name = ''
            size = format_size_for_user(rpd_file.size)

            mtime = arrow.get(rpd_file.modification_time)
            humanized_modification_time = _(
                '%(date_time)s (%(human_readable)s)' %
                {'date_time': mtime.to('local').naive.strftime(
                    '%c'),
                 'human_readable': mtime.humanize()})

            if not device_name:
                msg = '{}\n{}\n{}'.format(rpd_file.name,
                                      humanized_modification_time, size)
            else:
                msg = '{}\n{}\n{}\n{}'.format(rpd_file.name, device_name,
                                          humanized_modification_time, size)

            if rpd_file.camera_memory_card_identifiers:
                cards = _('Memory cards: %s') % make_internationalized_list(
                    rpd_file.camera_memory_card_identifiers)
                msg += '\n' + cards

            if rpd_file.status in Downloaded:
                path = rpd_file.download_path + os.sep
                msg += '\n\nDownloaded as:\n%(filename)s\n%(path)s' % {
                    'filename': rpd_file.download_name,
                    'path': path}

            if rpd_file.previously_downloaded():

                prev_datetime = arrow.get(rpd_file.prev_datetime,
                                          tzlocal())
                prev_date = _('%(date_time)s (%(human_readable)s)' %
                {'date_time': prev_datetime.naive.strftime(
                    '%c'),
                 'human_readable': prev_datetime.humanize()})

                path, prev_file_name = os.path.split(rpd_file.prev_full_name)
                path += os.sep
                msg += _('\n\nPrevious download:\n%(filename)s\n%(path)s\n%('
                         'date)s') % {'date': prev_date,
                                       'filename': prev_file_name,
                                       'path': path}
            return msg

    def setData(self, index: QModelIndex, value, role: int) -> bool:
        if not index.isValid():
            return False

        row = index.row()
        if row >= len(self.rows) or row < 0:
            return False
        uid = self.rows[row][0]
        if role == Qt.CheckStateRole:
            self.tsql.set_marked(uid=uid, marked=value)
            self.rows[row] = (uid, value == True)
            self.dataChanged.emit(index, index)
            return True
        return False

    def updateDisplayPostDataChange(self, scan_id: Optional[int]=None):
        if scan_id is not None:
            scan_ids = [scan_id]
        else:
            scan_ids = (scan_id for scan_id in self.rapidApp.devices)
        for scan_id in scan_ids:
            self.updateDeviceDisplayCheckMark(scan_id=scan_id)
        self.rapidApp.displayMessageInStatusBar()
        self.rapidApp.setDownloadActionState()

    def removeRows(self, position, rows=1, index=QModelIndex()):
        """
        Removes Python list rows only, i.e. self.rows.

        Does not touch database or other variables.
        """

        self.beginRemoveRows(QModelIndex(), position, position + rows - 1)
        del self.rows[position:position + rows]
        self.endRemoveRows()
        return True

    def addOrUpdateDevice(self, scan_id: int) -> None:
        device_name = self.rapidApp.devices[scan_id].display_name
        self.tsql.add_or_update_device(scan_id=scan_id, device_name=device_name)

    def addFiles(self, rpd_files: List[RPDFile], generate_thumbnail: bool):
        if not rpd_files:
            return

        thumbnail_rows = deque(maxlen=len(rpd_files))


        for rpd_file in rpd_files:
            uid = rpd_file.uid
            self.rpd_files[uid] = rpd_file

            if rpd_file.file_type == FileType.photo:
                self.thumbnails[uid] = self.photo_icon
            else:
                self.thumbnails[uid] = self.video_icon

            if generate_thumbnail:
                self.total_thumbs_to_generate += 1
                self.no_thumbnails_by_scan[rpd_file.scan_id] += 1

            tr = ThumbnailRow(uid=uid,
                              scan_id=rpd_file.scan_id,
                              mtime=rpd_file.modification_time,
                              marked=not rpd_file.previously_downloaded(),
                              file_name=rpd_file.name,
                              extension=rpd_file.extension,
                              file_type=rpd_file.file_type,
                              downloaded=False,
                              previously_downloaded=rpd_file.previously_downloaded(),
                              proximity_col1=-1,
                              proximity_col2=-1)

            thumbnail_rows.append(tr)

        self.add_buffer.extend(thumbnail_rows)

        if len(self.add_buffer) > self.buffer_length:
            self.flushAddBuffer()

    def flushAddBuffer(self):
        if self.add_buffer:
            self.beginResetModel()

            self.tsql.add_thumbnail_rows(thumbnail_rows=self.add_buffer)
            self.refresh(suppress_signal=True)

            self.add_buffer = deque()
            self.buffer_length = len(self.rows)

            self.endResetModel()

            self._resetHighlightingValues()
            self._resetRememberSelection()

    def setFileSort(self, sort: Sort, order: Qt.SortOrder, show: Show) -> None:
        if self.sort_by != sort or self.sort_order != order or self.show != show:
            logging.debug("Resetting view due to sort/filter change: %s, %s, %s", sort, order, show)
            self.sort_by = sort
            self.sort_order = order
            self.show = show
            self.refresh(rememberSelection=True)

    def rememberSelection(self):
        selection = self.rapidApp.thumbnailView.selectionModel()  # type: QItemSelectionModel
        selected = selection.selection()  # type: QItemSelection
        self.remember_selection_all_selected = len(selected) == len(self.rows)
        if not self.remember_selection_all_selected:
            self.remember_selection_selected_uids = [self.rows[index.row()][0]
                                                     for index in selected.indexes()]
            selection.reset()

    def reselect(self):
        if not self.remember_selection_all_selected:
            selection = self.rapidApp.thumbnailView.selectionModel()  # type: QItemSelectionModel
            new_selection = QItemSelection()  # type: QItemSelection
            rows = [self.uid_to_row[uid] for uid in self.remember_selection_selected_uids]
            rows.sort()
            for first, last in runs(rows):
                new_selection.select(self.index(first, 0), self.index(last, 0))
            selection.reset()
            selection.select(new_selection, QItemSelectionModel.Select)

            for first, last in runs(rows):
                self.dataChanged.emit(self.index(first, 0), self.index(last, 0))

    def _resetRememberSelection(self):
        self.remember_selection_all_selected = None  # type: Optional[bool]
        self.remember_selection_selected_uids = []  # type: List[bytes]

    @pyqtSlot(int, CacheDirs)
    def cacheDirsReceived(self, scan_id: int, cache_dirs: CacheDirs):
        if scan_id in self.rapidApp.devices:
            self.rapidApp.devices[scan_id].photo_cache_dir = cache_dirs.photo_cache_dir
            self.rapidApp.devices[scan_id].video_cache_dir = cache_dirs.video_cache_dir

    @pyqtSlot(RPDFile, QPixmap)
    def thumbnailReceived(self, rpd_file: RPDFile, thumbnail: Optional[QPixmap]) -> None:
        uid = rpd_file.uid
        if uid not in self.rpd_files:
            # A thumbnail has been generated for a no longer displayed file
            return
        scan_id = rpd_file.scan_id
        self.rpd_files[uid] = rpd_file
        if not thumbnail.isNull():
            try:
                row = self.uid_to_row[uid]
            except ValueError:
                return
            self.thumbnails[uid] = thumbnail
            self.dataChanged.emit(self.index(row,0),self.index(row,0))
        self.thumbnails_generated += 1
        self.no_thumbnails_by_scan[scan_id] -= 1
        log_state = False
        if self.no_thumbnails_by_scan[scan_id] == 0:
            if self.rapidApp.deviceState(scan_id) == DeviceState.thumbnailing:
                self.rapidApp.devices.set_device_state(scan_id, DeviceState.idle)
            device = self.rapidApp.devices[scan_id]
            logging.info('Finished thumbnail generation for %s', device.name())
            self.rapidApp.updateProgressBarState()
            log_state = True

        if self.thumbnails_generated == self.total_thumbs_to_generate:
            self.resetThumbnailTrackingAndDisplay()
        elif self.total_thumbs_to_generate:
            self.rapidApp.downloadProgressBar.setValue(self.thumbnails_generated)

        if log_state:
            self.logState()

    def _get_cache_location(self, download_folder: str, is_photo_dir: bool) -> str:
        if self.rapidApp.isValidDownloadDir(download_folder, is_photo_dir=is_photo_dir):
            return download_folder
        else:
            folder = get_program_cache_directory(create_if_not_exist=True)
            if folder is not None:
                return folder
            else:
                return os.path.expanduser('~')

    def getCacheLocations(self) -> CacheDirs:
        photo_cache_folder = self._get_cache_location(
            self.rapidApp.prefs.photo_download_folder, is_photo_dir=True)
        video_cache_folder = self._get_cache_location(
            self.rapidApp.prefs.video_download_folder, is_photo_dir=False)
        return CacheDirs(photo_cache_folder, video_cache_folder)

    @pyqtSlot()
    def thumbnailerReady(self) -> None:
        self.thumbnailer_ready = True
        if self.thumbnailer_generation_queue:
            for gen_args in self.thumbnailer_generation_queue:
                self.thumbnailmq.generateThumbnails(*gen_args)
            self.thumbnailer_generation_queue = []

    def generateThumbnails(self, scan_id: int, device: Device) -> None:
        """Initiates generation of thumbnails for the device."""

        if scan_id not in self.removed_devices:
            self.rapidApp.downloadProgressBar.setMaximum(self.total_thumbs_to_generate)
            cache_dirs = self.getCacheLocations()
            uids = self.tsql.get_uids_for_device(scan_id=scan_id)
            rpd_files = list((self.rpd_files[uid] for uid in uids))

            gen_args = (scan_id, rpd_files, device.name(), cache_dirs, device.camera_model,
                        device.camera_port)
            if not self.thumbnailer_ready:
                self.thumbnailer_generation_queue.append(gen_args)
            else:
                self.thumbnailmq.generateThumbnails(*gen_args)

    def resetThumbnailTrackingAndDisplay(self):
        self.rapidApp.downloadProgressBar.reset()
        self.thumbnails_generated = 0
        self.total_thumbs_to_generate = 0

    def clearAll(self, scan_id: Optional[int]=None, keep_downloaded_files: bool=False) -> bool:
        """
        Removes files from display and internal tracking.

        If scan_id is not None, then only files matching that scan_id
        will be removed. Otherwise, everything will be removed.

        If keep_downloaded_files is True, files will not be removed if
        they have been downloaded.

        Two aspects to this task:
         1. remove files list of rows which drive the list view display
         2. remove files from backend DB and from thumbnails and rpd_files lists.

        :param scan_id: if None, keep_downloaded_files must be False
        :param keep_downloaded_files: don't remove thumbnails if they represent
         files that have now been downloaded
        :return: True if any displayed row was removed, else False
        """
        if scan_id is None and not keep_downloaded_files:
            self.initialize()
            return True
        else:
            assert scan_id is not None
            if keep_downloaded_files:
                logging.debug("Clearing all non-downloaded thumbnails for scan id %s", scan_id)
            else:
                logging.debug("Clearing all thumbnails for scan id %s", scan_id)
            # Generate list of displayed thumbnails to remove
            if keep_downloaded_files:
                uids = self.getDisplayedUids(scan_id=scan_id)
            else:
                uids = self.getDisplayedUids(scan_id=scan_id, downloaded=None)

            rows = [self.uid_to_row[uid] for uid in uids]

            if rows:
                # Generate groups of rows, and remove that group
                # Must do it in reverse!
                rows.sort()
                rrows = reversed(list(runs(rows)))
                for first, last in rrows:
                    no_rows = last - first + 1
                    self.removeRows(first, no_rows)

            # Delete from DB and thumbnails and rpd_files lists
            if keep_downloaded_files:
                uids = self.tsql.get_uids(scan_id=scan_id, downloaded=False)
            else:
                uids = self.tsql.get_uids(scan_id=scan_id)

            logging.debug("Removing %s thumbnail and rpd_files rows", len(uids))
            for uid in uids:
                del self.thumbnails[uid]
                del self.rpd_files[uid]

            self.uid_to_row = {row[0]: idx for idx, row in enumerate(self.rows)}

            if keep_downloaded_files:
                self.tsql.delete_files_by_scan_id(scan_id=scan_id, downloaded=False)
            else:
                self.tsql.delete_files_by_scan_id(scan_id=scan_id)

            self.removed_devices.add(scan_id)

            if scan_id in self.no_thumbnails_by_scan:
                self.recalculateThumbnailsPercentage(scan_id=scan_id)
            self.rapidApp.displayMessageInStatusBar()

            if self.tsql.get_count(scan_id=scan_id) == 0:
                self.tsql.delete_device(scan_id=scan_id)

            # self.validateModelConsistency()

            return len(rows) > 0

    def filesAreMarkedForDownload(self) -> bool:
        """
        Checks for the presence of checkmark besides any file that has
        not yet been downloaded.

        :return: True if there is any file that the user has indicated
        they intend to download, else False.
        """

        return self.tsql.any_files_marked()

    def getNoFilesMarkedForDownload(self) -> int:
        return self.tsql.get_count(marked=True)

    def getNoHiddenFiles(self) -> int:
        if self.rapidApp.showOnlyNewFiles():
            return self.tsql.get_count(previously_downloaded=True, downloaded=False)
        else:
            return 0

    def getNoFilesAndTypesMarkedForDownload(self) -> FileTypeCounter:
        uids = self.tsql.get_uids(marked=True)
        return FileTypeCounter(self.rpd_files[uid].file_type for uid in uids)

    def getSizeOfFilesMarkedForDownload(self) -> int:
        uids = self.tsql.get_uids(marked=True)
        return sum(self.rpd_files[uid].size for uid in uids)

    def getNoFilesAvailableForDownload(self) -> FileTypeCounter:
        uids = self.tsql.get_uids(downloaded=False)
        return FileTypeCounter(self.rpd_files[uid].file_type for uid in uids)

    def getFilesMarkedForDownload(self, scan_id: int) -> DownloadFiles:
        """
        Returns a dict of scan ids and associated files the user has
        indicated they want to download, and whether there are photos
        or videos included in the download.

        :param scan_id: if not None, then returns those files only from
        the device associated with that scan_id
        :return: namedtuple DownloadFiles with defaultdict() indexed by
        scan_id with value List(rpd_file), namedtuple DownloadTypes,
        and defaultdict() indexed by scan_id with value DownloadStats
        """

        files = defaultdict(list)
        download_types = DownloadTypes()
        download_stats = defaultdict(DownloadStats)
        camera_access_needed = defaultdict(bool)
        generating_fdo_thumbs = self.rapidApp.prefs.save_fdo_thumbnails


        uids = self.tsql.get_uids(scan_id=scan_id, marked=True, downloaded=False)

        for uid in uids:
            rpd_file = self.rpd_files[uid] # type: RPDFile
            scan_id = rpd_file.scan_id
            files[scan_id].append(rpd_file)
            if rpd_file.file_type == FileType.photo:
                download_types.photos = True
                download_stats[scan_id].no_photos += 1
                download_stats[scan_id].photos_size_in_bytes += rpd_file.size
            else:
                download_types.videos = True
                download_stats[scan_id].no_videos += 1
                download_stats[scan_id].videos_size_in_bytes += rpd_file.size
            if rpd_file.from_camera and not rpd_file.cache_full_file_name:
                camera_access_needed[scan_id] = True

            # Need to generate a thumbnail after a file has been renamed
            # if large FDO Cache thumbnail does not exist or if the
            # existing thumbnail has been marked as not suitable for the
            # FDO Cache (e.g. if we don't know the correct orientation).
            # TODO check to see if this code should be updated given can now
            # read orientation from most cameras
            if ((rpd_file.thumbnail_status !=
                    ThumbnailCacheStatus.suitable_for_fdo_cache_write) or
                    (generating_fdo_thumbs and not
                         rpd_file.fdo_thumbnail_256_name)):
                download_stats[scan_id].post_download_thumb_generation += 1

        return DownloadFiles(files=files, download_types=download_types,
                             download_stats=download_stats,
                             camera_access_needed=camera_access_needed)

    def markDownloadPending(self, files: Dict[int, List[RPDFile]]) -> None:
        """
        Sets status to download pending and updates thumbnails display

        :param files: rpd_files by scan
        """
        uids = [rpd_file.uid for scan_id in files for rpd_file in files[scan_id]]
        rows = [self.uid_to_row[uid] for uid in uids]
        for i in range(len(rows)):
            self.rows[rows[i]] = (uids[i], False)
        self.tsql.set_list_marked(uids=uids, marked=False)

        for uid in uids:
            self.rpd_files[uid].status = DownloadStatus.download_pending

        rows.sort()
        for first, last in runs(rows):
            self.dataChanged.emit(self.index(first, 0), self.index(last, 0))

    def markThumbnailsNeeded(self, rpd_files: List[RPDFile]) -> bool:
        """
        Analyzes the files that will be downloaded, and sees if any of
        them still need to have their thumbnails generated.

        Marks generate_thumbnail in each rpd_file those for that need
        thumbnails.

        :param rpd_files: list of files to examine
        :return: True if at least one thumbnail needs to be generated
        """

        generation_needed = False
        for rpd_file in rpd_files:
            if rpd_file.uid not in self.thumbnails:
                rpd_file.generate_thumbnail = True
                generation_needed = True
        return generation_needed

    def getNoFilesRemaining(self, scan_id: Optional[int]=None) -> int:
        """
        :param scan_id: if None, returns files remaining to be
         downloaded for all scan_ids, else only for that scan_id.
        :return the number of files that have not yet been downloaded
        """

        return self.tsql.get_count(scan_id=scan_id, downloaded=False)

    def updateSelection(self, reset_selection: bool=False) -> None:
        if reset_selection:
            self.rapidApp.thumbnailView.selectionModel().reset()
        select_all_photos = self.rapidApp.selectAllPhotosCheckbox.isChecked()
        select_all_videos = self.rapidApp.selectAllVideosCheckbox.isChecked()
        self.selectAll(select_all=select_all_photos, file_type=FileType.photo)
        self.selectAll(select_all=select_all_videos, file_type=FileType.video)

    def selectAll(self, select_all: bool,
                  file_type: FileType)-> None:
        """
        Check or deselect all visible files that are not downloaded.

        :param select_all:  if True, select, else deselect
        :param file_type: the type of files to select/deselect
        """

        uids = self.getDisplayedUids(file_type=file_type)

        if not uids:
            return

        selection = self.rapidApp.thumbnailView.selectionModel()  # type: QItemSelectionModel
        selected = selection.selection()  # type: QItemSelection

        if select_all:
            # print("gathering unique ids")
            rows = [self.uid_to_row[uid] for uid in uids]
            # print(len(rows))
            # print('doing sort')
            rows.sort()
            new_selection = QItemSelection()  # type: QItemSelection
            # print("creating new selection")
            for first, last in runs(rows):
                new_selection.select(self.index(first, 0), self.index(last, 0))
            # print('merging select')
            new_selection.merge(selected, QItemSelectionModel.Select)
            # print('resetting')
            selection.reset()
            # print('doing select')
            selection.select(new_selection, QItemSelectionModel.Select)
        else:
            # print("gathering unique ids from existing selection")
            if file_type == FileType.photo:
                keep_type = FileType.video
            else:
                keep_type = FileType.photo
            # print("filtering", keep_type)
            keep_rows = [index.row() for index in selected.indexes()
                         if self.rpd_files[self.rows[index.row()][0]].file_type == keep_type]
            rows = [index.row() for index in selected.indexes()]
            # print(len(keep_rows), len(rows))
            # print("sorting rows to keep")
            keep_rows.sort()
            new_selection = QItemSelection()  # type: QItemSelection
            # print("creating new selection")
            for first, last in runs(keep_rows):
                new_selection.select(self.index(first, 0), self.index(last, 0))
            # print('resetting')
            selection.reset()
            # print('doing select')
            selection.select(new_selection, QItemSelectionModel.Select)

        # print('doing data changed')
        for first, last in runs(rows):
            self.dataChanged.emit(self.index(first, 0), self.index(last, 0))
        # print("finished")

    def checkAll(self, check_all: bool,
                 file_type: Optional[FileType]=None,
                 scan_id: Optional[int]=None) -> None:
        """
        Check or uncheck all visible files that are not downloaded.

        A file is "visible" if it is in the current thumbnail display.
        That means if files are not showing because they are previously
        downloaded, they will not be affected. Likewise, if temporal
        proximity rows are selected, only those files are affected.

        Runs in the main thread and is thus time sensitive.

        :param check_all: if True, mark as checked, else unmark
        :param file_type: if specified, files must be of specified type
        :param scan_id: if specified, affects only files for that scan
        """

        uids = self.getDisplayedUids(marked=not check_all, file_type=file_type, scan_id=scan_id)
        self.tsql.set_list_marked(uids=uids, marked=check_all)
        rows = [self.uid_to_row[uid] for uid in uids]
        for row in rows:
            self.rows[row] = (self.rows[row][0], check_all)
        rows.sort()
        for first, last in runs(rows):
            self.dataChanged.emit(self.index(first, 0), self.index(last, 0))

        self.updateDeviceDisplayCheckMark(scan_id=scan_id)
        self.rapidApp.displayMessageInStatusBar()
        self.rapidApp.setDownloadActionState()

    def visibleRows(self):
        """
        Yield rows visible in viewport. Currently not used.
        """

        view = self.rapidApp.thumbnailView
        rect = view.viewport().contentsRect()
        width = view.itemDelegate().width
        last_row = rect.bottomRight().x() // width * width
        top = view.indexAt(rect.topLeft())
        if top.isValid():
            bottom = view.indexAt(QPoint(last_row, rect.bottomRight().y()))
            if not bottom.isValid():
                # take a guess with an arbitrary figure
                bottom = self.index(top.row() + 15)
            for row in range(top.row(), bottom.row() + 1):
                yield row

    def getDisplayedUids(self, scan_id: Optional[int]=None,
                         marked: Optional[bool]=None,
                         file_type: Optional[FileType]=None,
                         downloaded: Optional[bool]=False) -> List[bytes]:
        return self.tsql.get_uids(scan_id=scan_id, downloaded=downloaded, show=self.show,
                                  proximity_col1=self.proximity_col1,
                                  proximity_col2=self.proximity_col2,
                                  marked=marked, file_type=file_type)

    def getDisplayedCount(self, scan_id: Optional[int] = None,
                          marked: Optional[bool] = None) -> int:
        return self.tsql.get_count(scan_id=scan_id, downloaded=False, show=self.show,
                                   proximity_col1=self.proximity_col1,
                                   proximity_col2=self.proximity_col2, marked=marked)
    
    def updateDeviceDisplayCheckMark(self, scan_id: int) -> None:
        if scan_id not in self.removed_devices:
            uid_count = self.getDisplayedCount(scan_id=scan_id)
            checked_uid_count = self.getDisplayedCount(scan_id=scan_id, marked=True)
            if uid_count == 0 or checked_uid_count == 0:
                checked = Qt.Unchecked
            elif uid_count != checked_uid_count:
                checked = Qt.PartiallyChecked
            else:
                checked = Qt.Checked
            self.rapidApp.mapModel(scan_id).setCheckedValue(checked, scan_id)

    def updateAllDeviceDisplayCheckMarks(self) -> None:
        for scan_id in self.rapidApp.devices:
            self.updateDeviceDisplayCheckMark(scan_id=scan_id)

    def highlightDeviceThumbs(self, scan_id) -> None:
        """
        Animate fade to and from highlight color for thumbnails associated
        with device.
        :param scan_id: device's id
        """

        if scan_id == self.currently_highlighting_scan_id:
            return

        self.resetHighlighting()

        self.currently_highlighting_scan_id = scan_id
        if scan_id != self.most_recent_highlighted_device:
            highlighting = [self.uid_to_row[uid] for uid in self.getDisplayedUids(scan_id=scan_id)]
            highlighting.sort()
            self.highlighting_rows = list(runs(highlighting))
            self.most_recent_highlighted_device = scan_id
        self.highlighting_timeline.setDirection(QTimeLine.Forward)
        self.highlighting_timeline.start()

    def resetHighlighting(self) -> None:
        if self.currently_highlighting_scan_id is not None:
            self.highlighting_timeline.stop()
            self.doHighlightDeviceThumbs(value=0)

    @pyqtSlot(int)
    def doHighlightDeviceThumbs(self, value: int) -> None:
        self.highlight_value = value
        for first, last in self.highlighting_rows:
            self.dataChanged.emit(self.index(first, 0), self.index(last, 0))

    @pyqtSlot()
    def highlightPhaseFinished(self):
        self.currently_highlighting_scan_id = None

    def _resetHighlightingValues(self):
        self.most_recent_highlighted_device = None  # type: Optional[int]
        self.highlighting_rows = []  # type: List[int]

    def terminateThumbnailGeneration(self, scan_id: int) -> bool:
        """
        Terminates thumbnail generation if thumbnails are currently
        being generated for this scan_id
        :return True if thumbnail generation had to be terminated, else
        False
        """

        manager = self.thumbnailmq.thumbnail_manager

        terminate = scan_id in manager
        if terminate:
            manager.stop_worker(scan_id)
            # TODO update this check once checking for thumnbnailing code is more robust
            # note that check == 1 because it is assume the scan id has not been deleted
            # from the device collection
            if len(self.rapidApp.devices.thumbnailing) == 1:
                self.resetThumbnailTrackingAndDisplay()
            else:
                self.recalculateThumbnailsPercentage(scan_id=scan_id)
        return terminate

    def recalculateThumbnailsPercentage(self, scan_id: int) -> None:
        """
        Adjust % of thumbnails generated calculations after device removal.

        :param scan_id: id of removed device
        """

        self.total_thumbs_to_generate -= self.no_thumbnails_by_scan[scan_id]
        self.rapidApp.downloadProgressBar.setMaximum(self.total_thumbs_to_generate)
        del self.no_thumbnails_by_scan[scan_id]

    def updateStatusPostDownload(self, rpd_file: RPDFile):
        uid = rpd_file.uid
        self.rpd_files[uid] = rpd_file
        self.tsql.set_downloaded(uid=uid, downloaded=True)
        row = self.uid_to_row[uid]
        self.dataChanged.emit(self.index(row,0),self.index(row,0))

    def filesRemainToDownload(self) -> bool:
        """
        :return True if any files remain that are not downloaded, else
         returns False
        """
        return self.tsql.any_files_to_download()

    def dataForProximityGeneration(self) -> List[ThumbnailDataForProximity]:
        return [ThumbnailDataForProximity(uid=rpd_file.uid,
                                          mtime=rpd_file.modification_time,
                                          file_type=rpd_file.file_type,
                                          previously_downloaded=rpd_file.previously_downloaded())
                for rpd_file in self.rpd_files.values()]

    def assignProximityGroups(self, col1_col2_uid: List[Tuple[int, int, bytes]]) -> None:
        """
        For every uid, associates it with a cell in the temporal proximity view.

        Relevant columns are col 1 and col 2.
        """

        self.tsql.assign_proximity_groups(col1_col2_uid)

    def setProximityGroupFilter(self, col1: Optional[Sequence[int]],
                                col2: Optional[Sequence[int]]) -> None:
        """
        Filter display of thumbnails based on what cells the user has clicked in the
        Temporal Proximity view.

        Relevant columns are col 1 and col 2.
        """

        if col1 != self.proximity_col1 or col2 != self.proximity_col2:
            self.proximity_col1 = col1
            self.proximity_col2 = col2
            self.refresh()


class ThumbnailView(QListView):
    def __init__(self, parent: QWidget) -> None:
        style = """QAbstractScrollArea { background-color: %s;}""" % ThumbnailBackgroundName
        super().__init__(parent)
        self.rapidApp = parent
        self.setViewMode(QListView.IconMode)
        self.setResizeMode(QListView.Adjust)
        self.setStyleSheet(style)
        self.setUniformItemSizes(True)
        self.setSpacing(8)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)

    @pyqtSlot(QMouseEvent)
    def mousePressEvent(self, event: QMouseEvent) -> None:
        """
        Filter selection changes when click is on a thumbnail checkbox.

        When the user has selected multiple items (thumbnails), and
        then clicks one of the checkboxes, Qt's default behaviour is to
        treat that click as selecting the single item, because it doesn't
        know about our checkboxes. Therefore if the user is in fact
        clicking on a checkbox, we need to filter that event.

        Note that no matter what we do here, the delegate's editorEvent
        will still be triggered.

        :param event: the mouse click event
        """

        checkbox_clicked = False
        index = self.indexAt(event.pos())
        if index.row() >= 0:
            rect = self.visualRect(index)  # type: QRect
            delegate = self.itemDelegate(index)  # type: ThumbnailDelegate
            checkboxRect = delegate.getCheckBoxRect(rect)
            checkbox_clicked = checkboxRect.contains(event.pos())
            if checkbox_clicked:
                status = index.data(Roles.download_status)  # type: DownloadStatus
                checkbox_clicked = status not in Downloaded

        if not checkbox_clicked:
            super().mousePressEvent(event)

class ThumbnailDelegate(QStyledItemDelegate):
    """
    Render thumbnail cells
    """

    def __init__(self, rapidApp, parent=None) -> None:
        super().__init__(parent)
        self.rapidApp = rapidApp

        self.checkboxStyleOption = QStyleOptionButton()
        self.checkboxRect = QApplication.style().subElementRect(
            QStyle.SE_CheckBoxIndicator, self.checkboxStyleOption, None)
        self.checkbox_size = self.checkboxRect.size().height()

        self.downloadPendingIcon = QPixmap(':/download-pending.png')
        self.downloadedIcon = QPixmap(':/downloaded.png')
        self.downloadedWarningIcon = QPixmap(':/downloaded-with-warning.png')
        self.downloadedErrorIcon = QPixmap(':/downloaded-with-error.png')
        self.audioIcon = QPixmap(':/audio.png')

        self.dimmed_opacity = 0.5

        self.image_width = max(ThumbnailSize.width, ThumbnailSize.height)
        self.image_height = self.image_width
        self.horizontal_margin = 10
        self.vertical_margin = 10
        self.image_footer = self.checkbox_size
        self.footer_padding = 5

        # Position of first memory card indicator
        self.card_x = max(self.checkboxRect.size().width(),
                          self.downloadPendingIcon.width(),
                          self.downloadedIcon.width()) + \
                      self.horizontal_margin + self.footer_padding

        self.shadow_size = 2
        self.width = self.image_width + self.horizontal_margin * 2
        self.height = self.image_height + self.footer_padding \
                      + self.image_footer + self.vertical_margin * 2

        # Thumbnail is located in a 160px square...
        self.image_area_size = max(ThumbnailSize.width, ThumbnailSize.height)
        self.image_frame_bottom = self.vertical_margin + self.image_area_size

        self.contextMenu = QMenu()
        self.openInFileBrowserAct = self.contextMenu.addAction(_('Open in File Browser...'))
        self.openInFileBrowserAct.triggered.connect(self.doOpenInFileBrowserAct)
        self.copyPathAct = self.contextMenu.addAction(_('Copy Path'))
        self.copyPathAct.triggered.connect(self.doCopyPathAction)
        # store the index in which the user right clicked
        self.clickedIndex = None  # type: QModelIndex

        self.color3 = QColor(CustomColors.color3.value)

        self.paleGray = QColor(PaleGray)
        self.darkGray = QColor(DarkGray)

        palette = QGuiApplication.palette()
        self.highlight = palette.highlight().color()  # type: QColor
        self.highlight_size = 3
        self.highlight_offset = 1
        self.highlightPen = QPen()
        self.highlightPen.setColor(self.highlight)
        self.highlightPen.setWidth(self.highlight_size)
        self.highlightPen.setStyle(Qt.SolidLine)
        self.highlightPen.setJoinStyle(Qt.MiterJoin)

        self.emblemFont = QFont()
        self.emblemFont.setPointSize(self.emblemFont.pointSize() - 3)
        self.emblemFontMetrics = QFontMetrics(self.emblemFont)
        self.emblem_pad = self.emblemFontMetrics.height() // 3
        self.emblem_descent = self.emblemFontMetrics.descent()
        self.emblemMargins = QMargins(self.emblem_pad, self.emblem_pad, self.emblem_pad,
                                      self.emblem_pad)

        self.emblem_bottom = (self.image_frame_bottom + self.footer_padding +
                              self.emblemFontMetrics.height() + self.emblem_pad * 2)

        ch = Color(self.highlight.name())
        cg = Color(self.paleGray.name())
        self.colorGradient = [QColor(c.hex) for c in cg.range_to(ch, FadeSteps)]

    @pyqtSlot()
    def doCopyPathAction(self) -> None:
        index = self.clickedIndex
        if index:
            path = index.model().data(index, Roles.path)
            QApplication.clipboard().setText(path)

    @pyqtSlot()
    def doOpenInFileBrowserAct(self) -> None:
        index = self.clickedIndex
        if index:
            uri = index.model().data(index, Roles.uri)
            cmd = '{} {}'.format(self.rapidApp.file_manager, uri)
            logging.debug("Launching: %s", cmd)
            args = shlex.split(cmd)
            subprocess.Popen(args)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        if index is None:
            return

        # Save state of painter, restore on function exit
        painter.save()

        checked = index.data(Qt.CheckStateRole) == Qt.Checked
        previously_downloaded = index.data(Roles.previously_downloaded)
        extension, ext_type = index.data( Roles.extension)
        download_status = index.data( Roles.download_status) # type: DownloadStatus
        has_audio = index.data( Roles.has_audio)
        secondary_attribute = index.data(Roles.secondary_attribute)
        memory_cards = index.data(Roles.camera_memory_card) # type: List[int]
        highlight = index.data(Roles.highlight)

        x = option.rect.x()
        y = option.rect.y()

        # Draw recentangle in which the individual items will be placed
        boxRect = QRect(x, y, self.width, self.height)
        shadowRect = QRect(x + self.shadow_size, y + self.shadow_size,
                           self.width, self.height)

        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(self.darkGray)
        painter.fillRect(shadowRect, self.darkGray)
        painter.drawRect(shadowRect)
        painter.setRenderHint(QPainter.Antialiasing, False)
        if highlight != 0:
            painter.fillRect(boxRect, self.colorGradient[highlight-1])
        else:
            painter.fillRect(boxRect, self.paleGray)

        if option.state & QStyle.State_Selected:
            hightlightRect = QRect(boxRect.left() + self.highlight_offset,
                              boxRect.top() + self.highlight_offset,
                              boxRect.width() - self.highlight_size,
                              boxRect.height() - self.highlight_size)
            painter.setPen(self.highlightPen)
            painter.drawRect(hightlightRect)

        thumbnail = index.model().data(index, Qt.DecorationRole)
        if previously_downloaded and not checked:
            disabled = QPixmap(thumbnail.size())
            disabled.fill(Qt.transparent)
            p = QPainter(disabled)
            p.setBackgroundMode(Qt.TransparentMode)
            p.setBackground(QBrush(Qt.transparent))
            p.eraseRect(thumbnail.rect())
            p.setOpacity(self.dimmed_opacity)
            p.drawPixmap(0, 0, thumbnail)
            p.end()
            thumbnail = disabled

        thumbnail_width = thumbnail.size().width()
        thumbnail_height = thumbnail.size().height()

        thumbnailX = self.horizontal_margin + (self.image_area_size -
                                               thumbnail_width) // 2 + x
        thumbnailY = self.vertical_margin + (self.image_area_size -
                                               thumbnail_height) // 2 + y

        target = QRect(thumbnailX, thumbnailY, thumbnail_width,
                       thumbnail_height)
        source = QRect(0, 0, thumbnail_width, thumbnail_height)
        painter.drawPixmap(target, thumbnail, source)

        if previously_downloaded and not checked:
            painter.setOpacity(self.dimmed_opacity)

        # painter.setPen(QColor(Qt.blue))
        # painter.drawText(x + 2, y + 15, str(index.row()))

        if has_audio:
            audio_x = self.width // 2 - self.audioIcon.width() // 2 + x
            audio_y = self.image_frame_bottom + self.footer_padding
            painter.drawPixmap(audio_x, audio_y, self.audioIcon)

        # Draw a small coloured box containing the file extension in the
        #  bottom right corner
        extension = extension.upper()
        # Calculate size of extension text
        painter.setFont(self.emblemFont)
        rect = self.emblemFontMetrics.boundingRect(extension)  # type: QRect
        extBoundingRect = rect.marginsAdded(self.emblemMargins) # type: QRect
        text_width = self.emblemFontMetrics.width(extension)
        text_height = self.emblemFontMetrics.height()
        text_x = self.width - self.horizontal_margin - text_width - self.emblem_pad * 2 + x
        text_y = self.image_frame_bottom + self.footer_padding + text_height + y

        color = extensionColor(ext_type=ext_type)

        # Use an angular rect, because a rounded rect with anti-aliasing doesn't look too good
        rect = QRect(text_x, text_y - text_height,
                     extBoundingRect.width(), extBoundingRect.height())
        painter.fillRect(rect, color)
        painter.setPen(QColor(Qt.white))
        painter.drawText(rect, Qt.AlignCenter, extension)

        # Draw another small colored box to the left of the
        # file extension box containing a secondary
        # attribute, if it exists. Currently the secondary attribute is
        # only an XMP file, but in future it could be used to display a
        # matching jpeg in a RAW+jpeg set
        if secondary_attribute:
            extBoundingRect = self.emblemFontMetrics.boundingRect(
                secondary_attribute).marginsAdded(self.emblemMargins) # type: QRect
            text_width = self.emblemFontMetrics.width(secondary_attribute)
            text_x = text_x - text_width - self.emblem_pad * 2 - self.footer_padding
            color = QColor(self.color3)
            rect = QRect(text_x, text_y - text_height,
                         extBoundingRect.width(), extBoundingRect.height())
            painter.fillRect(rect, color)
            painter.drawText(rect, Qt.AlignCenter, secondary_attribute)

        if memory_cards:
            # if downloaded from a camera, and the camera has more than
            # one memory card, a list of numeric identifiers (i.e. 1 or
            # 2) identifying which memory card the file came from
            text_x = self.card_x + x
            for card in memory_cards:
                card = str(card)
                extBoundingRect = self.emblemFontMetrics.boundingRect(
                    card).marginsAdded(self.emblemMargins) # type: QRect
                color = QColor(70, 70, 70)
                rect = QRect(text_x, text_y - text_height,
                             extBoundingRect.width(), extBoundingRect.height())
                painter.fillRect(rect, color)
                painter.drawText(rect, Qt.AlignCenter, card)
                text_x = text_x + extBoundingRect.width() + self.footer_padding

        if previously_downloaded and not checked:
            painter.setOpacity(1.0)

        if download_status == DownloadStatus.not_downloaded:
            checkboxStyleOption = QStyleOptionButton()
            if checked:
                checkboxStyleOption.state |= QStyle.State_On
            else:
                checkboxStyleOption.state |= QStyle.State_Off
            checkboxStyleOption.state |= QStyle.State_Enabled
            checkboxStyleOption.rect = self.getCheckBoxRect(option.rect)
            QApplication.style().drawControl(QStyle.CE_CheckBox, checkboxStyleOption, painter)
        else:
            if download_status == DownloadStatus.download_pending:
                pixmap = self.downloadPendingIcon
            elif download_status == DownloadStatus.downloaded:
                pixmap = self.downloadedIcon
            elif (download_status == DownloadStatus.downloaded_with_warning or
                  download_status == DownloadStatus.backup_problem):
                pixmap = self.downloadedWarningIcon
            elif (download_status == DownloadStatus.download_failed or
                  download_status == DownloadStatus.download_and_backup_failed):
                pixmap = self.downloadedErrorIcon
            else:
                pixmap = None
            if pixmap is not None:
                painter.drawPixmap(option.rect.x() + self.horizontal_margin, text_y - text_height,
                                   pixmap)

        painter.restore()

    def sizeHint(self, option: QStyleOptionViewItem, index:  QModelIndex) -> QSize:
        return QSize(self.width + self.shadow_size, self.height
                     + self.shadow_size)

    def editorEvent(self, event: QEvent,
                    model: QAbstractItemModel,
                    option: QStyleOptionViewItem,
                    index: QModelIndex) -> bool:
        """
        Change the data in the model and the state of the checkbox
        if the user presses the left mousebutton or presses
        Key_Space or Key_Select and this cell is editable. Otherwise do nothing.
        """

        download_status = index.data(Roles.download_status)

        if (event.type() == QEvent.MouseButtonRelease or event.type() ==
            QEvent.MouseButtonDblClick):
            if event.button() == Qt.RightButton:
                self.clickedIndex = index
                globalPos = self.rapidApp.thumbnailView.viewport().mapToGlobal(event.pos())
                # libgphoto2 needs exclusive access to the camera, so there are times when "open
                # in file browswer" should be disabled:
                # First, for all desktops, when a camera, disable when thumbnailing or
                # downloading.
                # Second, disable opening MTP devices in KDE environment,
                # as KDE won't release them until them the file browser is closed!
                # However if the file is already downloaded, we don't care, as can get it from
                # local source.

                active_camera = disable_kde = False
                if download_status not in Downloaded:
                    if index.data(Roles.is_camera):
                        scan_id = index.data(Roles.scan_id)
                        active_camera = self.rapidApp.deviceState(scan_id) != DeviceState.idle
                    if not active_camera:
                        disable_kde = index.data(Roles.mtp) and get_desktop() == Desktop.kde

                self.openInFileBrowserAct.setEnabled(not (disable_kde or active_camera))
                self.contextMenu.popup(globalPos)
                return False
            if event.button() != Qt.LeftButton or not self.getCheckBoxRect(
                    option.rect).contains(event.pos()):
                return False
            if event.type() == QEvent.MouseButtonDblClick:
                return True
        elif event.type() == QEvent.KeyPress:
            if event.key() != Qt.Key_Space and event.key() != Qt.Key_Select:
                return False
        else:
            return False

        if download_status != DownloadStatus.not_downloaded:
            return False

        # Change the checkbox-state
        self.setModelData(None, model, index)
        return True

    def setModelData (self, editor: QWidget,
                      model: QAbstractItemModel,
                      index: QModelIndex) -> None:
        newValue = not (index.data(Qt.CheckStateRole) == Qt.Checked)
        thumbnailModel = self.rapidApp.thumbnailModel  # type: ThumbnailListModel
        selection = self.rapidApp.thumbnailView.selectionModel()  # type: QItemSelectionModel
        if selection.hasSelection():
            selected = selection.selection()  # type: QItemSelection
            if index in selected.indexes():
                for i in selected.indexes():
                    thumbnailModel.setData(i, newValue, Qt.CheckStateRole)
            else:
                # The user has clicked on a checkbox that for a
                # thumbnail that is outside their previous selection
                selection.clear()
                selection.select(index, QItemSelectionModel.Select)
                model.setData(index, newValue, Qt.CheckStateRole)
        else:
            # The user has previously selected nothing, so mark this
            # thumbnail as selected
            selection.select(index, QItemSelectionModel.Select)
            model.setData(index, newValue, Qt.CheckStateRole)
        thumbnailModel.updateDisplayPostDataChange()

    def getLeftPoint(self, rect: QRect) -> QPoint:
        return QPoint(rect.x() + self.horizontal_margin,
                      #rect.y() + self.emblem_bottom - self.checkbox_size)
                      rect.y() + self.image_frame_bottom + self.footer_padding - 1)

    def getCheckBoxRect(self, rect: QRect) -> QRect:
        return QRect(self.getLeftPoint(rect), self.checkboxRect.size())


