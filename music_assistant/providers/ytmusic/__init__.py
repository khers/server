"""Youtube Music support for MusicAssistant."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import suppress
from datetime import datetime
from io import StringIO
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote

import yt_dlp
from aiohttp import ClientConnectorError
from duration_parser import parse as parse_str_duration
from music_assistant_models.config_entries import ConfigEntry, ConfigValueType
from music_assistant_models.enums import (
    AlbumType,
    ConfigEntryType,
    ContentType,
    ImageType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import (
    InvalidDataError,
    LoginFailed,
    MediaNotFoundError,
    UnplayableMediaError,
)
from music_assistant_models.media_items import (
    Album,
    Artist,
    AudioFormat,
    ItemMapping,
    MediaItemImage,
    MediaItemType,
    MediaType,
    Playlist,
    Podcast,
    PodcastEpisode,
    ProviderMapping,
    RecommendationFolder,
    SearchResults,
    Track,
)
from music_assistant_models.streamdetails import StreamDetails
from ytmusicapi.constants import SUPPORTED_LANGUAGES
from ytmusicapi.exceptions import YTMusicServerError
from ytmusicapi.helpers import get_authorization, sapisid_from_cookie

from music_assistant.constants import CONF_USERNAME, VERBOSE_LOG_LEVEL
from music_assistant.controllers.cache import use_cache
from music_assistant.models.music_provider import MusicProvider

from .helpers import (
    add_remove_playlist_tracks,
    convert_to_netscape,
    determine_recommendation_icon,
    get_album,
    get_artist,
    get_home,
    get_library_albums,
    get_library_artists,
    get_library_playlists,
    get_library_podcasts,
    get_library_tracks,
    get_playlist,
    get_podcast,
    get_podcast_episode,
    get_song_radio_tracks,
    get_track,
    is_brand_account,
    library_add_remove_album,
    library_add_remove_artist,
    library_add_remove_playlist,
    search,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant import MusicAssistant
    from music_assistant.models import ProviderInstanceType


CONF_COOKIE = "cookie"
CONF_PO_TOKEN_SERVER_URL = "po_token_server_url"
DEFAULT_PO_TOKEN_SERVER_URL = "http://127.0.0.1:4416"

YTM_DOMAIN = "https://music.youtube.com"
YTM_COOKIE_DOMAIN = ".youtube.com"
YTM_BASE_URL = f"{YTM_DOMAIN}/youtubei/v1/"
VARIOUS_ARTISTS_YTM_ID = "UCUTXlgdcKU5vfzFqHOWIvkA"
# Playlist ID's are not unique across instances for lists like 'Liked videos', 'SuperMix' etc.
# So we need to add a delimiter to make them unique
YT_PLAYLIST_ID_DELIMITER = "🎵"
PODCAST_EPISODE_SPLITTER = "|"
YT_PERSONAL_PLAYLISTS = (
    "LM",  # Liked songs
    "SE"  # Episodes for Later
    "RDTMAK5uy_kset8DisdE7LSD4TNjEVvrKRTmG7a56sY",  # SuperMix
    "RDTMAK5uy_nGQKSMIkpr4o9VI_2i56pkGliD6FQRo50",  # My Mix 1
    "RDTMAK5uy_lz2owBgwWf1mjzyn_NbxzMViQzIg8IAIg",  # My Mix 2
    "RDTMAK5uy_k5UUl0lmrrfrjMpsT0CoMpdcBz1ruAO1k",  # My Mix 3
    "RDTMAK5uy_nTsa0Irmcu2li2-qHBoZxtrpG9HuC3k_Q",  # My Mix 4
    "RDTMAK5uy_lfZhS7zmIcmUhsKtkWylKzc0EN0LW90-s",  # My Mix 5
    "RDTMAK5uy_k78ni6Y4fyyl0r2eiKkBEICh9Q5wJdfXk",  # My Mix 6
    "RDTMAK5uy_lfhhWWw9v71CPrR7MRMHgZzbH6Vku9iJc",  # My Mix 7
    "RDTMAK5uy_n_5IN6hzAOwdCnM8D8rzrs3vDl12UcZpA",  # Discover Mix
    "RDTMAK5uy_lr0LWzGrq6FU9GIxWvFHTRPQD2LHMqlFA",  # New Release Mix
    "RDTMAK5uy_nilrsVWxrKskY0ZUpVZ3zpB0u4LwWTVJ4",  # Replay Mix
    "RDTMAK5uy_mZtXeU08kxXJOUhL0ETdAuZTh1z7aAFAo",  # Archive Mix
)
YTM_PREMIUM_CHECK_TRACK_ID = "dQw4w9WgXcQ"

SUPPORTED_FEATURES = {
    ProviderFeature.LIBRARY_ARTISTS,
    ProviderFeature.LIBRARY_ALBUMS,
    ProviderFeature.LIBRARY_TRACKS,
    ProviderFeature.LIBRARY_PLAYLISTS,
    ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
    ProviderFeature.ARTIST_ALBUMS,
    ProviderFeature.ARTIST_TOPTRACKS,
    ProviderFeature.SIMILAR_TRACKS,
    ProviderFeature.LIBRARY_PODCASTS,
    ProviderFeature.RECOMMENDATIONS,
}


# TODO: fix disabled tests
# ruff: noqa: PLW2901, RET504


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return YoutubeMusicProvider(mass, manifest, config)


async def get_config_entries(
    mass: MusicAssistant,  # noqa: ARG001
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to setup this provider.

    instance_id: id of an existing provider instance (None if new instance setup).
    action: [optional] action key called from config entries UI.
    values: the (intermediate) raw values for config entries sent with the action.
    """
    return (
        ConfigEntry(
            key=CONF_USERNAME, type=ConfigEntryType.STRING, label="Username", required=True
        ),
        ConfigEntry(
            key=CONF_COOKIE,
            type=ConfigEntryType.SECURE_STRING,
            label="Login Cookie",
            required=True,
            description="The Login cookie you grabbed from an existing session, "
            "see the documentation.",
        ),
        ConfigEntry(
            key=CONF_PO_TOKEN_SERVER_URL,
            type=ConfigEntryType.STRING,
            default_value=DEFAULT_PO_TOKEN_SERVER_URL,
            label="PO Token Server URL",
            required=True,
            description="The URL to the PO Token server. "
            "Can be left as default for most people. \n\n"
            "**Note that this does require you to have the "
            "'YT Music PO Token Generator' addon installed!**",
        ),
    )


