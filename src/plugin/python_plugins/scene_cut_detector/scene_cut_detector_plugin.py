#!/usr/bin/env python
# SPDX-License-Identifier: Apache-2.0
"""Scene Cut Detector plugin.

Adds a "Detect Scene Cuts" command to the playlist media-item context menu
(and, as a fallback, to the currently selected Clip on an open Timeline).
It analyses the source video for hard scene cuts and builds a new Timeline
in the current Playlist where the source has been split into one sub-clip
per detected scene.

Detection is done by piping downscaled grayscale frames out of ffmpeg and
looking for large frame-to-frame differences (numpy). This avoids relying
on compiled Python video-decoding extensions (e.g. opencv-python, PyAV):
those wheels' native DLLs failed to load in testing on this machine, while
a plain ffmpeg.exe subprocess + numpy worked fine.

Under linux this requires 'ffmpeg' to be available on PATH, e.g.:
    winget install Gyan.FFmpeg
"""

import sys
import platform
import shutil
import subprocess
import threading
from pathlib import Path
from urllib.parse import unquote

import numpy as np

from xstudio.plugin import PluginBase

# path to QML resources relative to this .py file
qml_folder_name = "qml/SceneCutDetector.1"

# Frames are downscaled to this size before comparison - big enough to
# catch real cuts, small enough to decode/compare quickly.
_ANALYSIS_WIDTH = 64
_ANALYSIS_HEIGHT = 36
_ANALYSIS_FRAME_BYTES = _ANALYSIS_WIDTH * _ANALYSIS_HEIGHT

DETECTOR_OPTIONS = ["Content (fast cuts)", "Adaptive (camera motion)"]

def uri_to_local_path(uri_str):
    """Convert an xSTUDIO file URI to a local filesystem path."""
    path_part = uri_str.removeprefix("file://")\
                       .removeprefix("file:")\
                       .removeprefix("localhost")

    path_part = unquote(path_part).lstrip("/")
    if len(path_part) > 1 and path_part[1] == ":":
        return path_part
    return "/" + path_part

class MissingFFmpegError(Exception):
    """Raised when the 'ffmpeg' executable can't be found on PATH."""


