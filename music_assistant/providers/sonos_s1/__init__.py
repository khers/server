"""
Sonos Player S1 provider for Music Assistant.

Based on the SoCo library for Sonos which uses the legacy/V1 UPnP API.

Note that large parts of this code are copied over from the Home Assistant
integration for Sonos.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from music_assistant_models.config_entries import ConfigEntry, ConfigValueType
from music_assistant_models.enums import (
    ConfigEntryType,
    PlayerFeature,
    PlayerState,
    PlayerType,
    ProviderFeature,
)
from music_assistant_models.errors import PlayerCommandFailed, PlayerUnavailableError
from music_assistant_models.player import DeviceInfo, Player, PlayerMedia
from requests.exceptions import RequestException
from soco import SoCo, events_asyncio, zonegroupstate
from soco import config as soco_config
from soco.discovery import discover, scan_network

from music_assistant.constants import (
    CONF_ENTRY_FLOW_MODE_HIDDEN_DISABLED,
    CONF_ENTRY_HTTP_PROFILE_DEFAULT_1,
    CONF_ENTRY_MANUAL_DISCOVERY_IPS,
    CONF_ENTRY_OUTPUT_CODEC,
    VERBOSE_LOG_LEVEL,
    create_sample_rates_config_entry,
)
from music_assistant.helpers.upnp import create_didl_metadata
from music_assistant.models.player_provider import PlayerProvider

from .player import SonosPlayer

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


PLAYER_FEATURES = {
    PlayerFeature.SET_MEMBERS,
    PlayerFeature.VOLUME_MUTE,
    PlayerFeature.PAUSE,
    PlayerFeature.ENQUEUE,
    PlayerFeature.GAPLESS_PLAYBACK,
    PlayerFeature.GAPLESS_DIFFERENT_SAMPLERATE,
}

CONF_NETWORK_SCAN = "network_scan"
CONF_HOUSEHOLD_ID = "household_id"
SUBSCRIPTION_TIMEOUT = 1200
ZGS_SUBSCRIPTION_TIMEOUT = 2

CONF_ENTRY_SAMPLE_RATES = create_sample_rates_config_entry(
    max_sample_rate=48000, max_bit_depth=16, hidden=True
)


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    soco_config.EVENTS_MODULE = events_asyncio
    zonegroupstate.EVENT_CACHE_TIMEOUT = SUBSCRIPTION_TIMEOUT
    prov = SonosPlayerProvider(mass, manifest, config)
    # set-up soco logging
    if prov.logger.isEnabledFor(VERBOSE_LOG_LEVEL):
        logging.getLogger("soco").setLevel(logging.DEBUG)
    else:
        logging.getLogger("soco").setLevel(prov.logger.level + 10)
    await prov.handle_async_init()
    return prov


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to setup this provider.

    instance_id: id of an existing provider instance (None if new instance setup).
    action: [optional] action key called from config entries UI.
    values: the (intermediate) raw values for config entries sent with the action.
    """
    # ruff: noqa: ARG001
    household_ids = await discover_household_ids(mass)
    return (
        ConfigEntry(
            key=CONF_NETWORK_SCAN,
            type=ConfigEntryType.BOOLEAN,
            label="Enable network scan for discovery",
            default_value=False,
            description="Enable network scan for discovery of players. \n"
            "Can be used if (some of) your players are not automatically discovered.\n"
            "Should normally not be needed",
        ),
        ConfigEntry(
            key=CONF_HOUSEHOLD_ID,
            type=ConfigEntryType.STRING,
            label="Household ID",
            default_value=household_ids[0] if household_ids else None,
            description="Household ID for the Sonos (S1) system. Will be auto detected if empty.",
            category="advanced",
            required=False,
        ),
        CONF_ENTRY_MANUAL_DISCOVERY_IPS,
    )


@dataclass
class UnjoinData:
    """Class to track data necessary for unjoin coalescing."""

    players: list[SonosPlayer]
    event: asyncio.Event = field(default_factory=asyncio.Event)


