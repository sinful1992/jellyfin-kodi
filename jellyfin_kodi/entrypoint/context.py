# -*- coding: utf-8 -*-

#################################################################################################

import json
import sys

import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs

from .. import database
from ..dialogs import context
from ..helper import translate, settings, dialog, LazyLogger
from ..jellyfin import Jellyfin
from . import playseries

#################################################################################################

LOG = LazyLogger(__name__)
XML_PATH = (
    xbmcaddon.Addon("plugin.video.jellyfin").getAddonInfo("path"),
    "default",
    "1080i",
)
OPTIONS = {
    "Refresh": translate(30410),
    "Delete": translate(30409),
    "Addon": translate(30408),
    "AddFav": translate(30405),
    "RemoveFav": translate(30406),
    "Transcode": translate(30412),
    "PlaySeries": translate(30704),
}

#################################################################################################


class Context(object):

    _selected_option = None

    def __init__(self, transcode=False, delete=False):

        try:
            self.kodi_id = sys.listitem.getVideoInfoTag().getDbId() or None
            self.media = self.get_media_type()
            self.server_id = sys.listitem.getProperty("jellyfinserver") or None
            self.api_client = Jellyfin(self.server_id).get_client().jellyfin
            item_id = sys.listitem.getProperty("jellyfinid")
        except AttributeError:
            self.server_id = None

            if xbmc.getInfoLabel("ListItem.Property(jellyfinid)"):
                item_id = xbmc.getInfoLabel("ListItem.Property(jellyfinid)")
            else:
                self.kodi_id = xbmc.getInfoLabel("ListItem.DBID")
                self.media = xbmc.getInfoLabel("ListItem.DBTYPE")
                item_id = None

        addon_data = xbmcvfs.translatePath(
            "special://profile/addon_data/plugin.video.jellyfin/data.json"
        )
        with open(addon_data, "rb") as infile:
            data = json.load(infile)

        try:
            server_data = data["Servers"][0]
            self.api_client.config.data["auth.server"] = server_data.get("address")
            self.api_client.config.data["auth.server-name"] = server_data.get("Name")
            self.api_client.config.data["auth.user_id"] = server_data.get("UserId")
            self.api_client.config.data["auth.token"] = server_data.get("AccessToken")
        except Exception as e:
            LOG.warning("Addon appears to not be configured yet: {}".format(e))

        if self.server_id or item_id:
            self.item = self.api_client.get_item(item_id)
        else:
            self.item = self.get_item_id()

        if self.item:

            if transcode:
                self.transcode()

            elif delete:
                self.delete_item()

            elif self.select_menu():
                self.action_menu()

                if self._selected_option in (
                    OPTIONS["Delete"],
                    OPTIONS["AddFav"],
                    OPTIONS["RemoveFav"],
                ):

                    xbmc.sleep(500)
                    xbmc.executebuiltin("Container.Refresh")

    def get_media_type(self):
        """Get media type based on sys.listitem. If unfilled, base on visible window."""
        media = sys.listitem.getVideoInfoTag().getMediaType()

        if not media:

            if xbmc.getCondVisibility("Container.Content(albums)"):
                media = "album"
            elif xbmc.getCondVisibility("Container.Content(artists)"):
                media = "artist"
            elif xbmc.getCondVisibility("Container.Content(songs)"):
                media = "song"
            elif xbmc.getCondVisibility("Container.Content(pictures)"):
                media = "picture"
            else:
                LOG.info("media is unknown")

        return media

    def get_item_id(self):
        """Get synced item from jellyfindb."""
        item = database.get_item(self.kodi_id, self.media)

        if not item:
            return

        return {
            "Id": item[0],
            "UserData": json.loads(item[4]) if item[4] else {},
            "Type": item[3],
        }

    def select_menu(self):
        """Display the select dialog.
        Favorites, Refresh, Delete (opt), Settings.
        """
        options = []

        if self.item["Type"] in ("Series", "Season"):
            options.append(OPTIONS["PlaySeries"])

        if self.item["Type"] != "Season":

            if self.item["UserData"].get("IsFavorite"):
                options.append(OPTIONS["RemoveFav"])
            else:
                options.append(OPTIONS["AddFav"])

        options.append(OPTIONS["Refresh"])

        if settings("enableContextDelete.bool"):
            options.append(OPTIONS["Delete"])

        options.append(OPTIONS["Addon"])

        context_menu = context.ContextMenu("script-jellyfin-context.xml", *XML_PATH)
        context_menu.set_options(options)
        context_menu.doModal()

        if context_menu.is_selected():
            self._selected_option = context_menu.get_selected()

        return self._selected_option

    def action_menu(self):

        selected = self._selected_option

        if selected == OPTIONS["Refresh"]:
            self.api_client.refresh_item(self.item["Id"])

        elif selected == OPTIONS["AddFav"]:
            self.api_client.favorite(self.item["Id"], True)

        elif selected == OPTIONS["RemoveFav"]:
            self.api_client.favorite(self.item["Id"], False)

        elif selected == OPTIONS["Addon"]:
            xbmc.executebuiltin("Addon.OpenSettings(plugin.video.jellyfin)")

        elif selected == OPTIONS["Delete"]:
            self.delete_item()

        elif selected == OPTIONS["PlaySeries"]:
            self.play_series()

    def play_series(self):
        """Start the series at the right episode, also for a rewatch."""
        series_id = self.item["Id"]
        if self.item["Type"] == "Season":
            series_id = self.item.get("SeriesId") or self.api_client.get_item(
                self.item["Id"]
            ).get("SeriesId")
        if not series_id:
            return

        next_up = self.api_client.shows(
            "/NextUp", {"SeriesId": series_id, "UserId": "{UserId}", "Limit": 1}
        )
        episodes = self.api_client.shows(
            "/%s/Episodes" % series_id, {"UserId": "{UserId}"}
        )
        episode, resume = playseries.pick_series_episode(
            (next_up or {}).get("Items"), (episodes or {}).get("Items")
        )
        if not episode:
            LOG.info("Play series: no episodes for %s", series_id)
            return

        queue = playseries.series_queue((episodes or {}).get("Items"), episode)
        LOG.info(
            "Play series %s: %s (resume %s), %s queued",
            series_id,
            episode["Id"],
            resume,
            len(queue),
        )
        playlist = xbmc.PlayList(xbmc.PLAYLIST_VIDEO)
        playlist.clear()
        for position, queued in enumerate(queue):
            listitem = xbmcgui.ListItem(queued.get("Name") or "")
            listitem.setProperty("IsPlayable", "true")
            playlist.add(
                playseries.play_url(
                    queued["Id"], self.server_id, resume if position == 0 else None
                ),
                listitem,
            )
        xbmc.Player().play(playlist)

    def delete_item(self):
        if settings("skipContextMenu.bool") or dialog(
            "yesno", "{jellyfin}", translate(33015)
        ):
            self.api_client.delete_item(self.item["Id"])

    def transcode(self):
        filename = xbmc.getInfoLabel("ListItem.Filenameandpath")
        filename += "&transcode=true"
        xbmc.executebuiltin("PlayMedia(%s)" % filename)