class SceneCutDetector(PluginBase):
    """Split a media item into sub-clips at each detected scene cut."""

    def __init__(self, connection):

        PluginBase.__init__(
            self,
            connection,
            name="SceneCutDetector",
            qml_folder=qml_folder_name,
        )

        # User-facing settings, remembered as preferences between sessions,
        # and exposed as a group so our settings dialog QML can bind to them
        # (same mechanism AnnotationsExporter uses for its export dialog).
        self.detector_type = self.add_attribute(
            attribute_name="Detector",
            attribute_value=DETECTOR_OPTIONS[0],
            attribute_role_data={
                "combo_box_options": DETECTOR_OPTIONS,
                "combo_box_options_enabled": [True, True],
            },
            register_as_preference=True,
        )

        self.threshold = self.add_attribute(
            attribute_name="Cut Threshold",
            attribute_value=20.0,
            register_as_preference=True,
        )

        self.min_scene_len = self.add_attribute(
            attribute_name="Minimum Scene Length (frames)",
            attribute_value=15,
            register_as_preference=True,
        )

        self.progress = self.add_attribute(
            attribute_name="Progress",
            attribute_value=0.0
        )

        self.message = self.add_attribute(
            attribute_name="Message",
            attribute_value=""
        )

        self.running = self.add_attribute(
            attribute_name="Running",
            attribute_value=False
        )

        for attr in (self.detector_type, self.threshold, self.min_scene_len,
                     self.progress, self.message, self.running):
            attr.expose_in_ui_attrs_group("scene_cut_detector_attrs")

        # Single menu item - opens the settings dialog, which has its own
        # "Detect Scene Cuts" button to trigger the run.
        self.menu_id = self.insert_menu_item(
            menu_model_name="media_list_menu_",
            menu_text="Detect Scene Cuts...",
            menu_path="Media Actions",
            menu_item_position=1000.0,
            callback=self.menu_callback,
        )

        self.detect_thread = None
        self.detect_cancel = threading.Event()

        self.connect_to_ui()

    # ------------------------------------------------------------------
    # Menu handling
    # ------------------------------------------------------------------

    def menu_callback(self):
        """Open the settings dialog for the currently selected media."""

        self.running.set_value(False)

        media, _ = self._selected_media()

        if media is None:
            self.popup_message_box(
                "Detect Scene Cuts",
                "Select a single video media item (in a Playlist, or a Clip "
                "on a Timeline) before running scene cut detection.",
            )
            return

        self.create_qml_item("SceneCutDetectorDialog {}")

    def run_from_dialog(self):
        """Called from the settings dialog's "Detect Scene Cuts" button.

        Runs the scene detection asynchronously.
        """

        # stop any running detect thread
        if self.detect_thread and self.detect_thread.is_alive():
            self.detect_cancel.set()
            self.detect_thread.join()

        # start a new detect thread
        self.detect_thread = threading.Thread(target=self._detect_and_build_timeline)
        self.detect_cancel.clear()
        self.detect_thread.start()

    def cancel_from_dialog(self):
        """Called from the settings dialog's "Cancel" button.

        Runs synchronously (blocking the UI) - see NOTE in
        _detect_and_build_timeline for why this can't use a background
        thread. Returns [True, message] on success, or an error string,
        matching the convention AnnotationsExportDialog.qml expects.
        """
        if self.detect_thread and self.detect_thread.is_alive():
            self.detect_cancel.set()
            self.detect_thread.join()
        self.message.set_value("")
        self.running.set_value(False)

    def _selected_media(self):
        """Resolve the media the user wants to analyse.

        Looks first at the playlist media-list selection, then falls back to
        the selected Clip on the currently inspected Timeline.

        Returns both the media item as well as its parent playlist.
        """

        # NOTE: track.insert_clip() (and other helpers like create_video_track,
        # insert_gap) call Connection.remote_spawn(), which only works over a
        # genuine external/remote Connection - it fails with "Failed to spawn
        # remote actor" when called via a resident plugin's own in-process
        # connection (confirmed by testing: identical code succeeds from an
        # external script using its own Connection(auto_connect=True), but
        # fails via self.connection here). So we open our own loopback
        # connection to do the actor-spawning parts of timeline construction.
        from xstudio.connection import Connection as _Connection
        loopback_connection = _Connection(auto_connect=True)

        selected_media = None
        selected_container = loopback_connection.api.session.inspected_container
        if selected_container:
            selected_medias = selected_container.playhead_selection.selected_sources
            if selected_medias:
                selected_media = selected_medias[0]
            if selected_container.type != "Playlist":
                selected_container = selected_container.parent_playlist

        return [selected_media, selected_container]

    # ------------------------------------------------------------------
    # Detection + timeline construction
    # ------------------------------------------------------------------

    def _detect_and_build_timeline(self):
        """This runs in a thread and comminicates with the dialog via
        attributes. Catch all exceptions and display them in the dialog.
        """
        timeline = None

        try:
            self.running.set_value(True)

            media, playlist = self._selected_media()
            if media is None:
                self.message.set_value("No media selected.")
                return

            timeline = playlist.create_timeline(
                name=f"{media.name} - Scene Cuts",
                with_tracks=True)[1]
            timeline.history.enabled = False
            timeline.add_media(media)

            track = timeline.video_tracks[0]
            track.insert_clip(media)

            file_path = self._resolve_media_path(media)

            cut_frames = self._detect_cuts(file_path, track)

            if cut_frames:
                self.message.set_value(
                    f"Done!\nCreated a new Timeline with {len(cut_frames)} scene cuts.")
            else:
                self.message.set_value(f"No scene cuts were detected in '{media.name}'.")

        except MissingFFmpegError:
            self.message.set_value("'ffmpeg' could not be found on PATH.")
        except Exception:  # noqa: BLE001 - log full traceback, then re-raise
            import traceback
            traceback.print_exc()
            self.message.set_value("Exception received. Please check the console.")

        if timeline is not None:
            timeline.history.enabled = True
        self.running.set_value(False)

    def _resolve_media_path(self, media):
        """Get a local filesystem path for the selected media's current source."""

        # first check the lower level metadata for the path since it
        # will have already been through any file path remapping
        source = media.media_source()
        try:
            file_path = source.metadata["metadata"]["media"]["@"]["format"]["filename"]
        except KeyError:
            media_ref = source.media_reference
            if media_ref is None:
                raise RuntimeError("Selected media has no resolvable media reference.")
            file_path = str(media_ref.uri())

        file_path = uri_to_local_path(file_path)
        if not Path(file_path).is_file():
            raise RuntimeError(f"Media file not found on disk: {file_path}")

        return file_path

    def _detect_cuts(self, file_path, track):
        """Decode downscaled grayscale frames via ffmpeg and delegate to
        compute_cut_frames() for the actual difference/threshold logic.

        Returns a sorted list of 0-based frame numbers (in source decode
        order) where a new scene starts.
        """
        appname = 'ffmpeg'
        if platform.system() == 'Darwin':
            appname = str(Path(sys.executable).parent / 'ffmpeg')
        elif platform.system() == 'Windows':
            appname = str(Path(sys.executable).parent / 'ffmpeg.exe')

        if shutil.which(appname) is None:
            raise MissingFFmpegError()

        cmd = [
            appname,
            "-v", "error",
            "-i", file_path,
            "-vf", f"scale={_ANALYSIS_WIDTH}:{_ANALYSIS_HEIGHT}",
            "-pix_fmt", "gray",
            "-f", "rawvideo",
            "-",
        ]

        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        frame_count = 0

        def read_frames():
            nonlocal frame_count
            while True:
                raw = proc.stdout.read(_ANALYSIS_FRAME_BYTES)
                if len(raw) < _ANALYSIS_FRAME_BYTES:
                    break
                frame_count += 1
                yield np.frombuffer(raw, dtype=np.uint8)

        try:
            cut_frames = self._compute_cut_frames(read_frames(), track)
        finally:
            proc.stdout.close()
            stderr = proc.stderr.read()
            proc.wait()

        if proc.returncode != 0 and frame_count == 0:
            raise RuntimeError(
                "ffmpeg failed to decode \"{}\":\n{}".format(
                    file_path, stderr.decode(errors="replace")
                )
            )

        return cut_frames

    def _compute_cut_frames(self, frames, track):
        """Return frame numbers where consecutive frames indicate a scene cut."""
        detector_name = self.detector_type.value()
        threshold = self.threshold.value()
        min_scene_len = int(self.min_scene_len.value())
        
        duration = track.trimmed_range.frame_duration().frames()

        cut_frames = []
        recent_diffs = []
        previous_frame = None
        last_cut_frame = -min_scene_len
        self.progress.set_value(0.0)
        self.message.set_value("")

        for frame_index, frame in enumerate(frames):
            frame = np.asarray(frame, dtype=np.float32)

            if previous_frame is not None:
                diff = float(np.abs(frame - previous_frame).mean())

                if detector_name == DETECTOR_OPTIONS[1] and recent_diffs:
                    rolling_mean = sum(recent_diffs) / len(recent_diffs)
                    is_cut = diff > threshold and diff > 3.0 * max(rolling_mean, 1.0)
                else:
                    is_cut = diff > threshold

                if is_cut and (frame_index - last_cut_frame) >= min_scene_len:
                    last_cut_frame = frame_index
                    track.split(frame_index)

                    cut_frames.append(frame_index)
                    cut_count = len(cut_frames)
                    self.message.set_value(f"Found scene cut #{cut_count}")

                    clip = track.clips[-2]
                    clip.item_name = f"Shot {cut_count:03d}"

                recent_diffs.append(diff)
                if len(recent_diffs) > 15:
                    recent_diffs.pop(0)

            previous_frame = frame

            self.progress.set_value(frame_index / (duration-1))

            if self.detect_cancel.is_set():
                break

        total_shots = len(cut_frames) + 1
        track.clips[-1].item_name = f"Shot {total_shots:03d}"

        # loop over all the clips renaming them since
        # the above renaming isn't terribly reliable
        for i, clip in enumerate(track.clips):
            clip.item_name = f"Shot {i+1:03d}"

        return cut_frames

def create_plugin_instance(connection):
    """Create and return a plugin instance."""
    return SceneCutDetector(connection)