class SonosPlayerProvider(PlayerProvider):
    """Sonos Player provider."""

    _discovery_running: bool = False
    _discovery_reschedule_timer: asyncio.TimerHandle | None = None

    def __init__(self, mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig):
        """Handle initialization of the provider."""
        super().__init__(mass, manifest, config)
        self.sonosplayers: OrderedDict[str, SonosPlayer] = OrderedDict()

    @property
    def supported_features(self) -> set[ProviderFeature]:
        """Return the features supported by this Provider."""
        return {ProviderFeature.SYNC_PLAYERS}

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self.topology_condition = asyncio.Condition()
        self.boot_counts: dict[str, int] = {}
        self.mdns_names: dict[str, str] = {}
        self.unjoin_data: dict[str, UnjoinData] = {}
        self._discovery_running = False
        self.hosts_in_error: dict[str, bool] = {}
        self.discovery_lock = asyncio.Lock()
        self.creation_lock = asyncio.Lock()
        self._known_invisible: set[SoCo] = set()

    async def unload(self, is_removed: bool = False) -> None:
        """Handle close/cleanup of the provider."""
        if self._discovery_reschedule_timer:
            self._discovery_reschedule_timer.cancel()
            self._discovery_reschedule_timer = None
        # await any in-progress discovery
        while self._discovery_running:
            await asyncio.sleep(0.5)
        await asyncio.gather(*(player.offline() for player in self.sonosplayers.values()))
        if events_asyncio.event_listener:
            await events_asyncio.event_listener.async_stop()
        self.sonosplayers = OrderedDict()

    async def get_player_config_entries(
        self,
        player_id: str,
    ) -> tuple[ConfigEntry, ...]:
        """Return Config Entries for the given player."""
        base_entries = await super().get_player_config_entries(player_id)
        return (
            *base_entries,
            CONF_ENTRY_SAMPLE_RATES,
            CONF_ENTRY_OUTPUT_CODEC,
            CONF_ENTRY_FLOW_MODE_HIDDEN_DISABLED,
            CONF_ENTRY_HTTP_PROFILE_DEFAULT_1,
        )

    def is_device_invisible(self, ip_address: str) -> bool:
        """Check if device at provided IP is known to be invisible."""
        return any(x for x in self._known_invisible if x.ip_address == ip_address)

    async def cmd_stop(self, player_id: str) -> None:
        """Send STOP command to given player."""
        sonos_player = self.sonosplayers[player_id]
        if sonos_player.sync_coordinator:
            self.logger.debug(
                "Ignore STOP command for %s: Player is synced to another player.",
                player_id,
            )
            return
        await asyncio.to_thread(sonos_player.soco.stop)
        self.mass.call_later(2, sonos_player.poll_speaker)

    async def cmd_play(self, player_id: str) -> None:
        """Send PLAY command to given player."""
        sonos_player = self.sonosplayers[player_id]
        if sonos_player.sync_coordinator:
            self.logger.debug(
                "Ignore PLAY command for %s: Player is synced to another player.",
                player_id,
            )
            return
        await asyncio.to_thread(sonos_player.soco.play)
        sonos_player.mass_player.poll_interval = 5
        self.mass.call_later(2, sonos_player.poll_speaker)

    async def cmd_pause(self, player_id: str) -> None:
        """Send PAUSE command to given player."""
        sonos_player = self.sonosplayers[player_id]
        if sonos_player.sync_coordinator:
            self.logger.debug(
                "Ignore PLAY command for %s: Player is synced to another player.",
                player_id,
            )
            return
        if "Pause" not in sonos_player.soco.available_actions:
            # pause not possible
            await self.cmd_stop(player_id)
            return
        await asyncio.to_thread(sonos_player.soco.pause)
        self.mass.call_later(2, sonos_player.poll_speaker)

    async def cmd_volume_set(self, player_id: str, volume_level: int) -> None:
        """Send VOLUME_SET command to given player."""
        sonos_player = self.sonosplayers[player_id]

        def set_volume_level(player_id: str, volume_level: int) -> None:
            sonos_player.soco.volume = volume_level

        await asyncio.to_thread(set_volume_level, player_id, volume_level)
        self.mass.call_later(2, sonos_player.poll_speaker)

    async def cmd_volume_mute(self, player_id: str, muted: bool) -> None:
        """Send VOLUME MUTE command to given player."""

        def set_volume_mute(player_id: str, muted: bool) -> None:
            sonos_player = self.sonosplayers[player_id]
            sonos_player.soco.mute = muted

        await asyncio.to_thread(set_volume_mute, player_id, muted)

    async def cmd_group(self, player_id: str, target_player: str) -> None:
        """Handle GROUP command for given player.

        Join/add the given player(id) to the given (master) player/sync group.

            - player_id: player_id of the player to handle the command.
            - target_player: player_id of the syncgroup master or group player.
        """
        sonos_player = self.sonosplayers[player_id]
        sonos_master_player = self.sonosplayers[target_player]
        await sonos_master_player.join([sonos_player])
        self.mass.call_later(2, sonos_player.poll_speaker)

    async def cmd_ungroup(self, player_id: str) -> None:
        """Handle UNGROUP command for given player.

        Remove the given player from any (sync)groups it currently is grouped to.

            - player_id: player_id of the player to handle the command.
        """
        sonos_player = self.sonosplayers[player_id]
        await sonos_player.unjoin()
        self.mass.call_later(2, sonos_player.poll_speaker)

    async def play_media(
        self,
        player_id: str,
        media: PlayerMedia,
    ) -> None:
        """Handle PLAY MEDIA on given player."""
        sonos_player = self.sonosplayers[player_id]
        mass_player = self.mass.players.get(player_id)
        assert mass_player
        if sonos_player.sync_coordinator:
            # this should be already handled by the player manager, but just in case...
            msg = (
                f"Player {mass_player.display_name} can not "
                "accept play_media command, it is synced to another player."
            )
            raise PlayerCommandFailed(msg)
        didl_metadata = create_didl_metadata(media)
        await asyncio.to_thread(sonos_player.soco.play_uri, media.uri, meta=didl_metadata)
        self.mass.call_later(2, sonos_player.poll_speaker)
        sonos_player.mass_player.poll_interval = 5

    async def enqueue_next_media(self, player_id: str, media: PlayerMedia) -> None:
        """Handle enqueuing of the next queue item on the player."""
        sonos_player = self.sonosplayers[player_id]
        didl_metadata = create_didl_metadata(media)

        # disable crossfade mode if needed
        # crossfading is handled by our streams controller
        if sonos_player.crossfade:

            def set_crossfade() -> None:
                try:
                    sonos_player.soco.cross_fade = False
                    sonos_player.crossfade = False
                except Exception as err:
                    self.logger.warning(
                        "Unable to set crossfade for player %s: %s", sonos_player.zone_name, err
                    )

            await asyncio.to_thread(set_crossfade)

        try:
            await asyncio.to_thread(
                sonos_player.soco.avTransport.SetNextAVTransportURI,
                [("InstanceID", 0), ("NextURI", media.uri), ("NextURIMetaData", didl_metadata)],
                timeout=60,
            )
        except Exception as err:
            self.logger.warning(
                "Unable to enqueue next track on player: %s: %s", sonos_player.zone_name, err
            )

    async def poll_player(self, player_id: str) -> None:
        """Poll player for state updates."""
        if player_id not in self.sonosplayers:
            return
        sonos_player = self.sonosplayers[player_id]
        # dynamically change the poll interval
        if sonos_player.mass_player.state == PlayerState.PLAYING:
            sonos_player.mass_player.poll_interval = 5
        else:
            sonos_player.mass_player.poll_interval = 30
        try:
            # the check_poll logic will work out what endpoints need polling now
            # based on when we last received info from the device
            if needs_poll := await sonos_player.check_poll():
                await sonos_player.poll_speaker()
            # always update the attributes
            sonos_player.update_player(signal_update=needs_poll)
        except ConnectionResetError as err:
            raise PlayerUnavailableError from err

    async def discover_players(self) -> None:
        """Discover Sonos players on the network."""
        if self._discovery_running:
            return

        # Handle config option for manual IP's
        manual_ip_config = cast(
            "list[str]", self.config.get_value(CONF_ENTRY_MANUAL_DISCOVERY_IPS.key)
        )
        for ip_address in manual_ip_config:
            try:
                player = SoCo(ip_address)
                self._add_player(player)
            except RequestException as err:
                # player is offline
                self.logger.debug("Failed to add SonosPlayer %s: %s", player, err)
            except Exception as err:
                self.logger.warning(
                    "Failed to add SonosPlayer %s: %s",
                    player,
                    err,
                    exc_info=err if self.logger.isEnabledFor(10) else None,
                )

        allow_network_scan = self.config.get_value(CONF_NETWORK_SCAN)
        if not (household_id := self.config.get_value(CONF_HOUSEHOLD_ID)):
            household_id = "Sonos"

        def do_discover() -> None:
            """Run discovery and add players in executor thread."""
            self._discovery_running = True
            try:
                self.logger.debug("Sonos discovery started...")
                discovered_devices: set[SoCo] = (
                    discover(
                        timeout=30, household_id=household_id, allow_network_scan=allow_network_scan
                    )
                    or set()
                )

                # process new players
                for soco in discovered_devices:
                    try:
                        self._add_player(soco)
                    except RequestException as err:
                        # player is offline
                        self.logger.debug("Failed to add SonosPlayer %s: %s", soco, err)
                    except Exception as err:
                        self.logger.warning(
                            "Failed to add SonosPlayer %s: %s",
                            soco,
                            err,
                            exc_info=err if self.logger.isEnabledFor(10) else None,
                        )
            finally:
                self._discovery_running = False

        await asyncio.to_thread(do_discover)

        def reschedule() -> None:
            self._discovery_reschedule_timer = None
            self.mass.create_task(self.discover_players())

        # reschedule self once finished
        self._discovery_reschedule_timer = self.mass.loop.call_later(1800, reschedule)

    def _add_player(self, soco: SoCo) -> None:
        """Add discovered Sonos player."""
        player_id = soco.uid
        # check if existing player changed IP
        if existing := self.sonosplayers.get(player_id):
            if existing.soco.ip_address != soco.ip_address:
                existing.update_ip(soco.ip_address)
            return
        if not soco.is_visible:
            return
        enabled = self.mass.config.get_raw_player_config_value(player_id, "enabled", True)
        if not enabled:
            self.logger.debug("Ignoring disabled player: %s", player_id)
            return

        speaker_info = soco.get_speaker_info(True, timeout=7)
        if soco.uid not in self.boot_counts:
            self.boot_counts[soco.uid] = soco.boot_seqnum
        self.logger.debug("Adding new player: %s", speaker_info)
        if not (mass_player := self.mass.players.get(soco.uid)):
            mass_player = Player(
                player_id=soco.uid,
                provider=self.instance_id,
                type=PlayerType.PLAYER,
                name=soco.player_name,
                available=True,
                supported_features=PLAYER_FEATURES,
                device_info=DeviceInfo(
                    model=speaker_info["model_name"],
                    ip_address=soco.ip_address,
                    manufacturer="SONOS",
                ),
                needs_poll=True,
                poll_interval=30,
                can_group_with={self.instance_id},
            )
        self.sonosplayers[player_id] = sonos_player = SonosPlayer(
            self,
            soco=soco,
            mass_player=mass_player,
        )
        if not soco.fixed_volume:
            mass_player.supported_features = {
                *mass_player.supported_features,
                PlayerFeature.VOLUME_SET,
            }
        asyncio.run_coroutine_threadsafe(
            self.mass.players.register_or_update(sonos_player.mass_player), loop=self.mass.loop
        )


async def discover_household_ids(mass: MusicAssistant, prefer_s1: bool = True) -> list[str]:
    """Discover the HouseHold ID of S1 speaker(s) the network."""
    if cache := await mass.cache.get("sonos_household_ids"):
        return cast("list[str]", cache)
    household_ids: list[str] = []

    def get_all_sonos_ips() -> set[SoCo]:
        """Run full network discovery and return IP's of all devices found on the network."""
        discovered_zones: set[SoCo] | None
        if discovered_zones := scan_network(multi_household=True):
            return {zone.ip_address for zone in discovered_zones}
        return set()

    all_sonos_ips = await asyncio.to_thread(get_all_sonos_ips)
    for ip_address in all_sonos_ips:
        async with mass.http_session.get(f"http://{ip_address}:1400/status/zp") as resp:
            if resp.status == 200:
                data = await resp.text()
                if prefer_s1 and "<SWGen>2</SWGen>" in data:
                    continue
                if "HouseholdControlID" in data:
                    household_id = data.split("<HouseholdControlID>")[1].split(
                        "</HouseholdControlID>"
                    )[0]
                    household_ids.append(household_id)
    await mass.cache.set("sonos_household_ids", household_ids, 3600)
    return household_ids
