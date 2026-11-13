# -*- coding: utf-8 -*-

#################################################################################################

import os

import xbmc
import xbmcvfs

from .objects.obj import Objects
from .helper import translate, api, window, settings, dialog, event, JSONRPC
from .jellyfin import Jellyfin
from .helper import LazyLogger
from .helper import playutils
from .segments import SegmentChecker, SEGMENT_TYPES_MAP

#################################################################################################

LOG = LazyLogger(__name__)

# A seek this close to the end of the file can leave Kodi's decoder waiting for frames that
# never come, so playback freezes and the next episode never starts (#1227). A segment that
# reaches this window is "skipped" by finishing the item instead of seeking.
END_OF_FILE_WINDOW = 3.0

# Kodi's codec names for the DTS family, as Jellyfin names them (Codec, Profile).
KODI_CODECS = {
    "dtshd_ma": ("dts", "DTS-HD MA"),
    "dtshd_hra": ("dts", "DTS-HD HRA"),
    "dca": ("dts", None),
}

#################################################################################################


class Player(xbmc.Player):

    played = {}
    up_next = False
    skip_segments = {}
    skip_prompted = set()
    skip_dialog = None
    _skip_to_end = False
    segment_checker = None

    def __init__(self):
        xbmc.Player.__init__(self)

    def get_playing_file(self):
        try:
            return self.getPlayingFile()
        except Exception as error:
            LOG.exception(error)

    def get_file_info(self, file):
        try:
            return self.played[file]
        except Exception as error:
            LOG.exception(error)

    def is_playing_file(self, file):
        return file in self.played

    def onPlayBackStarted(self):
        """We may need to wait for info to be set in kodi monitor.
        Accounts for scenario where Kodi starts playback and exits immediately.
        First, ensure previous playback terminated correctly in Jellyfin.
        """

        self.stop_playback()
        self._reset_state(restart=True)
        count = 0
        monitor = xbmc.Monitor()

        try:
            current_file = self.getPlayingFile()

        except Exception:

            while count < 5:
                try:
                    current_file = self.getPlayingFile()
                    count = 0
                    break
                except Exception:
                    count += 1

                if monitor.waitForAbort(1):
                    return
            else:
                LOG.info("Cancel playback report")

                return

        items = window("jellyfin_play.json")
        item = None

        while not items:

            if monitor.waitForAbort(2):
                return

            items = window("jellyfin_play.json")
            count += 1

            if count == 20:
                LOG.info("Could not find jellyfin prop...")

                return

        for item in items:
            if item["Path"] == current_file:
                items.pop(items.index(item))

                break
        else:
            item = items.pop(0)

        window("jellyfin_play.json", items)

        self.set_item(current_file, item)
        # The tracks the add-on chose (server default, series memory, lossless
        # pick). Detecting Kodi's state below may overwrite them with whatever
        # Kodi opened first, so keep them to apply after the start settles.
        planned_audio = item["AudioStreamIndex"]
        planned_subtitle = item["SubtitleStreamIndex"]
        # Detect current audio/subtitle state from Kodi player
        self.detect_audio_subs(item)
        data = {
            "QueueableMediaTypes": "Video,Audio",
            "CanSeek": True,
            "ItemId": item["Id"],
            "MediaSourceId": item["MediaSourceId"],
            "PlayMethod": item["PlayMethod"],
            "VolumeLevel": item["Volume"],
            "PositionTicks": int(item["CurrentPosition"] * 10000000),
            "IsPaused": item["Paused"],
            "IsMuted": item["Muted"],
            "PlaySessionId": item["PlaySessionId"],
            "AudioStreamIndex": item["AudioStreamIndex"],
            "SubtitleStreamIndex": item["SubtitleStreamIndex"],
        }
        try:
            item["Server"].jellyfin.session_playing(data)
        except Exception as e:
            LOG.warning("Failed to report session playing: %s", e)
        window("jellyfin.skip.%s.bool" % item["Id"], True)

        # Load the segments now; the segment checker thread checks them within
        # a second. Checking here could open the Skip button for an intro at
        # 0:00 and hold this method until the intro ends, so the audio and
        # subtitle choice below would land mid-scene.
        if settings("mediaSegmentsEnabled.bool"):
            try:
                self._fetch_skip_segments(item)
            except Exception:
                pass  # Player may not be ready yet

        if monitor.waitForAbort(2):
            return

        if item["PlayOption"] == "Addon":
            item["AudioStreamIndex"] = planned_audio
            item["SubtitleStreamIndex"] = planned_subtitle
            self.set_audio_subs(planned_audio, planned_subtitle)
        else:
            self.apply_native_tracks(item)

    def set_item(self, file, item):
        """Set playback information."""
        try:
            item["Runtime"] = int(item["Runtime"])
        except (TypeError, ValueError):
            try:
                item["Runtime"] = int(self.getTotalTime())
                LOG.info("Runtime is missing, Kodi runtime: %s" % item["Runtime"])
            except Exception:
                item["Runtime"] = 0
                LOG.info("Runtime is missing, Using Zero")

        try:
            self.getTime()  # confirm the player is ready; bail out if it isn't
        except Exception:  # at this point we should be playing and if not then bail out
            return

        result = JSONRPC("Application.GetProperties").execute(
            {"properties": ["volume", "muted"]}
        )
        result = result.get("result", {})
        volume = result.get("volume")
        muted = result.get("muted")

        item.update(
            {
                "File": file,
                # A fresh playback start is at 0 (or the resume point, which is
                # already in item["CurrentPosition"]). Do NOT fall back to the live
                # player clock here: on a track change getTime() still returns the
                # previous track's position, which would report the new track as
                # starting mid-way and stop clients resetting their progress bar.
                "CurrentPosition": item.get("CurrentPosition") or 0,
                "Muted": muted,
                "Volume": volume,
                "Server": Jellyfin(item["ServerId"]).get_client(),
                "Paused": False,
            }
        )

        self.played[file] = item
        window("jellyfin_playing_id", item["Id"])
        LOG.info("-->[ play/%s ] %s", item["Id"], item)

    def set_audio_subs(self, audio=None, subtitle=None):
        if audio is not None:
            audio = int(audio)
        if subtitle:
            subtitle = int(subtitle)

        """ Only for after playback started
        """
        LOG.info("Setting audio: %s subs: %s", audio, subtitle)
        current_file = self.get_playing_file()

        if self.is_playing_file(current_file):

            item = self.get_file_info(current_file)
            mapping = item["SubsMapping"]
            kodi_audio_stream_indexes = item.get("KodiAudioStreamIndexes") or []

            if (
                audio is not None
                and len(self.getAvailableAudioStreams()) > 1
                and audio in kodi_audio_stream_indexes
            ):
                self.setAudioStream(kodi_audio_stream_indexes.index(audio))

            if subtitle is None or subtitle == -1:
                self.showSubtitles(False)

                return

            tracks = len(self.getAvailableAudioStreams())
            embedded = item.get("KodiEmbeddedSubtitleIndexes")

            if mapping:
                for index in mapping:

                    if mapping[index] == subtitle:
                        self.setSubtitleStream(int(index))

                        break
                else:
                    if embedded and subtitle in embedded:
                        self.setSubtitleStream(len(mapping) + embedded.index(subtitle))
                    else:
                        self.setSubtitleStream(len(mapping) + subtitle - tracks - 1)
            elif embedded and subtitle in embedded:
                self.setSubtitleStream(embedded.index(subtitle))
            else:
                self.setSubtitleStream(subtitle - tracks - 1)

    def detect_audio_subs(self, item):

        params = {
            "playerid": 1,
            "properties": ["currentsubtitle", "currentaudiostream", "subtitleenabled"],
        }
        result = JSONRPC("Player.GetProperties").execute(params)
        result = result.get("result")

        try:  # Audio tracks
            audio = result["currentaudiostream"]["index"]
        except (KeyError, TypeError):
            audio = None

        try:  # Subtitles tracks
            subs = result["currentsubtitle"]["index"]
        except (KeyError, TypeError):
            subs = 0

        try:  # If subtitles are enabled
            subs_enabled = result["subtitleenabled"]
        except (KeyError, TypeError):
            subs_enabled = False

        # When playback is started, the audiostream is not available
        # In such a case, audio is None and we must not overwrite the
        # item level value
        kodi_audio_stream_indexes = item.get("KodiAudioStreamIndexes") or []
        if audio is not None and 0 <= audio < len(kodi_audio_stream_indexes):
            item["AudioStreamIndex"] = kodi_audio_stream_indexes[audio]

        if not subs_enabled or not len(self.getAvailableSubtitleStreams()):
            item["SubtitleStreamIndex"] = None

            return

        mapping = item["SubsMapping"]
        tracks = len(self.getAvailableAudioStreams())
        embedded = item.get("KodiEmbeddedSubtitleIndexes")
        external_count = len(mapping) if mapping else 0

        if mapping and str(subs) in mapping:
            item["SubtitleStreamIndex"] = mapping[str(subs)]
        elif embedded and 0 <= subs - external_count < len(embedded):
            item["SubtitleStreamIndex"] = embedded[subs - external_count]
        elif mapping:
            item["SubtitleStreamIndex"] = subs - len(mapping) + tracks + 1
        else:
            item["SubtitleStreamIndex"] = subs + tracks + 1

    def next_up(self):
        if not settings("enableUpNext.bool"):
            return

        item = self.get_file_info(self.get_playing_file())
        objects = Objects()

        if item["Type"] != "Episode" or not item.get("CurrentEpisode"):
            return

        next_items = item["Server"].jellyfin.get_adjacent_episodes(
            item["CurrentEpisode"]["tvshowid"], item["Id"]
        )

        for index, next_item in enumerate(next_items["Items"]):
            if next_item["Id"] == item["Id"]:

                try:
                    next_item = next_items["Items"][index + 1]
                except IndexError:
                    LOG.warning("No next up episode.")

                    return

                break
        else:
            LOG.warning("Current episode not in its adjacent episodes.")

            return
        server_address = item["Server"].auth.get_server_info(
            item["Server"].auth.server_id
        )["address"]
        API = api.API(next_item, server_address)
        data = objects.map(next_item, "UpNext")
        artwork = API.get_all_artwork(objects.map(next_item, "ArtworkParent"), True)
        data["art"] = {
            "tvshow.poster": artwork.get("Series.Primary"),
            "tvshow.fanart": None,
            "thumb": artwork.get("Primary"),
        }
        if artwork["Backdrop"]:
            data["art"]["tvshow.fanart"] = artwork["Backdrop"][0]

        next_info = {
            "play_info": {
                "ItemIds": [data["episodeid"]],
                "ServerId": item["ServerId"],
                "PlayCommand": "PlayNow",
            },
            "current_episode": item["CurrentEpisode"],
            "next_episode": data,
        }

        LOG.info("--[ next up ] %s", next_info)
        event("upnext_data", next_info, hexlify=True)

    def onPlayBackPaused(self):
        current_file = self.get_playing_file()

        if self.is_playing_file(current_file):

            self.get_file_info(current_file)["Paused"] = True
            self.report_playback()
            LOG.debug("-->[ paused ]")

    def onPlayBackResumed(self):
        current_file = self.get_playing_file()

        if self.is_playing_file(current_file):

            self.get_file_info(current_file)["Paused"] = False
            self.report_playback()
            LOG.debug("--<[ paused ]")

    def onPlayBackSeek(self, time, seek_offset):
        """Does not seem to work in Leia??"""
        if self.is_playing_file(self.get_playing_file()):

            self.track_position()
            self.report_playback()
            LOG.info("--[ seek ]")

            # Check skip segments immediately after seek
            if settings("mediaSegmentsEnabled.bool"):
                try:
                    current_file = self.get_playing_file()
                    item = self.get_file_info(current_file)
                    current_pos = int(self.getTime())
                    self.check_skip_segments(item, current_pos)
                except Exception:
                    pass

    def report_playback(self, report=True):
        """Report playback progress to jellyfin server.
        Check if the user seek.
        """
        current_file = self.get_playing_file()

        if not self.is_playing_file(current_file):
            return

        item = self.get_file_info(current_file)

        if window("jellyfin.external.bool"):
            return

        if not report:
            previous = item["CurrentPosition"]

            try:
                item["CurrentPosition"] = int(self.getTime())
            except Exception as e:
                # getTime() raises RuntimeError if nothing is playing
                LOG.debug("Failed to get playback position: %s", e)
                return

            if int(item["CurrentPosition"]) == 1:
                return

            try:
                played = (
                    float(item["CurrentPosition"] * 10000000)
                    / int(item["Runtime"])
                    * 100
                )
            except ZeroDivisionError:  # Runtime is 0.
                played = 0

            if played > 2.0 and not self.up_next:

                self.up_next = True
                self.next_up()

            if (item["CurrentPosition"] - previous) < 30:
                return

        result = JSONRPC("Application.GetProperties").execute(
            {"properties": ["volume", "muted"]}
        )
        result = result.get("result", {})
        item["Volume"] = result.get("volume")
        item["Muted"] = result.get("muted")
        item["CurrentPosition"] = int(self.getTime())
        self.detect_audio_subs(item)

        data = {
            "QueueableMediaTypes": "Video,Audio",
            "CanSeek": True,
            "ItemId": item["Id"],
            "MediaSourceId": item["MediaSourceId"],
            "PlayMethod": item["PlayMethod"],
            "VolumeLevel": item["Volume"],
            "PositionTicks": int(item["CurrentPosition"] * 10000000),
            "IsPaused": item["Paused"],
            "IsMuted": item["Muted"],
            "PlaySessionId": item["PlaySessionId"],
            "AudioStreamIndex": item["AudioStreamIndex"],
            "SubtitleStreamIndex": item["SubtitleStreamIndex"],
        }
        item["Server"].jellyfin.session_progress(data)

    def onAVChange(self):
        """Kodi switched audio or subtitles: note it now, so Stop saves the pick."""
        try:
            current_file = self.get_playing_file()
            if self.is_playing_file(current_file):
                item = self.get_file_info(current_file)
                if item.get("PlayOption") == "Native":
                    if "NativeStart" in item:
                        item["NativeTracks"] = self._native_current_tracks(
                            self._kodi_streams()
                        )
                else:
                    self.detect_audio_subs(item)
        except Exception as error:
            LOG.debug("onAVChange: %s", error)

    @staticmethod
    def _kodi_streams():
        result = JSONRPC("Player.GetProperties").execute(
            {
                "playerid": 1,
                "properties": [
                    "audiostreams",
                    "subtitles",
                    "currentaudiostream",
                    "currentsubtitle",
                    "subtitleenabled",
                ],
            }
        )
        return (result or {}).get("result") or {}

    @staticmethod
    def _from_kodi(stream, kind):
        """A Kodi audio/subtitle stream as a Jellyfin-style stream, so the same
        matching and lossless rules apply in native mode."""
        codec = (stream.get("codec") or "").lower()
        codec, profile = KODI_CODECS.get(codec, (codec or None, None))
        return {
            "Index": stream.get("index"),
            "Type": kind,
            "Language": stream.get("language") or None,
            "Codec": codec,
            "Profile": profile,
            "Channels": stream.get("channels"),
            "Title": stream.get("name") or None,
            "IsForced": bool(stream.get("isforced")),
        }

    @classmethod
    def _native_current_tracks(cls, props):
        """[audio description, subtitle description or "off"] playing in Kodi now."""
        audio = props.get("currentaudiostream") or None
        subtitle = props.get("currentsubtitle") or None
        audio_desc = None
        if audio:
            converted = cls._from_kodi(audio, "Audio")
            audio_desc = playutils.describe_stream([converted], converted["Index"])
        if not props.get("subtitleenabled") or not subtitle:
            return [audio_desc, "off"]
        converted = cls._from_kodi(subtitle, "Subtitle")
        return [audio_desc, playutils.describe_stream([converted], converted["Index"])]

    def apply_native_tracks(self, item):
        """Native mode: Kodi plays the file itself, so apply the series memory and
        the lossless pick by matching Kodi's own stream list."""
        remember = settings("rememberSeriesTracks.bool")
        lossless = settings("preferLosslessAudio.bool")
        if not remember and not lossless:
            return
        try:
            props = self._kodi_streams()
            audios = [
                self._from_kodi(s, "Audio") for s in props.get("audiostreams") or []
            ]
            subs = [
                self._from_kodi(s, "Subtitle") for s in props.get("subtitles") or []
            ]

            entry = None
            if remember:
                item["TrackMemoryKey"] = self._native_memory_key(item)
                if item["TrackMemoryKey"]:
                    entry = playutils.recall_tracks(
                        playutils.load_track_memory(),
                        item["TrackMemoryKey"],
                        playutils.time.time(),
                    )

            audio = subtitle = None
            if entry:
                audio, subtitle = playutils.apply_track_memory(entry, audios + subs)

            current = (props.get("currentaudiostream") or {}).get("index")
            if audio is None and lossless and current is not None:
                picked = playutils.pick_audio_stream(
                    audios, current, playutils.kodi_lossless_passthrough()
                )
                if picked != current:
                    audio = picked

            if audio is not None and audio != current:
                self.setAudioStream(audio)
            if subtitle == -1:
                self.showSubtitles(False)
            elif subtitle is not None:
                self.setSubtitleStream(subtitle)
                self.showSubtitles(True)

            LOG.info("Native tracks: audio %s, subtitle %s", audio, subtitle)
            if item.get("TrackMemoryKey"):
                changed = audio is not None or subtitle is not None
                applied = self._kodi_streams() if changed else props
                item["NativeStart"] = self._native_current_tracks(applied)
                item["NativeTracks"] = item["NativeStart"]
        except Exception as error:
            LOG.warning("Native track choice failed: %s", error)

    @staticmethod
    def _native_memory_key(item):
        """Same key as add-on mode (user/series), so both modes share the memory."""
        if item.get("Type") != "Episode":
            return None
        server = item["Server"]
        series_id = (server.jellyfin.get_item(item["Id"]) or {}).get("SeriesId")
        user_id = server.config.data.get("auth.user_id") or ""
        return "%s/%s" % (user_id, series_id) if series_id else None

    @staticmethod
    def _remember_series_tracks(item):
        """Save the audio/subtitles of an episode for its series when they were
        changed during playback; an unchanged pick leaves the memory alone."""
        key = item.get("TrackMemoryKey")
        if not key or not settings("rememberSeriesTracks.bool"):
            return

        if item.get("PlayOption") == "Native":
            start, now = item.get("NativeStart"), item.get("NativeTracks")
            if not start or not now or now == start:
                return
            Player._save_series_tracks(key, now[0], now[1])
            return

        def subs(index):
            return None if index in (None, -1) else index

        audio = item.get("AudioStreamIndex")
        subtitle = subs(item.get("SubtitleStreamIndex"))
        if audio == item.get("StartAudioStreamIndex") and subtitle == subs(
            item.get("StartSubtitleStreamIndex")
        ):
            return

        streams = item.get("TrackStreams") or []
        audio_desc = playutils.describe_stream(streams, audio)
        subtitle_desc = (
            "off" if subtitle is None else playutils.describe_stream(streams, subtitle)
        )
        if audio_desc is None and subtitle_desc is None:
            return
        Player._save_series_tracks(key, audio_desc, subtitle_desc)

    @staticmethod
    def _save_series_tracks(key, audio_desc, subtitle_desc):
        try:
            memory = playutils.load_track_memory()
            playutils.remember_tracks(
                memory, key, audio_desc, subtitle_desc, playutils.time.time()
            )
            playutils.save_track_memory(memory)
            LOG.info(
                "Remembered tracks for %s: %s / %s", key, audio_desc, subtitle_desc
            )
        except Exception as error:
            LOG.warning("Could not remember tracks: %s", error)

    def track_position(self):
        """Remember where playback is, once a second, so the stop report
        sends the position on screen instead of the last progress poll."""
        try:
            current_file = self.get_playing_file()
            if not self.is_playing_file(current_file):
                return
            self.get_file_info(current_file)["LivePosition"] = float(self.getTime())
        except Exception as error:
            LOG.debug("track_position: %s", error)

    def onPlayBackStopped(self):
        """Will be called when user stops playing a file."""
        window("jellyfin_play", clear=True)
        self.stop_playback()
        LOG.info("--<[ playback ]")

    def onPlayBackEnded(self):
        """Will be called when kodi stops playing a file."""
        self.stop_playback()
        LOG.info("--<<[ playback ]")

    def stop_playback(self):
        """Stop all playback. Check for external player for positionticks."""
        if not self.played:
            return

        segments_by_item = dict(self.skip_segments)
        self._reset_state()

        LOG.info("Played info: %s", self.played)

        for file in self.played:
            item = self.get_file_info(file)

            window("jellyfin.skip.%s.bool" % item["Id"], True)

            self._remember_series_tracks(item)

            live_position = item.pop("LivePosition", None)
            if live_position is not None:
                item["CurrentPosition"] = live_position

            if window("jellyfin.external.bool"):
                window("jellyfin.external", clear=True)

                if int(item["CurrentPosition"]) == 1:
                    item["CurrentPosition"] = self._runtime_seconds(item)

            if item.get("FinishPosition"):
                item["CurrentPosition"] = item.pop("FinishPosition")
            elif self._stopped_in_end_credits(
                item, segments_by_item.get(item["Id"]), item["CurrentPosition"]
            ):
                LOG.info("Stopped in the end credits: reporting the item as finished")
                item["CurrentPosition"] = self._runtime_seconds(item)

            data = {
                "ItemId": item["Id"],
                "MediaSourceId": item["MediaSourceId"],
                "PositionTicks": int(item["CurrentPosition"] * 10000000),
                "PlaySessionId": item["PlaySessionId"],
            }
            item["Server"].jellyfin.session_stop(data)

            if item.get("LiveStreamId"):

                LOG.info("<[ livestream/%s ]", item["LiveStreamId"])
                item["Server"].jellyfin.close_live_stream(item["LiveStreamId"])

            elif item["PlayMethod"] == "Transcode":

                LOG.info("<[ transcode/%s ]", item["Id"])
                item["Server"].jellyfin.close_transcode(
                    item["DeviceId"], item["PlaySessionId"]
                )

            path = xbmcvfs.translatePath(
                "special://profile/addon_data/plugin.video.jellyfin/temp/"
            )

            if xbmcvfs.exists(path):
                dirs, files = xbmcvfs.listdir(path)

                for file in files:
                    # Only delete the cached files for the previous play session
                    if item["Id"] in file:
                        xbmcvfs.delete(os.path.join(path, file))

            result = {}
            if settings("offerDelete.bool"):
                result = item["Server"].jellyfin.get_item(item["Id"]) or {}

            if "UserData" in result and result["UserData"]["Played"]:
                delete = False

                if result["Type"] == "Episode" and settings("deleteTV.bool"):
                    delete = True
                elif result["Type"] == "Movie" and settings("deleteMovies.bool"):
                    delete = True

                if not settings("offerDelete.bool"):
                    delete = False

                if delete:
                    LOG.info("Offer delete option")

                    if dialog(
                        "yesno", translate(30091), translate(33015), autoclose=120000
                    ):
                        item["Server"].jellyfin.delete_item(item["Id"])

            window("jellyfin.external_check", clear=True)

        self.played.clear()
        window("jellyfin_playing_id", clear=True)

    @staticmethod
    def _runtime_seconds(item):
        """Runtime in seconds: item["Runtime"] holds RunTimeTicks from the server,
        or seconds when set_item filled it from Kodi."""
        try:
            runtime = float(item.get("Runtime") or 0)
        except (TypeError, ValueError):
            return 0.0
        if runtime > 1e6:
            runtime /= 10000000.0
        return runtime

    @classmethod
    def _stopped_in_end_credits(cls, item, segments, position):
        """True when playback stopped inside a Credits segment that runs to the
        end of the file, so the stop should count the item as watched."""
        runtime = cls._runtime_seconds(item)
        try:
            position = float(position)
        except (TypeError, ValueError):
            return False
        if runtime <= 0 or not segments:
            return False

        for segment in segments.values():
            if segment.get("Type") != "Credits":
                continue
            start = segment.get("Start")
            end = segment.get("End")
            if start is None or end is None or end <= start:
                continue
            if end >= runtime - END_OF_FILE_WINDOW and start <= position:
                return True
        return False

    def _fetch_skip_segments(self, item):
        if not settings("mediaSegmentsEnabled.bool"):
            return

        item_id = item["Id"]
        self.skip_segments.pop(item_id, None)
        self.skip_prompted = set()

        self._reset_skip_dialog()

        segments = item["Server"].jellyfin.get_media_segments(item_id)
        if segments:
            segments = self._convert_media_segments(segments)

        if segments:
            self.skip_segments[item_id] = segments
            LOG.info("Loaded media segments for %s: %s", item_id, list(segments.keys()))

    def _convert_media_segments(self, response):
        if not response or "Items" not in response:
            return None

        segments = {}
        for item in response["Items"]:
            seg_type = SEGMENT_TYPES_MAP.get(item.get("Type"))
            if seg_type:
                segments[item.get("Id")] = {
                    "EpisodeId": item.get("ItemId"),
                    "Type": seg_type,
                    "Start": item.get("StartTicks", 0) / 10000000.0,
                    "End": item.get("EndTicks", 0) / 10000000.0,
                }
        return segments if segments else None

    def _process_segment(
        self, item_id, segment_type, segment, current_position, skip_mode
    ):
        """Check if current position is within segment bounds. Returns (start, end) tuple, None if outside bounds, or False if invalid."""
        start = segment.get("Start")
        end = segment.get("End")
        if start is None or end is None or end <= start:
            return False

        LOG.debug(
            "Skip check: pos=%.1f, %s start=%.1f end=%.1f, in_segment=%s",
            current_position,
            segment_type,
            start,
            end,
            start <= current_position <= end,
        )

        if not (start <= current_position <= end):
            return None

        return (start, end)

    def check_skip_segments(self, item, current_position):
        item_id = item["Id"]
        segments = self.skip_segments.get(item_id)
        if not segments:
            return

        for segment_id, segment in segments.items():

            segment_type = segment.get("Type")

            skip_mode = self._get_segment_skip_mode(segment_type)
            if skip_mode == 0:  # Off
                continue

            bounds = self._process_segment(
                item_id, segment_type, segment, current_position, skip_mode
            )
            if not bounds:
                continue

            start, end = bounds

            LOG.debug(
                "Skip check: IN WINDOW! segment_key=%s, already_prompted=%s",
                segment_id,
                segment_id in self.skip_prompted,
            )
            if segment_id in self.skip_prompted:
                continue

            self.skip_prompted.add(segment_id)
            LOG.debug(
                "Skip check: Triggering _handle_skip_segment for %s", segment_type
            )

            if segment_type == "Credits" and not self.up_next:
                self.up_next = True
                self.next_up()

            self._handle_skip_segment(segment_type, start, end, skip_mode)
            break

    def _get_segment_skip_mode(self, segment_type):
        """Get the skip mode for a segment type. Returns 0=Off, 1=Auto, 2=Button."""
        setting_map = {
            "Introduction": "skipIntroductionMode",
            "Credits": "skipCreditsMode",
            "Recap": "skipRecapMode",
            "Preview": "skipPreviewMode",
            "Commercial": "skipCommercialMode",
        }
        setting_key = setting_map.get(segment_type)
        if not setting_key:
            return 0
        return int(settings(setting_key) or 0)

    def _get_runtime_for_seek(self):
        try:
            total_time = float(self.getTotalTime())
            if total_time > 0:
                return total_time
        except Exception:
            pass

        try:
            current_file = self.get_playing_file()
            if current_file and self.is_playing_file(current_file):
                item = self.get_file_info(current_file)
                runtime = float(item.get("Runtime") or 0)
                # Runtime holds RunTimeTicks from the server (playutils), or seconds when
                # set_item had to fill it from Kodi. Ticks for anything over 0.1 s exceed 1e6;
                # seconds stay far below that (1e6 s = 11 days).
                if runtime > 1e6:
                    runtime /= 10000000.0
                if runtime > 0:
                    return runtime
        except Exception:
            pass

        return 0.0

    def _get_safe_seek_time(self, seek_time, margin=1.0):
        try:
            safe_seek_time = max(0.0, float(seek_time))
        except (TypeError, ValueError):
            return None

        runtime = self._get_runtime_for_seek()
        if runtime > 0:
            max_seek_time = runtime - margin
            if max_seek_time <= 0:
                max_seek_time = runtime
            safe_seek_time = min(safe_seek_time, max_seek_time)

        try:
            current_position = float(self.getTime())
            if safe_seek_time <= current_position:
                return None
        except Exception:
            pass

        return safe_seek_time

    def _segment_reaches_end(self, end):
        runtime = self._get_runtime_for_seek()
        return runtime > 0 and end >= runtime - END_OF_FILE_WINDOW

    def _finish_current_item(self, segment_type):
        """Skip to the end without seeking near EOF: play the next playlist entry,
        or stop with the stop position reported at the end so the item counts as played.
        """
        runtime = self._get_runtime_for_seek()
        try:
            current_file = self.get_playing_file()
            if runtime > 0 and self.is_playing_file(current_file):
                self.get_file_info(current_file)["FinishPosition"] = runtime
        except Exception as error:
            LOG.debug("_finish_current_item: no playing item: %s", error)

        playlist = xbmc.PlayList(xbmc.PLAYLIST_VIDEO)
        if 0 <= playlist.getposition() < playlist.size() - 1:
            LOG.info("Skipped %s to the end: next playlist item", segment_type)
            self.playnext()
        else:
            LOG.info("Skipped %s to the end: stopping", segment_type)
            self.stop()

    def _handle_skip_segment(self, segment_type, start, end, mode):
        if self._segment_reaches_end(end):
            try:
                if float(self.getTime()) >= end:
                    return
            except Exception:
                pass

            if mode == 1:  # Auto skip
                self._finish_current_item(segment_type)
            elif mode == 2:  # Show skip button
                runtime = self._get_runtime_for_seek()
                self._skip_to_end = True
                self._show_skip_button(
                    segment_type, max(0.0, end - start), runtime - END_OF_FILE_WINDOW
                )
            return

        safe_end = self._get_safe_seek_time(end)
        if safe_end is None or safe_end <= start:
            return

        LOG.debug(
            "_handle_skip_segment: type=%s, mode=%d, start=%.1f, end=%.1f, safe_end=%.1f",
            segment_type,
            mode,
            start,
            end,
            safe_end,
        )

        if mode == 1:  # Auto skip
            self.seekTime(safe_end)
            LOG.info("Auto-skipped %s to %.1f", segment_type, safe_end)
            # Show notification
            message = "Skipped %s" % segment_type
            dialog(
                "notification",
                heading="Jellyfin",
                message=message,
                icon="{jellyfin}",
                time=3000,
            )

        elif mode == 2:  # Show skip button
            self._skip_to_end = False
            self._show_skip_button(segment_type, safe_end - start, safe_end)

    def _show_skip_button(self, segment_type, duration, end_time):
        LOG.debug(
            "_show_skip_button: type=%s, duration=%.1f, end_time=%.1f",
            segment_type,
            duration,
            end_time,
        )
        try:
            import xbmcaddon
            from .dialogs.skip import SkipDialog

            self._reset_skip_dialog()

            addon_path = xbmcaddon.Addon("plugin.video.jellyfin").getAddonInfo("path")
            LOG.debug("_show_skip_button: addon_path=%s", addon_path)

            self.skip_dialog = SkipDialog(
                "script-jellyfin-skip.xml",
                addon_path,
                "default",
                "1080i",
            )
            self.skip_dialog.set_skip_info(segment_type, duration)
            LOG.debug("_show_skip_button: calling show()")
            self.skip_dialog.show()
            LOG.debug("_show_skip_button: show() completed")

            self._skip_end_time = end_time
            self._monitor_skip_dialog()
        except Exception as e:
            LOG.error("_show_skip_button error: %s", e, exc_info=True)

    def _monitor_skip_dialog(self):
        """Monitor the skip dialog and handle user input or timeout."""
        LOG.debug("_monitor_skip_dialog: starting, end_time=%.1f", self._skip_end_time)
        monitor = xbmc.Monitor()

        # Monitor loop - check for user input or end of segment
        while self.skip_dialog and not monitor.abortRequested():
            # Check if user clicked skip
            if self.skip_dialog.is_skip():
                if self._skip_to_end:
                    self._reset_skip_dialog()
                    self._finish_current_item("segment")
                    break
                self.seekTime(self._skip_end_time)
                LOG.info("User skipped to %.1f", self._skip_end_time)
                break

            # Check if user cancelled
            if self.skip_dialog.is_cancel():
                LOG.debug("User cancelled skip dialog")
                break

            # Check if we've passed the segment end time
            try:
                current_pos = self.getTime()
                if current_pos >= self._skip_end_time:
                    LOG.debug(
                        "_monitor_skip_dialog: passed end_time %.1f, closing",
                        self._skip_end_time,
                    )
                    break
            except Exception:
                break

            if monitor.waitForAbort(0.2):
                break

        LOG.debug("_monitor_skip_dialog: exiting loop")
        self._reset_skip_dialog()
        self._skip_to_end = False

    def _reset_state(self, restart=False):
        self._reset_segment_checker(restart)
        self._reset_skip_dialog()

        self.up_next = False
        self.skip_segments = {}
        self.skip_prompted = set()

    def _reset_segment_checker(self, restart=False):
        if self.segment_checker:
            self.segment_checker.stop()

        if restart:
            self.segment_checker = SegmentChecker(player=self)
            self.segment_checker.start()

    def _reset_skip_dialog(self):
        if self.skip_dialog:
            try:
                self.skip_dialog.close()
            except Exception:
                pass

        self.skip_dialog = None