class YoutubeMusicProvider(MusicProvider):
    """Provider for Youtube Music."""

    _headers = None
    _context = None
    _cookies = None
    _cipher = None
    _yt_user = None
    _cookie = None

    async def handle_async_init(self) -> None:
        """Set up the YTMusic provider."""
        logging.getLogger("yt_dlp").setLevel(self.logger.level + 10)
        self._cookie = self.config.get_value(CONF_COOKIE)
        self._po_token_server_url = (
            self.config.get_value(CONF_PO_TOKEN_SERVER_URL) or DEFAULT_PO_TOKEN_SERVER_URL
        )
        if not await self._verify_po_token_url():
            raise LoginFailed(
                "PO Token server URL is not reachable. "
                "Make sure you have installed the YT Music PO Token Generator "
                "and that it is running."
            )
        yt_username = self.config.get_value(CONF_USERNAME)
        self._yt_user = yt_username if is_brand_account(yt_username) else None
        # yt-dlp needs a netscape formatted cookie
        self._netscape_cookie = convert_to_netscape(self._cookie, YTM_COOKIE_DOMAIN)
        self._initialize_headers()
        self._initialize_context()
        self._cookies = {"CONSENT": "YES+1"}
        # get default language (that is supported by YTM)
        mass_locale = self.mass.metadata.locale
        for lang_code in SUPPORTED_LANGUAGES:
            if lang_code in (mass_locale, mass_locale.split("_")[0]):
                self.language = lang_code
                break
        else:
            self.language = "en"
        if not await self._user_has_ytm_premium():
            raise LoginFailed("User does not have Youtube Music Premium")

    @property
    def supported_features(self) -> set[ProviderFeature]:
        """Return the features supported by this Provider."""
        return SUPPORTED_FEATURES

    async def search(
        self, search_query: str, media_types=list[MediaType], limit: int = 5
    ) -> SearchResults:
        """Perform search on musicprovider.

        :param search_query: Search query.
        :param media_types: A list of media_types to include. All types if None.
        :param limit: Number of items to return in the search (per type).
        """
        parsed_results = SearchResults()
        ytm_filter = None
        if len(media_types) == 1:
            # YTM does not support multiple searchtypes, falls back to all if no type given
            if media_types[0] == MediaType.ARTIST:
                ytm_filter = "artists"
            if media_types[0] == MediaType.ALBUM:
                ytm_filter = "albums"
            if media_types[0] == MediaType.TRACK:
                ytm_filter = "songs"
            if media_types[0] == MediaType.PLAYLIST:
                ytm_filter = "playlists"
            if media_types[0] == MediaType.RADIO:
                # bit of an edge case but still good to handle
                return parsed_results
        results = await search(
            query=search_query, ytm_filter=ytm_filter, limit=limit, language=self.language
        )
        parsed_results = SearchResults()
        for result in results:
            try:
                if result["resultType"] == "artist" and MediaType.ARTIST in media_types:
                    parsed_results.artists.append(self._parse_artist(result))
                elif result["resultType"] == "album" and MediaType.ALBUM in media_types:
                    parsed_results.albums.append(self._parse_album(result))
                elif result["resultType"] == "playlist" and MediaType.PLAYLIST in media_types:
                    parsed_results.playlists.append(self._parse_playlist(result))
                elif (
                    result["resultType"] in ("song", "video")
                    and MediaType.TRACK in media_types
                    and (track := self._parse_track(result))
                ):
                    parsed_results.tracks.append(track)
            except InvalidDataError:
                pass  # ignore invalid item
        return parsed_results

    async def get_library_artists(self) -> AsyncGenerator[Artist, None]:
        """Retrieve all library artists from Youtube Music."""
        artists_obj = await get_library_artists(
            headers=self._headers, language=self.language, user=self._yt_user
        )
        for artist in artists_obj:
            yield self._parse_artist(artist)

    async def get_library_albums(self) -> AsyncGenerator[Album, None]:
        """Retrieve all library albums from Youtube Music."""
        albums_obj = await get_library_albums(
            headers=self._headers, language=self.language, user=self._yt_user
        )
        for album in albums_obj:
            yield self._parse_album(album, album["browseId"])

    async def get_library_playlists(self) -> AsyncGenerator[Playlist, None]:
        """Retrieve all library playlists from the provider."""
        playlists_obj = await get_library_playlists(
            headers=self._headers, language=self.language, user=self._yt_user
        )
        for playlist in playlists_obj:
            yield self._parse_playlist(playlist)

    async def get_library_tracks(self) -> AsyncGenerator[Track, None]:
        """Retrieve library tracks from Youtube Music."""
        tracks_obj = await get_library_tracks(
            headers=self._headers, language=self.language, user=self._yt_user
        )
        for track in tracks_obj:
            # Library tracks sometimes do not have a valid artist id
            # In that case, call the API for track details based on track id
            try:
                yield self._parse_track(track)
            except InvalidDataError:
                track = await self.get_track(track["videoId"])
                yield track

    async def get_library_podcasts(self) -> AsyncGenerator[Podcast, None]:
        """Retrieve the library podcasts from Youtube Music."""
        podcasts_obj = await get_library_podcasts(
            headers=self._headers, language=self.language, user=self._yt_user
        )
        for podcast in podcasts_obj:
            yield self._parse_podcast(podcast)

    async def get_album(self, prov_album_id) -> Album:
        """Get full album details by id."""
        if album_obj := await get_album(prov_album_id=prov_album_id, language=self.language):
            return self._parse_album(album_obj=album_obj, album_id=prov_album_id)
        msg = f"Item {prov_album_id} not found"
        raise MediaNotFoundError(msg)

    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get album tracks for given album id."""
        album_obj = await get_album(prov_album_id=prov_album_id, language=self.language)
        if not album_obj.get("tracks"):
            return []
        tracks = []
        for track_obj in album_obj["tracks"]:
            try:
                track = self._parse_track(track_obj=track_obj)
            except InvalidDataError:
                continue
            tracks.append(track)
        return tracks

    async def get_artist(self, prov_artist_id) -> Artist:
        """Get full artist details by id."""
        if artist_obj := await get_artist(
            prov_artist_id=prov_artist_id, headers=self._headers, language=self.language
        ):
            return self._parse_artist(artist_obj=artist_obj)
        msg = f"Item {prov_artist_id} not found"
        raise MediaNotFoundError(msg)

    async def get_track(self, prov_track_id) -> Track:
        """Get full track details by id."""
        if track_obj := await get_track(
            prov_track_id=prov_track_id,
            headers=self._headers,
            language=self.language,
        ):
            return self._parse_track(track_obj)
        msg = f"Item {prov_track_id} not found"
        raise MediaNotFoundError(msg)

    async def get_playlist(self, prov_playlist_id) -> Playlist:
        """Get full playlist details by id."""
        # Grab the playlist id from the full url in case of personal playlists
        if YT_PLAYLIST_ID_DELIMITER in prov_playlist_id:
            prov_playlist_id = prov_playlist_id.split(YT_PLAYLIST_ID_DELIMITER)[0]
        if playlist_obj := await get_playlist(
            prov_playlist_id=prov_playlist_id,
            headers=self._headers,
            language=self.language,
            user=self._yt_user,
        ):
            return self._parse_playlist(playlist_obj)
        msg = f"Item {prov_playlist_id} not found"
        raise MediaNotFoundError(msg)

    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[Track]:
        """Return playlist tracks for the given provider playlist id."""
        if page > 0:
            # paging not supported, we always return the whole list at once
            return []
        # Grab the playlist id from the full url in case of personal playlists
        if YT_PLAYLIST_ID_DELIMITER in prov_playlist_id:
            prov_playlist_id = prov_playlist_id.split(YT_PLAYLIST_ID_DELIMITER)[0]
        # Add a try to prevent MA from stopping syncing whenever we fail a single playlist
        try:
            playlist_obj = await get_playlist(
                prov_playlist_id=prov_playlist_id, headers=self._headers, user=self._yt_user
            )
        except KeyError as ke:
            self.logger.warning("Could not load playlist: %s: %s", prov_playlist_id, ke)
            return []
        if "tracks" not in playlist_obj:
            return []
        result = []
        # TODO: figure out how to handle paging in YTM
        for index, track_obj in enumerate(playlist_obj["tracks"], 1):
            if track_obj["isAvailable"]:
                # Playlist tracks sometimes do not have a valid artist id
                # In that case, call the API for track details based on track id
                try:
                    if track := self._parse_track(track_obj):
                        track.position = index
                        result.append(track)
                except InvalidDataError:
                    if track := await self.get_track(track_obj["videoId"]):
                        track.position = index
                        result.append(track)
        # YTM doesn't seem to support paging so we ignore offset and limit
        return result

    async def get_artist_albums(self, prov_artist_id) -> list[Album]:
        """Get a list of albums for the given artist."""
        artist_obj = await get_artist(prov_artist_id=prov_artist_id, headers=self._headers)
        if "albums" in artist_obj and "results" in artist_obj["albums"]:
            albums = []
            for album_obj in artist_obj["albums"]["results"]:
                if "artists" not in album_obj:
                    album_obj["artists"] = [
                        {"id": artist_obj["channelId"], "name": artist_obj["name"]}
                    ]
                albums.append(self._parse_album(album_obj, album_obj["browseId"]))
            return albums
        return []

    async def get_artist_toptracks(self, prov_artist_id) -> list[Track]:
        """Get a list of 25 most popular tracks for the given artist."""
        artist_obj = await get_artist(prov_artist_id=prov_artist_id, headers=self._headers)
        if artist_obj.get("songs") and artist_obj["songs"].get("browseId"):
            prov_playlist_id = artist_obj["songs"]["browseId"]
            playlist_tracks = await self.get_playlist_tracks(prov_playlist_id)
            return playlist_tracks[:25]
        return []

    async def get_podcast(self, prov_podcast_id: str) -> Podcast:
        """Get the full details of a Podcast."""
        podcast_obj = await get_podcast(prov_podcast_id, headers=self._headers)
        return self._parse_podcast(podcast_obj)

    async def get_podcast_episodes(
        self, prov_podcast_id: str
    ) -> AsyncGenerator[PodcastEpisode, None]:
        """Get all episodes from a podcast."""
        podcast_obj = await get_podcast(prov_podcast_id, headers=self._headers)
        podcast_obj["podcastId"] = prov_podcast_id
        podcast = self._parse_podcast(podcast_obj)
        for index, episode_obj in enumerate(podcast_obj.get("episodes", []), start=1):
            episode = self._parse_podcast_episode(episode_obj, podcast)
            ep_index = episode_obj.get("index") or index
            episode.position = ep_index
            yield episode

    async def get_podcast_episode(self, prov_episode_id: str) -> PodcastEpisode:
        """Get a single Podcast Episode."""
        podcast_id, episode_id = prov_episode_id.split(PODCAST_EPISODE_SPLITTER)
        podcast = await self.get_podcast(podcast_id)
        episode_obj = await get_podcast_episode(episode_id, headers=self._headers)
        episode = self._parse_podcast_episode(episode_obj, podcast)
        episode.position = 0
        return episode

    async def library_add(self, item: MediaItemType) -> bool:
        """Add an item to the library."""
        result = False
        if item.media_type == MediaType.ARTIST:
            result = await library_add_remove_artist(
                headers=self._headers, prov_artist_id=item.item_id, add=True, user=self._yt_user
            )
        elif item.media_type == MediaType.ALBUM:
            result = await library_add_remove_album(
                headers=self._headers, prov_item_id=item.item_id, add=True, user=self._yt_user
            )
        elif item.media_type == MediaType.PLAYLIST:
            result = await library_add_remove_playlist(
                headers=self._headers, prov_item_id=item.item_id, add=True, user=self._yt_user
            )
        elif item.media_type == MediaType.TRACK:
            raise NotImplementedError
        return result

    async def library_remove(self, prov_item_id, media_type: MediaType):
        """Remove an item from the library."""
        result = False
        try:
            if media_type == MediaType.ARTIST:
                result = await library_add_remove_artist(
                    headers=self._headers,
                    prov_artist_id=prov_item_id,
                    add=False,
                    user=self._yt_user,
                )
            elif media_type == MediaType.ALBUM:
                result = await library_add_remove_album(
                    headers=self._headers, prov_item_id=prov_item_id, add=False, user=self._yt_user
                )
            elif media_type == MediaType.PLAYLIST:
                result = await library_add_remove_playlist(
                    headers=self._headers, prov_item_id=prov_item_id, add=False, user=self._yt_user
                )
            elif media_type == MediaType.TRACK:
                raise NotImplementedError
        except YTMusicServerError as err:
            # YTM raises if trying to remove an item that is not in the library
            raise NotImplementedError(err) from err
        return result

    async def add_playlist_tracks(self, prov_playlist_id: str, prov_track_ids: list[str]) -> None:
        """Add track(s) to playlist."""
        # Grab the playlist id from the full url in case of personal playlists
        if YT_PLAYLIST_ID_DELIMITER in prov_playlist_id:
            prov_playlist_id = prov_playlist_id.split(YT_PLAYLIST_ID_DELIMITER)[0]
        return await add_remove_playlist_tracks(
            headers=self._headers,
            prov_playlist_id=prov_playlist_id,
            prov_track_ids=prov_track_ids,
            add=True,
            user=self._yt_user,
        )

    async def remove_playlist_tracks(
        self, prov_playlist_id: str, positions_to_remove: tuple[int, ...]
    ) -> None:
        """Remove track(s) from playlist."""
        # Grab the playlist id from the full url in case of personal playlists
        if YT_PLAYLIST_ID_DELIMITER in prov_playlist_id:
            prov_playlist_id = prov_playlist_id.split(YT_PLAYLIST_ID_DELIMITER)[0]
        playlist_obj = await get_playlist(prov_playlist_id=prov_playlist_id, headers=self._headers)
        if "tracks" not in playlist_obj:
            return None
        tracks_to_delete = []
        for index, track in enumerate(playlist_obj["tracks"]):
            if index in positions_to_remove:
                # YT needs both the videoId and the setVideoId in order to remove
                # the track. Thus, we need to obtain the playlist details and
                # grab the info from there.
                tracks_to_delete.append(
                    {"videoId": track["videoId"], "setVideoId": track["setVideoId"]}
                )

        return await add_remove_playlist_tracks(
            headers=self._headers,
            prov_playlist_id=prov_playlist_id,
            prov_track_ids=tracks_to_delete,
            add=False,
            user=self._yt_user,
        )

    async def get_similar_tracks(self, prov_track_id, limit=25) -> list[Track]:
        """Retrieve a dynamic list of tracks based on the provided item."""
        result = []
        result = await get_song_radio_tracks(
            headers=self._headers, prov_item_id=prov_track_id, limit=limit, user=self._yt_user
        )
        if "tracks" in result:
            tracks = []
            for track in result["tracks"]:
                # Playlist tracks sometimes do not have a valid artist id
                # In that case, call the API for track details based on track id
                try:
                    track = self._parse_track(track)
                    if track:
                        tracks.append(track)
                except InvalidDataError:
                    if track := await self.get_track(track["videoId"]):
                        tracks.append(track)
            return tracks
        return []

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Return the content details for the given track when it will be streamed."""
        if media_type == MediaType.PODCAST_EPISODE:
            item_id = item_id.split(PODCAST_EPISODE_SPLITTER)[1]
        stream_format = await self._get_stream_format(item_id=item_id)
        self.logger.debug("Found stream_format: %s for song %s", stream_format["format"], item_id)
        stream_details = StreamDetails(
            provider=self.lookup_key,
            item_id=item_id,
            audio_format=AudioFormat(
                content_type=ContentType.try_parse(stream_format["audio_ext"]),
            ),
            stream_type=StreamType.HTTP,
            path=stream_format["url"],
            can_seek=True,
            allow_seek=True,
        )
        if (
            stream_format.get("audio_channels")
            and str(stream_format.get("audio_channels")).isdigit()
        ):
            stream_details.audio_format.channels = int(stream_format.get("audio_channels"))
        if stream_format.get("asr"):
            stream_details.audio_format.sample_rate = int(stream_format.get("asr"))
        return stream_details

    @use_cache(3600)
    async def recommendations(self) -> list[RecommendationFolder]:
        """Get available recommendations."""
        recommendations = await get_home(self._headers, self.language, user=self._yt_user)
        folders = []
        for section in recommendations:
            folder = RecommendationFolder(
                name=section["title"],
                item_id=f"{self.instance_id}_{section['title']}",
                provider=self.lookup_key,
                icon=determine_recommendation_icon(section["title"]),
            )
            for recommended_item in section.get("contents", []):
                if recommended_item.get("videoId"):
                    # Probably a track
                    try:
                        track = self._parse_track(recommended_item)
                        folder.items.append(track)
                    except InvalidDataError:
                        self.logger.debug("Invalid track in recommendations: %s", recommended_item)
                elif recommended_item.get("playlistId"):
                    # Probably a playlist
                    recommended_item["id"] = recommended_item["playlistId"]
                    del recommended_item["playlistId"]
                    folder.items.append(self._parse_playlist(recommended_item))
                elif recommended_item.get("browseId"):
                    # Probably an album
                    folder.items.append(self._parse_album(recommended_item))
                elif recommended_item.get("subscribers"):
                    # Probably artist
                    folder.items.append(self._parse_album(recommended_item))
                else:
                    self.logger.warning(
                        "Unknown item type in recommendation folder: %s", recommended_item
                    )
                    continue
            folders.append(folder)
        return folders

    async def _post_data(self, endpoint: str, data: dict[str, str], **kwargs):
        """Post data to the given endpoint."""
        url = f"{YTM_BASE_URL}{endpoint}"
        data.update(self._context)
        async with self.mass.http_session.post(
            url,
            headers=self._headers,
            json=data,
            ssl=False,
            cookies=self._cookies,
        ) as response:
            return await response.json()

    async def _get_data(self, url: str, params: dict | None = None):
        """Get data from the given URL."""
        async with self.mass.http_session.get(
            url, headers=self._headers, params=params, cookies=self._cookies
        ) as response:
            return await response.text()

    def _initialize_headers(self) -> dict[str, str]:
        """Return headers to include in the requests."""
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:72.0) Gecko/20100101 Firefox/72.0",  # noqa: E501
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.5",
            "Content-Type": "application/json",
            "X-Goog-AuthUser": "0",
            "x-origin": YTM_DOMAIN,
            "Cookie": self._cookie,
        }
        if "__Secure-3PAPISID" not in self._cookie:
            raise LoginFailed(
                "Invalid Cookie detected. Cookie is missing the __Secure-3PAPISID field. "
                "Please ensure you are passing the correct cookie. "
                "You can verify this by checking if the string "
                "'__Secure-3PAPISID' is present in the cookie string."
            )
        sapisid = sapisid_from_cookie(self._cookie)
        headers["Authorization"] = get_authorization(sapisid + " " + YTM_DOMAIN)
        self._headers = headers

    def _initialize_context(self) -> dict[str, str]:
        """Return a dict to use as a context in requests."""
        self._context = {
            "context": {
                "client": {"clientName": "WEB_REMIX", "clientVersion": "0.1"},
                "user": {},
            }
        }

    def _parse_album(self, album_obj: dict, album_id: str | None = None) -> Album:
        """Parse a YT Album response to an Album model object."""
        album_id = album_id or album_obj.get("id") or album_obj.get("browseId")
        if "title" in album_obj:
            name = album_obj["title"]
        elif "name" in album_obj:
            name = album_obj["name"]
        album = Album(
            item_id=album_id,
            name=name,
            provider=self.lookup_key,
            provider_mappings={
                ProviderMapping(
                    item_id=str(album_id),
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url=f"{YTM_DOMAIN}/playlist?list={album_id}",
                )
            },
        )
        if album_obj.get("year") and album_obj["year"].isdigit():
            album.year = album_obj["year"]
        if "thumbnails" in album_obj:
            album.metadata.images = self._parse_thumbnails(album_obj["thumbnails"])
        if description := album_obj.get("description"):
            album.metadata.description = unquote(description)
        if "isExplicit" in album_obj:
            album.metadata.explicit = album_obj["isExplicit"]
        if "artists" in album_obj:
            album.artists = [
                self._get_artist_item_mapping(artist)
                for artist in album_obj["artists"]
                if artist.get("id")
                or artist.get("channelId")
                or artist.get("name") == "Various Artists"
            ]
        if "type" in album_obj:
            if album_obj["type"] == "Single":
                album_type = AlbumType.SINGLE
            elif album_obj["type"] == "EP":
                album_type = AlbumType.EP
            elif album_obj["type"] == "Album":
                album_type = AlbumType.ALBUM
            else:
                album_type = AlbumType.UNKNOWN
            album.album_type = album_type
        return album

    def _parse_artist(self, artist_obj: dict) -> Artist:
        """Parse a YT Artist response to Artist model object."""
        artist_id = None
        if "channelId" in artist_obj:
            artist_id = artist_obj["channelId"]
        elif artist_obj.get("id"):
            artist_id = artist_obj["id"]
        elif artist_obj["name"] == "Various Artists":
            artist_id = VARIOUS_ARTISTS_YTM_ID
        if not artist_id:
            msg = "Artist does not have a valid ID"
            raise InvalidDataError(msg)
        artist = Artist(
            item_id=artist_id,
            name=artist_obj["name"],
            provider=self.lookup_key,
            provider_mappings={
                ProviderMapping(
                    item_id=str(artist_id),
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url=f"{YTM_DOMAIN}/channel/{artist_id}",
                )
            },
        )
        if "description" in artist_obj:
            artist.metadata.description = artist_obj["description"]
        if artist_obj.get("thumbnails"):
            artist.metadata.images = self._parse_thumbnails(artist_obj["thumbnails"])
        return artist

    def _parse_playlist(self, playlist_obj: dict) -> Playlist:
        """Parse a YT Playlist response to a Playlist object."""
        playlist_id = playlist_obj["id"]
        playlist_name = playlist_obj["title"]
        is_editable = playlist_obj.get("privacy", "") == "PRIVATE"
        # Playlist ID's are not unique across instances for lists like 'Likes', 'Supermix', etc.
        # So suffix with the instance id to make them unique
        if playlist_id in YT_PERSONAL_PLAYLISTS:
            playlist_id = f"{playlist_id}{YT_PLAYLIST_ID_DELIMITER}{self.instance_id}"
            playlist_name = f"{playlist_name} ({self.name})"
        playlist = Playlist(
            item_id=playlist_id,
            provider=self.instance_id if is_editable else self.lookup_key,
            name=playlist_name,
            provider_mappings={
                ProviderMapping(
                    item_id=playlist_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url=f"{YTM_DOMAIN}/playlist?list={playlist_id}",
                )
            },
            is_editable=is_editable,
        )
        if "description" in playlist_obj:
            playlist.metadata.description = playlist_obj["description"]
        if playlist_obj.get("thumbnails"):
            playlist.metadata.images = self._parse_thumbnails(playlist_obj["thumbnails"])

        if authors := playlist_obj.get("author"):
            if isinstance(authors, str):
                playlist.owner = authors
            elif isinstance(authors, list):
                playlist.owner = authors[0]["name"]
            else:
                playlist.owner = authors["name"]
        else:
            playlist.owner = self.name
        playlist.cache_checksum = playlist_obj.get("checksum")
        return playlist

    def _parse_track(self, track_obj: dict) -> Track:
        """Parse a YT Track response to a Track model object."""
        if not track_obj.get("videoId"):
            msg = "Track is missing videoId"
            raise InvalidDataError(msg)
        track_id = str(track_obj["videoId"])
        track = Track(
            item_id=track_id,
            provider=self.lookup_key,
            name=track_obj["title"],
            provider_mappings={
                ProviderMapping(
                    item_id=track_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    available=track_obj.get("isAvailable", True),
                    url=f"{YTM_DOMAIN}/watch?v={track_id}",
                    audio_format=AudioFormat(
                        content_type=ContentType.M4A,
                    ),
                )
            },
            disc_number=0,  # not supported on YTM?
            track_number=track_obj.get("trackNumber", 0),
        )

        if track_obj.get("artists"):
            track.artists = [
                self._get_artist_item_mapping(artist)
                for artist in track_obj["artists"]
                if artist.get("id")
                or artist.get("channelId")
                or artist.get("name") == "Various Artists"
            ]
        # guard that track has valid artists
        if not track.artists:
            msg = "Track is missing artists"
            raise InvalidDataError(msg)
        if track_obj.get("thumbnails"):
            track.metadata.images = self._parse_thumbnails(track_obj["thumbnails"])
        if (
            track_obj.get("album")
            and isinstance(track_obj.get("album"), dict)
            and track_obj["album"].get("id")
        ):
            album = track_obj["album"]
            track.album = self._get_item_mapping(MediaType.ALBUM, album["id"], album["name"])
        if "isExplicit" in track_obj:
            track.metadata.explicit = track_obj["isExplicit"]
        if "duration" in track_obj and str(track_obj["duration"]).isdigit():
            track.duration = int(track_obj["duration"])
        elif "duration_seconds" in track_obj and str(track_obj["duration_seconds"]).isdigit():
            track.duration = int(track_obj["duration_seconds"])
        return track

    def _parse_podcast(self, podcast_obj: dict) -> Podcast:
        """Parse a YTM Podcast into a MA Podcast."""
        podcast = Podcast(
            item_id=podcast_obj["podcastId"],
            name=podcast_obj["title"],
            provider=self.lookup_key,
            provider_mappings={
                ProviderMapping(
                    item_id=podcast_obj["podcastId"],
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )
        if description := podcast_obj.get("description"):
            podcast.metadata.description = description
        if author := podcast_obj.get("author"):
            podcast.publisher = author["name"]
        if thumbnails := podcast_obj.get("thumbnails"):
            podcast.metadata.images = self._parse_thumbnails(thumbnails)
        return podcast

    def _parse_podcast_episode(self, episode_obj: dict, podcast: Podcast | None) -> PodcastEpisode:
        """Parse a raw episode into a PodcastEpisode."""
        episode_id = episode_obj.get("videoId")
        if not episode_id:
            msg = "Podcast episode is missing videoId"
            raise InvalidDataError(msg)
        item_id = f"{podcast.item_id}{PODCAST_EPISODE_SPLITTER}{episode_id}"
        episode = PodcastEpisode(
            item_id=item_id,
            provider=self.lookup_key,
            name=episode_obj.get("title"),
            podcast=podcast,
            provider_mappings={
                ProviderMapping(
                    item_id=item_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    audio_format=AudioFormat(
                        content_type=ContentType.M4A,
                    ),
                    url=f"{YTM_DOMAIN}/watch?v={episode_id}",
                )
            },
        )
        if duration := episode_obj.get("duration"):
            duration_sec = parse_str_duration(duration)
            episode.duration = duration_sec
        if description := episode_obj.get("description"):
            episode.metadata.description = description
        if thumbnails := episode_obj.get("thumbnails"):
            episode.metadata.images = self._parse_thumbnails(thumbnails)
        if release_date := episode_obj.get("date"):
            with suppress(ValueError):
                episode.metadata.release_date = datetime.fromisoformat(release_date)
        return episode

    async def _get_stream_format(self, item_id: str) -> dict[str, Any]:
        """Figure out the stream URL to use and return the highest quality."""

        def _extract_best_stream_url_format() -> dict[str, Any]:
            url = f"{YTM_DOMAIN}/watch?v={item_id}"
            ydl_opts = {
                "quiet": self.logger.level > logging.DEBUG,
                "verbose": self.logger.level == VERBOSE_LOG_LEVEL,
                "cookiefile": StringIO(self._netscape_cookie),
                # This enforces a player client and skips unnecessary scraping to increase speed
                "extractor_args": {
                    "youtubepot-bgutilhttp": {
                        "base_url": [self._po_token_server_url],
                    },
                    "youtube": {
                        "skip": ["translated_subs", "dash"],
                        "player_client": ["web_music"],
                        "player_skip": ["webpage"],
                    },
                },
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                try:
                    info = ydl.extract_info(url, download=False)
                except yt_dlp.utils.DownloadError as err:
                    raise UnplayableMediaError(err) from err
                format_selector = ydl.build_format_selector("m4a/bestaudio")
                if not (stream_format := next(format_selector({"formats": info["formats"]})), None):
                    raise UnplayableMediaError("No stream formats found")
                return stream_format

        return await asyncio.to_thread(_extract_best_stream_url_format)

    def _get_item_mapping(self, media_type: MediaType, key: str, name: str) -> ItemMapping:
        return ItemMapping(
            media_type=media_type,
            item_id=key,
            provider=self.lookup_key,
            name=name,
        )

    def _get_artist_item_mapping(self, artist_obj: dict) -> ItemMapping:
        artist_id = artist_obj.get("id") or artist_obj.get("channelId")
        if not artist_id and artist_obj["name"] == "Various Artists":
            artist_id = VARIOUS_ARTISTS_YTM_ID
        return self._get_item_mapping(MediaType.ARTIST, artist_id, artist_obj.get("name"))

    async def _verify_po_token_url(self) -> bool:
        """Ping the PO Token server and verify the response."""
        url = f"{self._po_token_server_url}/ping"
        try:
            async with self.mass.http_session.get(url) as response:
                response.raise_for_status()
                self.logger.debug("PO Token server responded with %s", response.status)
                return response.status == 200
        except ClientConnectorError:
            return False

    async def _user_has_ytm_premium(self) -> bool:
        """Check if the user has Youtube Music Premium."""
        stream_format = await self._get_stream_format(YTM_PREMIUM_CHECK_TRACK_ID)
        # Only premium users can stream the HQ stream of this song
        return stream_format["format_id"] == "141"

    def _parse_thumbnails(self, thumbnails_obj: dict) -> list[MediaItemImage]:
        """Parse and YTM thumbnails to MediaItemImage."""
        result: list[MediaItemImage] = []
        processed_images = set()
        for img in sorted(thumbnails_obj, key=lambda w: w.get("width", 0), reverse=True):
            url: str = img["url"]
            url_base = url.split("=w")[0]
            width: int = img["width"]
            height: int = img["height"]
            image_ratio: float = width / height
            image_type = (
                ImageType.LANDSCAPE
                if "maxresdefault" in url or image_ratio > 2.0
                else ImageType.THUMB
            )
            if "=w" not in url and width < 500:
                continue
            # if the size is in the url, we can actually request a higher thumb
            if "=w" in url and width < 600:
                url = f"{url_base}=w600-h600-p"
                image_type = ImageType.THUMB
            if (url_base, image_type) in processed_images:
                continue
            processed_images.add((url_base, image_type))
            result.append(
                MediaItemImage(
                    type=image_type,
                    path=url,
                    provider=self.lookup_key,
                    remotely_accessible=True,
                )
            )
        return result
