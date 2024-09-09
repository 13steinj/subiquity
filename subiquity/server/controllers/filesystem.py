# Copyright 2015 Canonical, Ltd.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import asyncio
import functools
import glob
import json
import logging
import os
import pathlib
import subprocess
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Union

import attr
import pyudev
import requests
from curtin import swap
from curtin.commands.extract import AbstractSourceHandler
from curtin.storage_config import ptable_part_type_to_flag
from curtin.util import human2bytes

from subiquity.common.apidef import API
from subiquity.common.errorreport import ErrorReportKind
from subiquity.common.filesystem import boot, gaps, labels, sizes
from subiquity.common.filesystem.actions import DeviceAction
from subiquity.common.filesystem.manipulator import FilesystemManipulator
from subiquity.common.types import (
    AddPartitionV2,
    Bootloader,
    Disk,
    GuidedCapability,
    GuidedChoiceV2,
    GuidedDisallowedCapability,
    GuidedDisallowedCapabilityReason,
    GuidedStorageResponseV2,
    GuidedStorageTarget,
    GuidedStorageTargetManual,
    GuidedStorageTargetReformat,
    GuidedStorageTargetResize,
    GuidedStorageTargetUseGap,
    ModifyPartitionV2,
    ProbeStatus,
    RecoveryKey,
    ReformatDisk,
    SizingPolicy,
    StorageResponse,
    StorageResponseV2,
)
from subiquity.models.filesystem import (
    LVM_CHUNK_SIZE,
    ActionRenderMode,
    ArbitraryDevice,
)
from subiquity.models.filesystem import Disk as ModelDisk
from subiquity.models.filesystem import (
    MatchDirective,
    MiB,
    Partition,
    Raid,
    RecoveryKeyHandler,
    _Device,
    align_down,
    align_up,
    humanize_size,
)
from subiquity.server import snapdapi
from subiquity.server.autoinstall import AutoinstallError
from subiquity.server.controller import SubiquityController
from subiquity.server.controllers.source import SEARCH_DRIVERS_AUTOINSTALL_DEFAULT
from subiquity.server.mounter import Mounter
from subiquity.server.snapdapi import (
    StorageEncryptionSupport,
    StorageSafety,
    SystemDetails,
)
from subiquity.server.types import InstallerChannels
from subiquitycore.async_helpers import (
    SingleInstanceTask,
    TaskAlreadyRunningError,
    schedule_task,
)
from subiquitycore.context import with_context
from subiquitycore.lsb_release import lsb_release
from subiquitycore.utils import arun_command, gen_zsys_uuid, run_command

log = logging.getLogger("subiquity.server.controllers.filesystem")
block_discover_log = logging.getLogger("block-discover")


# for translators: 'reason' is the reason FDE is unavailable.
system_defective_encryption_text = _(
    "TPM backed full-disk encryption is not available "
    'on this device (the reason given was "{reason}").'
)

system_multiple_volumes_text = _(
    "TPM backed full-disk encryption is not yet supported when "
    "the target spans multiple volumes."
)

system_non_gpt_text = _(
    "TPM backed full-disk encryption is only supported with a target volume "
    "partition table of GPT."
)


DRY_RUN_RESET_SIZE = 500 * MiB


class NoSnapdSystemsOnSource(Exception):
    pass


@attr.s(auto_attribs=True)
class CapabilityInfo:
    allowed: List[GuidedCapability] = attr.Factory(list)
    disallowed: List[GuidedDisallowedCapability] = attr.Factory(list)

    def combine(self, other: "CapabilityInfo") -> None:
        for allowed_cap in other.allowed:
            if allowed_cap not in self.allowed:
                self.allowed.append(allowed_cap)
        seen_disallowed = set()
        new_disallowed = []
        for disallowed_cap in self.disallowed + other.disallowed:
            if disallowed_cap.capability in self.allowed:
                continue
            if disallowed_cap.capability in seen_disallowed:
                continue
            new_disallowed.append(disallowed_cap)
            seen_disallowed.add(disallowed_cap.capability)
        self.disallowed = new_disallowed
        self.allowed.sort()
        self.disallowed.sort()

    def copy(self) -> "CapabilityInfo":
        return CapabilityInfo(
            allowed=list(self.allowed), disallowed=list(self.disallowed)
        )

    def disallow_if(
        self,
        filter: Callable[[GuidedCapability], bool],
        reason: GuidedDisallowedCapabilityReason,
        message: Optional[str] = None,
    ) -> None:
        new_allowed = []
        for cap in self.allowed:
            if filter(cap):
                self.disallowed.append(
                    GuidedDisallowedCapability(
                        capability=cap,
                        reason=reason,
                        message=message,
                    )
                )
            else:
                new_allowed.append(cap)
        self.allowed = new_allowed

    def disallow_all(
        self,
        reason: GuidedDisallowedCapabilityReason,
        message: Optional[str] = None,
    ) -> None:
        self.disallow_if(lambda cap: True, reason, message)


@attr.s(auto_attribs=True)
class VariationInfo:
    name: str
    label: Optional[str]
    capability_info: CapabilityInfo = attr.Factory(CapabilityInfo)
    min_size: Optional[int] = None
    system: Optional[SystemDetails] = None

    def is_core_boot_classic(self) -> bool:
        return self.label is not None

    def is_valid(self) -> bool:
        return bool(self.capability_info.allowed)

    def capability_info_for_gap(
        self,
        gap: gaps.Gap,
        install_min: int,
    ) -> CapabilityInfo:
        if gap is None:
            gap_size = 0
        else:
            gap_size = gap.size
        r = self.capability_info.copy()
        if gap_size < install_min:
            r.disallow_all(
                reason=GuidedDisallowedCapabilityReason.TOO_SMALL,
            )
        return r

    @classmethod
    def classic(cls, name: str, min_size: int):
        return cls(
            name=name,
            label=None,
            min_size=min_size,
            capability_info=CapabilityInfo(
                allowed=[
                    GuidedCapability.DIRECT,
                    GuidedCapability.LVM,
                    GuidedCapability.LVM_LUKS,
                    GuidedCapability.ZFS,
                    GuidedCapability.ZFS_LUKS_KEYSTORE,
                ]
            ),
        )

    @classmethod
    def dd(cls, name: str, min_size: int):
        return cls(
            name=name,
            label=None,
            min_size=min_size,
            capability_info=CapabilityInfo(
                allowed=[
                    GuidedCapability.DD,
                ]
            ),
        )


class FilesystemController(SubiquityController, FilesystemManipulator):
    endpoint = API.storage

    autoinstall_key = "storage"
    autoinstall_schema = {"type": "object"}  # ...
    model_name = "filesystem"

    _configured = False

    def __init__(self, app):
        self.ai_data = {}
        super().__init__(app)
        self.model.target = app.base_model.target
        if self.opts.dry_run and self.opts.bootloader:
            name = self.opts.bootloader.upper()
            self.model.bootloader = getattr(Bootloader, name)
        self.model.storage_version = self.opts.storage_version
        self._monitor = None
        self._errors = {}
        self._probe_once_task = SingleInstanceTask(
            self._probe_once, propagate_errors=False
        )
        self._probe_task = SingleInstanceTask(
            self._probe, propagate_errors=False, cancel_restart=False
        )
        self._examine_systems_task = SingleInstanceTask(self._examine_systems)
        self.supports_resilient_boot = False
        self.app.hub.subscribe(
            (InstallerChannels.CONFIGURED, "source"),
            self._examine_systems_task.start_sync,
        )
        self.app.hub.subscribe(InstallerChannels.PRE_SHUTDOWN, self._pre_shutdown)
        self._variation_info: Dict[str, VariationInfo] = {}
        self._info: Optional[VariationInfo] = None
        self._on_volume: Optional[snapdapi.OnVolume] = None
        self._source_handler: Optional[AbstractSourceHandler] = None
        self._system_mounter: Optional[Mounter] = None
        self._role_to_device: Dict[str, _Device] = {}
        self._device_to_structure: Dict[_Device, snapdapi.OnVolume] = {}
        self._pyudev_context: Optional[pyudev.Context] = None
        self.use_tpm: bool = False
        self.locked_probe_data: bool = False
        # If probe data come in while we are doing partitioning, store it in
        # this variable. It will be picked up on next reset.
        self.queued_probe_data: Optional[Dict[str, Any]] = None
        self.reset_partition_only: bool = False

    def is_core_boot_classic(self):
        return self._info.is_core_boot_classic()

    def load_autoinstall_data(self, data):
        # Log disabled to prevent LUKS password leak
        # log.debug("load_autoinstall_data %s", data)
        # log.debug("self.ai_data = %s", data)
        self.ai_data = data

    # The identity and user-data section are optional if we are only installing
    # the reset partition, however the identity controller needs to know this
    # before the filesystem controller naturally sets this. So this is a
    # function to inspect the outcome early.
    # See: https://github.com/canonical/subiquity/pull/1965
    def is_reset_partition_only(self):
        storage_config = self.app.autoinstall_config.get(self.autoinstall_key, {})
        layout = storage_config.get("layout", {})
        return layout.get("reset-partition-only", False)

    async def configured(self):
        self._configured = True
        if self._info is None:
            self.set_info_for_capability(GuidedCapability.DIRECT)
        if (
            self.app.base_model.source.search_drivers
            is SEARCH_DRIVERS_AUTOINSTALL_DEFAULT
        ):
            self.app.base_model.source.search_drivers = not self.is_core_boot_classic()
        await super().configured()
        self.stop_monitor()

    async def _mount_systems_dir(self, variation_name):
        self._source_handler = self.app.controllers.Source.get_handler(variation_name)
        source_path = self._source_handler.setup()
        cur_systems_dir = "/var/lib/snapd/seed/systems"
        source_systems_dir = os.path.join(source_path, cur_systems_dir[1:])
        if self.app.opts.dry_run:
            systems_dir_exists = self.app.dr_cfg.systems_dir_exists
        else:
            systems_dir_exists = pathlib.Path(source_systems_dir).is_dir()
        if not systems_dir_exists:
            raise NoSnapdSystemsOnSource
        self._system_mounter = Mounter(self.app)
        if not self.app.opts.dry_run:
            await self._system_mounter.bind_mount_tree(
                source_systems_dir, cur_systems_dir
            )

        cur_snaps_dir = "/var/lib/snapd/seed/snaps"
        source_snaps_dir = os.path.join(source_path, cur_snaps_dir[1:])
        if not self.app.opts.dry_run:
            await self._system_mounter.bind_mount_tree(source_snaps_dir, cur_snaps_dir)

    async def _unmount_systems_dir(self):
        if self._system_mounter is not None:
            await self._system_mounter.cleanup()
            self._system_mounter = None
        if self._source_handler is not None:
            self._source_handler.cleanup()
            self._source_handler = None

    async def _get_system(self, variation_name, label):
        try:
            await self._mount_systems_dir(variation_name)
        except NoSnapdSystemsOnSource:
            return None
        try:
            system = await self.app.snapdapi.v2.systems[label].GET()
        except requests.exceptions.HTTPError as http_err:
            log.warning("v2/systems/%s returned %s", label, http_err.response.text)
            raise
        finally:
            await self._unmount_systems_dir()
        log.debug("got system %s", system)
        return system

    def info_for_system(self, name: str, label: str, system: SystemDetails):
        if len(system.volumes) > 1:
            log.error("Skipping uninstallable system: %s", system_multiple_volumes_text)
            return None

        [volume] = system.volumes.values()
        if volume.schema != "gpt":
            log.error("Skipping uninstallable system: %s", system_non_gpt_text)
            return None

        info = VariationInfo(
            name=name,
            label=label,
            system=system,
        )

        def disallowed_encryption(msg):
            GCDR = GuidedDisallowedCapabilityReason
            reason = GCDR.CORE_BOOT_ENCRYPTION_UNAVAILABLE
            return GuidedDisallowedCapability(
                capability=GuidedCapability.CORE_BOOT_ENCRYPTED,
                reason=reason,
                message=msg,
            )

        se = system.storage_encryption
        if se.support == StorageEncryptionSupport.DEFECTIVE:
            info.capability_info.disallowed = [
                disallowed_encryption(se.unavailable_reason)
            ]
            return info

        offsets_and_sizes = list(self._offsets_and_sizes_for_volume(volume))
        _structure, last_offset, last_size = offsets_and_sizes[-1]
        info.min_size = last_offset + last_size

        if se.support == StorageEncryptionSupport.DISABLED:
            info.capability_info.allowed = [GuidedCapability.CORE_BOOT_UNENCRYPTED]
            msg = _("TPM backed full-disk encryption has been disabled")
            info.capability_info.disallowed = [disallowed_encryption(msg)]
        elif se.support == StorageEncryptionSupport.UNAVAILABLE:
            info.capability_info.allowed = [GuidedCapability.CORE_BOOT_UNENCRYPTED]
            info.capability_info.disallowed = [
                disallowed_encryption(se.unavailable_reason)
            ]
        else:
            if se.storage_safety == StorageSafety.ENCRYPTED:
                info.capability_info.allowed = [GuidedCapability.CORE_BOOT_ENCRYPTED]
            elif se.storage_safety == StorageSafety.PREFER_ENCRYPTED:
                info.capability_info.allowed = [
                    GuidedCapability.CORE_BOOT_PREFER_ENCRYPTED
                ]
            elif se.storage_safety == StorageSafety.PREFER_UNENCRYPTED:
                info.capability_info.allowed = [
                    GuidedCapability.CORE_BOOT_PREFER_UNENCRYPTED
                ]

        return info

    async def _examine_systems(self):
        self._variation_info.clear()
        catalog_entry = self.app.base_model.source.current
        for name, variation in catalog_entry.variations.items():
            system = None
            label = variation.snapd_system_label
            if label is not None:
                system = await self._get_system(name, label)
            log.debug("got system %s for variation %s", system, name)
            if system is not None and len(system.volumes) > 0:
                if not self.app.opts.enhanced_secureboot:
                    log.debug("Not offering enhanced_secureboot: commandline disabled")
                    continue
                info = self.info_for_system(name, label, system)
                if info is None:
                    continue
                if self.model.bootloader != Bootloader.UEFI:
                    log.debug(
                        "Disabling core boot based install options on non-UEFI "
                        "system"
                    )
                    info.capability_info.disallow_if(
                        lambda cap: cap.is_core_boot(),
                        GuidedDisallowedCapabilityReason.NOT_UEFI,
                        _(
                            "Enhanced secure boot options only available on UEFI "
                            "systems."
                        ),
                    )
                search_drivers = self.app.base_model.source.search_drivers
                if (
                    search_drivers is not SEARCH_DRIVERS_AUTOINSTALL_DEFAULT
                    and search_drivers
                ):
                    log.debug(
                        "Disabling core boot based install options as third-party "
                        "drivers selected"
                    )
                    info.capability_info.disallow_if(
                        lambda cap: cap.is_core_boot(),
                        GuidedDisallowedCapabilityReason.THIRD_PARTY_DRIVERS,
                        _(
                            "Enhanced secure boot options cannot currently install "
                            "third party drivers."
                        ),
                    )
                self._variation_info[name] = info
            elif catalog_entry.type.startswith("dd-"):
                min_size = variation.size
                self._variation_info[name] = VariationInfo.dd(
                    name=name, min_size=min_size
                )
            else:
                self._variation_info[name] = VariationInfo.classic(
                    name=name, min_size=variation.size
                )

    @with_context()
    async def apply_autoinstall_config(self, context=None):
        await self._start_task
        await self._probe_task.wait()
        await self._examine_systems_task.wait()
        if False in self._errors:
            raise self._errors[False][0]
        if True in self._errors:
            raise self._errors[True][0]
        if self.ai_data is None:
            # If there are any classic variations, we default that.
            if any(
                not variation.is_core_boot_classic()
                for variation in self._variation_info.values()
                if variation.is_valid()
            ):
                self.ai_data = {
                    "layout": {
                        "name": "lvm",
                    },
                }
            else:
                self.ai_data = {
                    "layout": {
                        "name": "hybrid",
                    },
                }
        await self.convert_autoinstall_config(context=context)
        if self.reset_partition_only:
            return
        if not self.model.is_root_mounted():
            raise Exception("autoinstall config did not mount root")
        if self.model.needs_bootloader_partition():
            raise Exception(
                "autoinstall config did not create needed bootloader partition"
            )

    def update_devices(self, device_map):
        for action in self.model._actions:
            path = device_map.get(action.id)
            if path is not None:
                log.debug("recording path %r for device %s", path, action.id)
                action.path = path
                if action in self._device_to_structure:
                    self._device_to_structure[action].device = path

    def guided_direct(self, gap):
        spec = dict(fstype="ext4", mount="/")
        self.create_partition(device=gap.device, gap=gap, spec=spec)

    def guided_dd(self, disk: ModelDisk):
        self.model.dd_target = disk

    def guided_lvm(self, gap, choice: GuidedChoiceV2):
        device = gap.device
        part_align = device.alignment_data().part_align
        bootfs_size = align_up(sizes.get_bootfs_size(gap.size), part_align)
        gap_boot, gap_rest = gap.split(bootfs_size)
        spec = dict(fstype="ext4", mount="/boot")
        self.create_partition(device, gap_boot, spec)
        part = self.create_partition(device, gap_rest, dict(fstype=None))

        vg_name = "ubuntu-vg"
        i = 0
        while self.model._one(type="lvm_volgroup", name=vg_name) is not None:
            i += 1
            vg_name = "ubuntu-vg-{}".format(i)
        spec = dict(name=vg_name, devices=set([part]))
        if choice.password is not None:
            spec["passphrase"] = choice.password
        if choice.recovery_key and not choice.password:
            raise Exception("Cannot have a recovery key without encryption")
        spec["recovery-key"] = RecoveryKeyHandler.from_post_data(
            choice.recovery_key, default_suffix=f"recovery-key-{vg_name}.txt"
        )

        vg = self.create_volgroup(spec)
        if choice.sizing_policy == SizingPolicy.SCALED:
            lv_size = sizes.scaled_rootfs_size(vg.size)
            lv_size = align_down(lv_size, LVM_CHUNK_SIZE)
        elif choice.sizing_policy == SizingPolicy.ALL:
            lv_size = vg.size
        else:
            raise Exception(f"Unhandled size policy {choice.sizing_policy}")
        log.debug(f"lv_size {lv_size} for {choice.sizing_policy}")
        self.create_logical_volume(
            vg=vg,
            spec=dict(
                size=lv_size,
                name="ubuntu-lv",
                fstype="ext4",
                mount="/",
            ),
        )
        self.model.load_or_generate_recovery_keys()
        self.model.expose_recovery_keys()

    def guided_zfs(self, gap, choice: GuidedChoiceV2):
        device = gap.device
        part_align = device.alignment_data().part_align
        bootfs_size = align_up(sizes.get_bootfs_size(gap.size), part_align)
        gap_boot, gap_rest = gap.split(bootfs_size)
        bpart = self.create_partition(device, gap_boot, dict(fstype=None))
        encryption_style = None
        if encrypted := choice.password is not None:
            encryption_style = "luks_keystore"

        avail = gap_rest.size - self._info.min_size
        swap_size = align_down(swap.suggested_swapsize(avail=avail), part_align)
        if swap_size > 0:
            gap_swap, gap = gap_rest.split(swap_size)
            if encrypted:
                part = self.create_partition(device, gap_swap, {})
                self.create_cryptoswap(part)
            else:
                self.create_partition(device, gap_swap, dict(fstype="swap"))
        else:
            gap = gap_rest
        rpart = self.create_partition(device, gap, dict(fstype=None))

        uuid = gen_zsys_uuid()

        bpool = self.create_zpool(bpart, "bpool", "/boot", boot=True, canmount="off")
        bpool.create_zfs("BOOT", canmount="off", mountpoint="none")
        bpool.create_zfs(f"BOOT/ubuntu_{uuid}", mountpoint="/boot")

        rpool = self.create_zpool(
            rpart,
            "rpool",
            "/",
            canmount="off",
            encryption_style=encryption_style,
            key=choice.password,
        )
        rpool.create_zfs("ROOT", canmount="off", mountpoint="none")
        rpool.create_zfs(f"ROOT/ubuntu_{uuid}", mountpoint="/")
        rpool.create_zfs(f"ROOT/ubuntu_{uuid}/var", canmount="off")
        rpool.create_zfs(f"ROOT/ubuntu_{uuid}/var/lib")
        rpool.create_zfs(f"ROOT/ubuntu_{uuid}/var/lib/AccountsService")
        rpool.create_zfs(f"ROOT/ubuntu_{uuid}/var/lib/apt")
        rpool.create_zfs(f"ROOT/ubuntu_{uuid}/var/lib/dpkg")
        rpool.create_zfs(f"ROOT/ubuntu_{uuid}/var/lib/NetworkManager")

        rpool.create_zfs(f"ROOT/ubuntu_{uuid}/srv")
        rpool.create_zfs(f"ROOT/ubuntu_{uuid}/usr", canmount="off")
        rpool.create_zfs(f"ROOT/ubuntu_{uuid}/usr/local")
        rpool.create_zfs(f"ROOT/ubuntu_{uuid}/var/games")
        rpool.create_zfs(f"ROOT/ubuntu_{uuid}/var/log")
        rpool.create_zfs(f"ROOT/ubuntu_{uuid}/var/mail")
        rpool.create_zfs(f"ROOT/ubuntu_{uuid}/var/snap")
        rpool.create_zfs(f"ROOT/ubuntu_{uuid}/var/spool")
        rpool.create_zfs(f"ROOT/ubuntu_{uuid}/var/www")

        userdata_uuid = gen_zsys_uuid()
        rpool.create_zfs("USERDATA", canmount="off", mountpoint="none")
        rpool.create_zfs(f"USERDATA/root_{userdata_uuid}", mountpoint="/root")
        rpool.create_zfs(f"USERDATA/home_{userdata_uuid}", mountpoint="/home")

    @functools.singledispatchmethod
    def start_guided(self, target: GuidedStorageTarget, disk: ModelDisk) -> gaps.Gap:
        """Setup changes to the disk to prepare the gap that we will be
        doing a guided install into."""
        raise NotImplementedError(target)

    @start_guided.register
    def start_guided_reformat(
        self, target: GuidedStorageTargetReformat, disk: ModelDisk
    ) -> gaps.Gap:
        """Perform the reformat, and return the resulting gap."""
        in_use_parts = [p for p in disk.partitions() if p._is_in_use]
        if in_use_parts:
            for p in list(disk.partitions()):
                if not p._is_in_use:
                    self.delete_partition(p)
        else:
            self.reformat(disk, wipe="superblock-recursive")
        return gaps.largest_gap(disk)

    @start_guided.register
    def start_guided_use_gap(
        self, target: GuidedStorageTargetUseGap, disk: ModelDisk
    ) -> gaps.Gap:
        """Lookup the matching model gap."""
        return gaps.at_offset(disk, target.gap.offset)

    @start_guided.register
    def start_guided_resize(
        self, target: GuidedStorageTargetResize, disk: ModelDisk
    ) -> gaps.Gap:
        """Perform the resize of the target partition,
        and return the resulting gap."""
        partition = self.get_partition(disk, target.partition_number)
        part_align = disk.alignment_data().part_align
        new_size = align_up(target.new_size, part_align)
        if new_size > partition.size:
            raise Exception(f"Aligned requested size {new_size} too large")
        partition.size = new_size
        partition.resize = True
        # Calculating where that gap will be can be tricky due to alignment
        # needs and the possibility that we may be splitting a logical
        # partition, which needs an extra 1MiB spacer.
        gap = gaps.after(disk, partition.offset)
        if gap is None:
            pgs = gaps.parts_and_gaps(disk)
            raise Exception(f"gap not found after resize, pgs={pgs}")
        return gap

    def set_info_for_capability(self, capability: GuidedCapability):
        """Given a request for a capability, select the variation to use."""
        if capability == GuidedCapability.CORE_BOOT_ENCRYPTED:
            # If the request is for encryption, a variation with any
            # of these capabilities is OK:
            caps = {
                GuidedCapability.CORE_BOOT_ENCRYPTED,
                GuidedCapability.CORE_BOOT_PREFER_ENCRYPTED,
                GuidedCapability.CORE_BOOT_PREFER_UNENCRYPTED,
            }
        elif capability == GuidedCapability.CORE_BOOT_UNENCRYPTED:
            # Similar if the request is for uncrypted
            caps = {
                GuidedCapability.CORE_BOOT_UNENCRYPTED,
                GuidedCapability.CORE_BOOT_PREFER_ENCRYPTED,
                GuidedCapability.CORE_BOOT_PREFER_UNENCRYPTED,
            }
        else:
            # Otherwise, just look for what we were asked for.
            caps = {capability}
        for info in self._variation_info.values():
            if caps & set(info.capability_info.allowed):
                self._info = info
                return
        raise Exception("could not find variation for {}".format(capability))

    async def guided(
        self, choice: GuidedChoiceV2, reset_partition_only: bool = False
    ) -> None:
        self.model.dd_target = None
        if choice.capability == GuidedCapability.MANUAL:
            return

        self.model.guided_configuration = choice

        self.set_info_for_capability(choice.capability)

        disk = self.model._one(id=choice.target.disk_id)

        if self.is_core_boot_classic():
            if not self.app.opts.enhanced_secureboot:
                raise ValueError(
                    "Not using enhanced_secureboot: disabled on commandline"
                )
            assert isinstance(choice.target, GuidedStorageTargetReformat)
            self.use_tpm = choice.capability == GuidedCapability.CORE_BOOT_ENCRYPTED
            await self.guided_core_boot(disk)
            return

        gap = self.start_guided(choice.target, disk)
        if DeviceAction.TOGGLE_BOOT in DeviceAction.supported(disk):
            self.add_boot_disk(disk)
        # find what's left of the gap after adding boot
        gap = gap.within()
        if gap is None:
            raise Exception("failed to locate gap after adding boot")

        if choice.reset_partition:
            if choice.reset_partition_size is not None:
                part_align = disk.alignment_data().part_align
                reset_size = align_up(choice.reset_partition_size, part_align)
            elif self.app.opts.dry_run:
                reset_size = DRY_RUN_RESET_SIZE
            else:
                cp = await arun_command(["du", "-sb", "/cdrom"])
                reset_size = int(cp.stdout.strip().split()[0])
                reset_size = align_up(int(reset_size * 1.10), 256 * MiB)
            reset_gap, gap = gap.split(reset_size)
            self.model.reset_partition = self.create_partition(
                device=reset_gap.device,
                gap=reset_gap,
                spec={"fstype": "fat32"},
                flag="msftres",
            )
            self.reset_partition_only = reset_partition_only
            if reset_partition_only:
                for mount in self.model._all(type="mount"):
                    self.delete_mount(mount)
                self.model.target = self.app.base_model.target = None
                return

        if choice.capability.is_lvm():
            self.guided_lvm(gap, choice)
        elif choice.capability.is_zfs():
            self.guided_zfs(gap, choice)
        elif choice.capability == GuidedCapability.DIRECT:
            self.guided_direct(gap)
        elif choice.capability == GuidedCapability.DD:
            self.guided_dd(disk)
        else:
            raise ValueError("cannot process capability")

    async def _probe_response(self, wait, resp_cls):
        if not self._probe_task.done():
            if wait:
                await self._start_task
                await self._probe_task.wait()
            else:
                return resp_cls(status=ProbeStatus.PROBING)
        if True in self._errors:
            return resp_cls(
                status=ProbeStatus.FAILED, error_report=self._errors[True][1].ref()
            )
        if not self._examine_systems_task.done():
            if wait:
                await self._examine_systems_task.wait()
            else:
                return resp_cls(status=ProbeStatus.PROBING)
        return None

    def full_probe_error(self):
        if False in self._errors:
            return self._errors[False][1].ref()
        else:
            return None

    def _done_response(self):
        return StorageResponse(
            status=ProbeStatus.DONE,
            bootloader=self.model.bootloader,
            error_report=self.full_probe_error(),
            orig_config=self.model._orig_config,
            config=self.model._render_actions(mode=ActionRenderMode.FOR_API),
            dasd=self.model._probe_data.get("dasd", {}),
            storage_version=self.model.storage_version,
        )

    async def GET(
        self, wait: bool = False, use_cached_result: bool = False
    ) -> StorageResponse:
        if not use_cached_result:
            probe_resp = await self._probe_response(wait, StorageResponse)
            if probe_resp is not None:
                return probe_resp
        return self._done_response()

    async def POST(self, config: list):
        log.debug(config)
        self.model._actions = self.model._actions_from_config(
            config, blockdevs=self.model._probe_data["blockdev"], is_probe_data=False
        )
        self.model.load_or_generate_recovery_keys()
        self.model.expose_recovery_keys()
        await self.configured()

    def potential_boot_disks(self, check_boot=True, with_reformatting=False):
        disks = []
        for raid in self.model._all(type="raid"):
            if check_boot and not boot.can_be_boot_device(
                raid, with_reformatting=with_reformatting
            ):
                continue
            disks.append(raid)
        for disk in self.model._all(type="disk"):
            if check_boot and not boot.can_be_boot_device(
                disk, with_reformatting=with_reformatting
            ):
                continue
            cd = disk.constructed_device()
            if isinstance(cd, Raid):
                can_be_boot = False
                for v in cd._subvolumes:
                    if check_boot and boot.can_be_boot_device(
                        v, with_reformatting=with_reformatting
                    ):
                        can_be_boot = True
                if can_be_boot:
                    continue
            disks.append(disk)
        return [d for d in disks]

    def _offsets_and_sizes_for_volume(self, volume):
        offset = self.model._partition_alignment_data["gpt"].min_start_offset
        for structure in volume.structure:
            if structure.role == snapdapi.Role.MBR:
                continue
            if structure.offset is not None:
                offset = structure.offset
            yield (structure, offset, structure.size)
            offset = offset + structure.size

    async def guided_core_boot(self, disk: Disk):
        # Formatting for a core boot classic system relies on some curtin
        # features that are only available with v2 partitioning.
        await self._mount_systems_dir(self._info.name)
        self.model.storage_version = 2
        [volume] = self._info.system.volumes.values()
        self._on_volume = snapdapi.OnVolume.from_volume(volume)

        preserved_parts = set()

        if self._on_volume.schema != disk.ptable:
            disk.ptable = self._on_volume.schema
            parts_by_offset_size = {}
        else:
            parts_by_offset_size = {
                (part.offset, part.size): part for part in disk.partitions()
            }

            for _struct, offset, size in self._offsets_and_sizes_for_volume(
                self._on_volume
            ):
                if (offset, size) in parts_by_offset_size:
                    preserved_parts.add(parts_by_offset_size[(offset, size)])

            for part in list(disk.partitions()):
                if part not in preserved_parts:
                    self.delete_partition(part)
                    del parts_by_offset_size[(part.offset, part.size)]

        if not preserved_parts:
            self.reformat(disk, self._on_volume.schema)

        for structure, offset, size in self._offsets_and_sizes_for_volume(
            self._on_volume
        ):
            if (offset, size) in parts_by_offset_size:
                part = parts_by_offset_size[(offset, size)]
            else:
                if (
                    structure.role == snapdapi.Role.SYSTEM_DATA
                    and structure == self._on_volume.structure[-1]
                ):
                    gap = gaps.at_offset(disk, offset)
                    size = gap.size
                part = self.model.add_partition(
                    disk, offset=offset, size=size, check_alignment=False
                )

            type_uuid = structure.gpt_part_type_uuid()
            if type_uuid:
                part.partition_type = type_uuid
                part.flag = ptable_part_type_to_flag(type_uuid)
            if structure.name:
                part.partition_name = structure.name
            if structure.filesystem:
                part.wipe = "superblock"
                self.delete_filesystem(part.fs())
                fs = self.model.add_filesystem(
                    part, structure.filesystem, label=structure.label
                )
                if structure.role == snapdapi.Role.SYSTEM_DATA:
                    self.model.add_mount(fs, "/")
                elif structure.role == snapdapi.Role.SYSTEM_BOOT:
                    self.model.add_mount(fs, "/boot")
                elif part.flag == "boot":
                    part.grub_device = True
                    self.model.add_mount(fs, "/boot/efi")
            if structure.role != snapdapi.Role.NONE:
                self._role_to_device[structure.role] = part
            self._device_to_structure[part] = structure

        disk._partitions.sort(key=lambda p: p.number)

    def _on_volumes(self) -> Dict[str, snapdapi.OnVolume]:
        # Return a value suitable for use as the 'on-volumes' part of a
        # SystemActionRequest.
        #
        # This must be run after curtin partitioning, which will result in a
        # call to update_devices which will have set .path on all block
        # devices.
        [key] = self._info.system.volumes.keys()
        return {key: self._on_volume}

    @with_context(description="configuring TPM-backed full disk encryption")
    async def setup_encryption(self, context):
        label = self._info.label
        result = await snapdapi.post_and_wait(
            self.app.snapdapi,
            self.app.snapdapi.v2.systems[label].POST,
            snapdapi.SystemActionRequest(
                action=snapdapi.SystemAction.INSTALL,
                step=snapdapi.SystemActionStep.SETUP_STORAGE_ENCRYPTION,
                on_volumes=self._on_volumes(),
            ),
        )
        role_to_encrypted_device = result["encrypted-devices"]
        for role, enc_path in role_to_encrypted_device.items():
            arb_device = ArbitraryDevice(m=self.model, path=enc_path)
            self.model._actions.append(arb_device)
            part = self._role_to_device[role]
            for fs in self.model._all(type="format"):
                if fs.volume == part:
                    fs.volume = arb_device

    @with_context(description="making system bootable")
    async def finish_install(self, context):
        label = self._info.label
        await snapdapi.post_and_wait(
            self.app.snapdapi,
            self.app.snapdapi.v2.systems[label].POST,
            snapdapi.SystemActionRequest(
                action=snapdapi.SystemAction.INSTALL,
                step=snapdapi.SystemActionStep.FINISH,
                on_volumes=self._on_volumes(),
            ),
        )

    async def guided_POST(self, data: GuidedChoiceV2) -> StorageResponse:
        log.debug(data)
        await self.guided(data)
        if not data.capability.supports_manual_customization():
            await self.configured()
        return self._done_response()

    async def reset_POST(self, context, request) -> StorageResponse:
        log.info("Resetting Filesystem model")
        self.model.reset()
        return await self.GET(context)

    async def has_rst_GET(self) -> bool:
        search = "/sys/module/ahci/drivers/pci:ahci/*/remapped_nvme"
        for remapped_nvme in glob.glob(search):
            with open(remapped_nvme, "r") as f:
                if int(f.read()) > 0:
                    return True
        return False

    async def has_bitlocker_GET(self) -> List[Disk]:
        """list of Disks that contain a partition that is BitLockered"""
        bitlockered_disks = []
        for disk in self.model.all_disks() + self.model.all_raids():
            for part in disk.partitions():
                fs = part.fs()
                if not fs:
                    continue
                fstype = fs.fstype
                if fstype == "BitLocker":
                    bitlockered_disks.append(disk)
                    break
        return [labels.for_client(disk) for disk in bitlockered_disks]

    def get_partition(self, disk, number):
        for p in disk.partitions():
            if p.number == number:
                return p
        raise ValueError(f"Partition {number} on {disk.id} not found")

    def calculate_suggested_install_min(self):
        catalog_entry = self.app.base_model.source.current
        source_min = max(
            variation.size for variation in catalog_entry.variations.values()
        )
        align = max(
            (pa.part_align for pa in self.model._partition_alignment_data.values())
        )
        install_min = sizes.calculate_suggested_install_min(source_min, align)
        log.debug(f"suggested install minimum size: {humanize_size(install_min)}")
        return install_min

    async def get_v2_storage_response(self, model, wait, include_raid):
        probe_resp = await self._probe_response(wait, StorageResponseV2)
        if probe_resp is not None:
            return probe_resp
        if include_raid:
            disks = self.potential_boot_disks(with_reformatting=True)
        else:
            disks = model._all(type="disk")
        minsize = self.calculate_suggested_install_min()
        return StorageResponseV2(
            status=ProbeStatus.DONE,
            disks=[labels.for_client(d) for d in disks],
            need_root=not model.is_root_mounted(),
            need_boot=model.needs_bootloader_partition(),
            install_minimum_size=minsize,
        )

    async def generate_recovery_key_GET(self) -> str:
        return self.model.generate_recovery_key()

    async def v2_GET(
        self,
        wait: bool = False,
        include_raid: bool = False,
    ) -> StorageResponseV2:
        return await self.get_v2_storage_response(self.model, wait, include_raid)

    async def v2_POST(self) -> StorageResponseV2:
        await self.configured()
        return await self.v2_GET()

    async def v2_orig_config_GET(self) -> StorageResponseV2:
        model = self.model.get_orig_model()
        return await self.get_v2_storage_response(model, False, False)

    async def v2_reset_POST(self) -> StorageResponseV2:
        log.info("Resetting Filesystem model")
        # From the API standpoint, it seems sound to set locked_probe_data back
        # to False after a reset. But in practise, v2_reset_POST can be called
        # during manual partitioning ; and we don't want to reenable automatic
        # loading of probe data. Going forward, this could be controlled by an
        # optional parameter maybe?
        if self.queued_probe_data is not None:
            log.debug("using newly obtained probe data")
            self.model.load_probe_data(self.queued_probe_data)
            self.queued_probe_data = None
        else:
            self.model.reset()
        return await self.v2_GET()

    async def v2_ensure_transaction_POST(self) -> None:
        self.locked_probe_data = True

    def get_classic_capabilities(self):
        classic_capabilities = set()
        for info in self._variation_info.values():
            if not info.is_valid():
                continue
            if not info.is_core_boot_classic():
                classic_capabilities.update(info.capability_info.allowed)
        return sorted(classic_capabilities)

    def _guided_has_enough_room_for_partitions(
        self,
        disk,
        *,
        resized_partition: Optional[Partition] = None,
        gap: Optional[gaps.Gap] = None,
    ) -> bool:
        """Check if we have enough room for all the primary partitions. This
        isn't failproof but should limit the number of TargetResize/UseGap
        scenarios that are suggested but can't be applied because we don't have
        enough room for partitions.
        """
        if (resized_partition is None) == (gap is None):
            raise ValueError("please specify either resized_partition or gap")

        new_primary_parts = 0
        if resized_partition is not None:
            install_to_logical = resized_partition.is_logical
        else:
            install_to_logical = gap.in_extended

        if not install_to_logical:
            new_primary_parts += 1

        boot_plan = boot.get_boot_device_plan(disk, resize_partition=resized_partition)
        new_primary_parts += boot_plan.new_partition_count()
        # In theory, there could be a recovery partition as well. Not sure
        # how to account for it since we don't know yet if one will be
        # requested.
        return new_primary_parts <= gaps.remaining_primary_partitions(
            disk, disk.alignment_data()
        )

    def use_gap_has_enough_room_for_partitions(self, disk, gap: gaps.Gap) -> bool:
        return self._guided_has_enough_room_for_partitions(disk, gap=gap)

    def resize_has_enough_room_for_partitions(self, disk, resized: Partition) -> bool:
        return self._guided_has_enough_room_for_partitions(
            disk, resized_partition=resized
        )

    def available_use_gap_scenarios(
        self, install_min: int
    ) -> list[tuple[int, GuidedStorageTargetUseGap]]:
        scenarios: list[tuple[int, GuidedStorageTargetUseGap]] = []
        for disk in self.potential_boot_disks(with_reformatting=False):
            parts = [
                p
                for p in disk.partitions()
                if p.flag != "bios_grub" and not p._is_in_use
            ]
            if len(parts) < 1:
                # On an (essentially) empty disk, don't bother to offer it
                # with UseGap, as it's basically the same as the Reformat
                # case.
                continue
            gap = gaps.largest_gap(disk)
            if gap is None:
                # Do we return a reason here?
                continue
            if not self.use_gap_has_enough_room_for_partitions(disk, gap):
                log.error("skipping UseGap: not enough room for primary partitions")
                continue

            capability_info = CapabilityInfo()
            for variation in self._variation_info.values():
                if variation.is_core_boot_classic():
                    continue
                capability_info.combine(
                    variation.capability_info_for_gap(gap, install_min)
                )
            api_gap = labels.for_client(gap)
            use_gap = GuidedStorageTargetUseGap(
                disk_id=disk.id,
                gap=api_gap,
                allowed=capability_info.allowed,
                disallowed=capability_info.disallowed,
            )
            scenarios.append((gap.size, use_gap))
        return scenarios

    def available_target_resize_scenarios(
        self, install_min: int
    ) -> list[tuple[int, GuidedStorageTargetResize]]:
        scenarios: list[tuple[int, GuidedStorageTargetResize]] = []

        for disk in self.potential_boot_disks(check_boot=False):
            part_align = disk.alignment_data().part_align
            for partition in disk.partitions():
                if partition._is_in_use:
                    continue
                vals = sizes.calculate_guided_resize(
                    partition.estimated_min_size,
                    partition.size,
                    install_min,
                    part_align=part_align,
                )
                if vals is None:
                    # Return a reason here
                    continue
                if not boot.can_be_boot_device(
                    disk, resize_partition=partition, with_reformatting=False
                ):
                    # Return a reason here
                    continue

                if not self.resize_has_enough_room_for_partitions(disk, partition):
                    log.error(
                        "skipping TargetResize: not enough room for primary partitions"
                    )
                    continue

                resize = GuidedStorageTargetResize.from_recommendations(
                    partition, vals, allowed=self.get_classic_capabilities()
                )
                scenarios.append((vals.install_max, resize))
        return scenarios

    async def v2_guided_GET(self, wait: bool = False) -> GuidedStorageResponseV2:
        """Acquire a list of possible guided storage configuration scenarios.
        Results are sorted by the size of the space potentially available to
        the install."""
        probe_resp = await self._probe_response(wait, GuidedStorageResponseV2)
        if probe_resp is not None:
            return probe_resp

        scenarios = []
        install_min = self.calculate_suggested_install_min()

        classic_capabilities = self.get_classic_capabilities()

        if GuidedCapability.DIRECT in classic_capabilities:
            scenarios.append((0, GuidedStorageTargetManual()))

        for disk in self.potential_boot_disks(with_reformatting=True):
            capability_info = CapabilityInfo()
            for variation in self._variation_info.values():
                gap = gaps.largest_gap(disk._reformatted())
                capability_info.combine(
                    variation.capability_info_for_gap(gap, install_min)
                )
            reformat = GuidedStorageTargetReformat(
                disk_id=disk.id,
                allowed=capability_info.allowed,
                disallowed=capability_info.disallowed,
            )
            scenarios.append((disk.size, reformat))

        scenarios.extend(self.available_use_gap_scenarios(install_min))
        scenarios.extend(self.available_target_resize_scenarios(install_min))

        scenarios.sort(reverse=True, key=lambda x: x[0])
        return GuidedStorageResponseV2(
            status=ProbeStatus.DONE,
            configured=self.model.guided_configuration,
            targets=[s[1] for s in scenarios],
        )

    async def v2_guided_POST(self, data: GuidedChoiceV2) -> GuidedStorageResponseV2:
        log.debug(data)
        self.locked_probe_data = True
        await self.guided(data)
        return await self.v2_guided_GET()

    async def v2_reformat_disk_POST(self, data: ReformatDisk) -> StorageResponseV2:
        self.locked_probe_data = True
        self.reformat(self.model._one(id=data.disk_id), data.ptable)
        return await self.v2_GET()

    async def v2_add_boot_partition_POST(self, disk_id: str) -> StorageResponseV2:
        self.locked_probe_data = True
        disk = self.model._one(id=disk_id)
        if boot.is_boot_device(disk):
            raise ValueError("device already has bootloader partition")
        if DeviceAction.TOGGLE_BOOT not in DeviceAction.supported(disk):
            raise ValueError("disk does not support boot partiton")
        self.add_boot_disk(disk)
        return await self.v2_GET()

    async def v2_add_partition_POST(self, data: AddPartitionV2) -> StorageResponseV2:
        log.debug(data)
        self.locked_probe_data = True
        if data.partition.boot is not None:
            raise ValueError("add_partition does not support changing boot")
        disk = self.model._one(id=data.disk_id)
        requested_size = data.partition.size or 0
        if requested_size > data.gap.size:
            raise ValueError("new partition too large")
        if requested_size < 1:
            requested_size = data.gap.size
        # empty string is an unformatted partition
        fstype = data.partition.format or None
        spec = {
            "size": requested_size,
            "fstype": fstype,
            "mount": data.partition.mount,
        }

        gap = gaps.at_offset(disk, data.gap.offset).split(requested_size)[0]
        self.create_partition(disk, gap, spec, wipe="superblock")
        return await self.v2_GET()

    async def v2_delete_partition_POST(
        self, data: ModifyPartitionV2
    ) -> StorageResponseV2:
        log.debug(data)
        self.locked_probe_data = True
        disk = self.model._one(id=data.disk_id)
        partition = self.get_partition(disk, data.partition.number)
        self.delete_partition(partition)
        return await self.v2_GET()

    async def v2_edit_partition_POST(
        self, data: ModifyPartitionV2
    ) -> StorageResponseV2:
        log.debug(data)
        self.locked_probe_data = True
        disk = self.model._one(id=data.disk_id)
        partition = self.get_partition(disk, data.partition.number)
        if (
            data.partition.size not in (None, partition.size)
            and self.app.opts.storage_version < 2
        ):
            raise ValueError("edit_partition does not support changing size")
        if data.partition.boot is not None and data.partition.boot != partition.boot:
            raise ValueError("edit_partition does not support changing boot")
        spec = {"mount": data.partition.mount or partition.mount}
        if data.partition.format is not None:
            if data.partition.format != partition.original_fstype():
                if data.partition.wipe is None:
                    raise ValueError("changing partition format requires a wipe value")
            spec["fstype"] = data.partition.format
        if data.partition.size is not None:
            spec["size"] = data.partition.size
        spec["wipe"] = data.partition.wipe
        self.partition_disk_handler(disk, spec, partition=partition)
        return await self.v2_GET()

    async def dry_run_wait_probe_POST(self) -> None:
        if not self.app.opts.dry_run:
            raise NotImplementedError

        # This will start the probe task if not yet started.
        self.ensure_probing()

        await self._probe_task.task

    @with_context(name="probe_once", description="restricted={restricted}")
    async def _probe_once(self, *, context, restricted):
        if restricted:
            probe_types = {"blockdev", "filesystem", "nvme"}
            fname = "probe-data-restricted.json"
            key = "ProbeDataRestricted"
        else:
            probe_types = {"defaults", "filesystem_sizing"}
            if self.app.opts.use_os_prober:
                probe_types |= {"os"}
            fname = "probe-data.json"
            key = "ProbeData"
        storage = await self.app.prober.get_storage(probe_types)
        # It is possible for the user to submit filesystem config
        # while a probert probe is running. We don't want to overwrite
        # the users config with a blank one if this happens! (See
        # https://bugs.launchpad.net/bugs/1954848).
        if self._configured:
            return
        fpath = os.path.join(self.app.block_log_dir, fname)
        with open(fpath, "w") as fp:
            json.dump(storage, fp, indent=4)
        self.app.note_file_for_apport(key, fpath)
        if not self.locked_probe_data:
            self.queued_probe_data = None
            self.model.load_probe_data(storage)
        else:
            self.queued_probe_data = storage

    @with_context()
    async def _probe(self, *, context=None):
        self._errors = {}
        for restricted, kind, short_label in [
            (False, ErrorReportKind.BLOCK_PROBE_FAIL, "block"),
            (True, ErrorReportKind.DISK_PROBE_FAIL, "disk"),
        ]:
            probert_timeout = 90.0
            if self.app.opts.use_os_prober:
                # We know that os-prober is going to be (very) slow on some
                # systems, let's give probert more time.
                probert_timeout *= 2
            try:
                start = time.time()
                await self._probe_once_task.start(
                    context=context, restricted=restricted
                )
                # We wait on the task directly here, not
                # self._probe_once_task.wait as if _probe_once_task
                # gets cancelled, we should be cancelled too.
                await asyncio.wait_for(self._probe_once_task.task, probert_timeout)
            except asyncio.CancelledError:
                # asyncio.CancelledError is a subclass of Exception in
                # Python 3.6 (sadface)
                raise
            except Exception as exc:
                block_discover_log.exception(
                    "block probing failed restricted=%s", restricted
                )
                report = self.app.make_apport_report(kind, "block probing")
                if report is not None:
                    self._errors[restricted] = (exc, report)
                continue
            finally:
                elapsed = time.time() - start
                log.debug(f"{short_label} probing took {elapsed:.1f} seconds")
                # In the past, this start_monitor() equivalent was much sooner.
                # We don't actually need the information it provides though
                # until a probe has finished, so the start_monitor() is delayed
                # to here. start_monitor() is allowed after a failed probe, in
                # case of a hotplug event, perhaps to remove a problematic
                # device.
                self.start_monitor()
            break

    def get_bootable_matching_disks(
        self, match: MatchDirective | Sequence[MatchDirective]
    ) -> list[_Device]:
        """given a match directive, find disks or disk-like devices for which
        we have a plan to boot, and return them.
        As match directives are autoinstall-supplied, raise AutoinstallError if
        no matching disk is found."""
        disks = self.potential_boot_disks(with_reformatting=True)
        matching_disks = self.model.disks_for_match(disks, match)
        if not matching_disks:
            raise AutoinstallError(f"Failed to find matching device for {match}")
        return matching_disks

    def get_bootable_matching_disk(
        self, match: MatchDirective | Sequence[MatchDirective]
    ) -> _Device:
        """given a match directive, find disks or disk-like devices for which
        we have a plan to boot, and return the best matching one of those.
        As match directives are autoinstall-supplied, raise AutoinstallError if
        no matching disk is found."""
        matching_disks = self.get_bootable_matching_disks(match)
        assert matching_disks
        return matching_disks[0]

    async def run_autoinstall_guided(self, layout):
        name = layout["name"]
        password = None
        sizing_policy = None
        guided_recovery_key: Union[bool, RecoveryKey] = False

        if name == "hybrid":
            # this check is conceptually unnecessary but results in a
            # much cleaner error message...
            core_boot_caps = set()
            for variation in self._variation_info.values():
                if not variation.is_valid():
                    continue
                if variation.is_core_boot_classic():
                    core_boot_caps.update(variation.capability_info.allowed)
            if not core_boot_caps:
                raise Exception(
                    "can only use name: hybrid when installing core boot classic"
                )
            if "mode" in layout:
                raise Exception("cannot use 'mode' when installing core boot classic")
            encrypted = layout.get("encrypted", None)
            GC = GuidedCapability
            if encrypted is None:
                if (
                    GC.CORE_BOOT_ENCRYPTED in core_boot_caps
                    or GC.CORE_BOOT_PREFER_ENCRYPTED in core_boot_caps
                ):
                    capability = GC.CORE_BOOT_ENCRYPTED
                else:
                    capability = GC.CORE_BOOT_UNENCRYPTED
            elif encrypted:
                capability = GC.CORE_BOOT_ENCRYPTED
            else:
                if (
                    core_boot_caps == {GuidedCapability.CORE_BOOT_ENCRYPTED}
                    and not encrypted
                ):
                    raise Exception("cannot install this model unencrypted")
                capability = GC.CORE_BOOT_UNENCRYPTED
            match = layout.get("match", {"size": "largest"})
            disk = self.get_bootable_matching_disk(match)
            mode = "reformat_disk"
        else:
            # this check is conceptually unnecessary but results in a
            # much cleaner error message...
            for variation in self._variation_info.values():
                if not variation.is_valid():
                    continue
                if not variation.is_core_boot_classic():
                    break
            else:
                raise Exception(
                    "must use name: hybrid when installing core boot classic"
                )
            mode = layout.get("mode", "reformat_disk")
            self.validate_layout_mode(mode)
            password = layout.get("password", None)
            recovery_key = layout.get("recovery-key", False)
            if name == "lvm":
                sizing_policy = SizingPolicy.from_string(
                    layout.get("sizing-policy", None)
                )
                if password is not None:
                    capability = GuidedCapability.LVM_LUKS
                else:
                    capability = GuidedCapability.LVM
                if recovery_key and password is None:
                    raise Exception(
                        "recovery_key can only be used if password is specified"
                    )
                guided_recovery_key = RecoveryKey.from_autoinstall(recovery_key)

            elif name == "dd":
                capability = GuidedCapability.DD
                assert mode == "reformat_disk"
            elif name == "zfs":
                if password is not None:
                    capability = GuidedCapability.ZFS_LUKS_KEYSTORE
                else:
                    capability = GuidedCapability.ZFS
            else:
                capability = GuidedCapability.DIRECT

        if mode == "reformat_disk":
            match = layout.get("match", {"size": "largest"})
            disk = self.get_bootable_matching_disk(match)
            target = GuidedStorageTargetReformat(disk_id=disk.id, allowed=[])
        elif mode == "use_gap":
            match = layout.get("match", {})
            bootable_disks = self.get_bootable_matching_disks(match)
            gap = gaps.largest_gap(bootable_disks)
            if not gap:
                raise Exception(
                    "autoinstall cannot configure storage "
                    "- no gap found large enough for install"
                )
            target = GuidedStorageTargetUseGap(
                disk_id=gap.device.id, gap=gap, allowed=[]
            )

        reset_partition = False
        reset_partition_size = None
        rp_input = layout.get("reset-partition", None)
        if rp_input:
            reset_partition = True
            # bool is a subclass of int -- check for int explicitly
            if isinstance(rp_input, str) or type(rp_input) is int:
                reset_partition_size = int(human2bytes(rp_input))
                log.info(
                    "autoinstall: will install reset partition "
                    f"of size {reset_partition_size}"
                )
            else:
                log.info("autoinstall: will install reset partition")

        log.info(
            f"autoinstall: running guided {capability} install in "
            f"mode {mode} using {target}"
        )
        await self.guided(
            GuidedChoiceV2(
                target=target,
                capability=capability,
                password=password,
                recovery_key=guided_recovery_key,
                sizing_policy=sizing_policy,
                reset_partition=reset_partition,
                reset_partition_size=reset_partition_size,
            ),
            reset_partition_only=layout.get("reset-partition-only", False),
        )

    def validate_layout_mode(self, mode):
        if mode not in ("reformat_disk", "use_gap"):
            raise ValueError(f"Unknown layout mode {mode}")

    @with_context()
    async def convert_autoinstall_config(self, context=None):
        # Log disabled to prevent LUKS password leak
        # log.debug("self.ai_data = %s", self.ai_data)
        if "layout" in self.ai_data:
            if "config" in self.ai_data:
                log.warning(
                    "The 'storage' section should not contain both "
                    "'layout' and 'config', using 'layout'"
                )
            await self.run_autoinstall_guided(self.ai_data["layout"])
        elif "config" in self.ai_data:
            if self.app.base_model.source.current.type.startswith("dd-"):
                raise Exception("must not use config: when installing a disk image")
            # XXX in principle should there be a way to influence the
            # variation chosen here? Not with current use cases for
            # variations anyway.
            for variation in self._variation_info.values():
                if not variation.is_valid():
                    continue
                if not variation.is_core_boot_classic():
                    self._info = variation
                    break
            else:
                raise Exception(
                    "must not use config: when installing core boot classic"
                )
            self.model.apply_autoinstall_config(self.ai_data["config"])
        self.model.swap = self.ai_data.get("swap")
        self.model.grub = self.ai_data.get("grub")

    def start(self):
        if self.model.bootloader == Bootloader.PREP:
            self.supports_resilient_boot = False
        else:
            release = lsb_release(dry_run=self.app.opts.dry_run)["release"]
            self.supports_resilient_boot = release >= "20.04"
        self._start_task = schedule_task(self._start())

    async def _start(self):
        await self._probe_task.start()

    def start_monitor(self):
        if self._configured:
            return

        log.debug("start_monitor")
        if self._pyudev_context is None:
            self._pyudev_context = pyudev.Context()
        self._monitor = pyudev.Monitor.from_netlink(self._pyudev_context)
        self._monitor.filter_by(subsystem="block")
        self._monitor.start()
        loop = asyncio.get_running_loop()
        loop.add_reader(self._monitor.fileno(), self._udev_event)

    def stop_monitor(self):
        if self._monitor is None:
            return

        log.debug("stop_monitor")
        loop = asyncio.get_running_loop()
        loop.remove_reader(self._monitor.fileno())

        self._monitor = None

    def ensure_probing(self):
        try:
            self._probe_task.start_sync()
        except TaskAlreadyRunningError:
            log.debug("Skipping run of Probert - probe run already active")
        else:
            log.debug("Triggered Probert run on udev event")

    def _udev_event(self):
        # We outright stop monitoring because we're not super concerned about
        # the specifics of the udev event, only that one happened and that when
        # the events settle, we want to reprobe.  This is significantly faster
        # than keeping a monitor around and draining the event queue.
        # LP: #2009141
        self.stop_monitor()

        cp = run_command(["udevadm", "settle", "-t", "0"])

        if cp.returncode != 0:
            log.debug("waiting 0.1 to let udev event queue settle")
            loop = asyncio.get_running_loop()
            loop.call_later(0.1, self._udev_event)
            return
        self.ensure_probing()

    def make_autoinstall(self):
        if self.model.dd_target is None:
            rendered = self.model.render()
            r = {"config": rendered["storage"]["config"]}
            if "swap" in rendered:
                r["swap"] = rendered["swap"]
        else:
            r = {
                "layout": {
                    "name": "dd",
                    "match": {
                        "path": self.model.dd_target.path,
                    },
                },
            }
        return r

    async def _pre_shutdown(self):
        if not self.reset_partition_only:
            # /target is mounted only if the installation was actually started.
            try:
                await self.app.command_runner.run(["mountpoint", "/target"])
            except subprocess.CalledProcessError:
                log.debug(
                    "/target does not exist or is not mounted,"
                    " skipping call to umount --recursive"
                )
            else:
                await self.app.command_runner.run(["umount", "--recursive", "/target"])
        await self.app.command_runner.run(["zpool", "export", "-a"])
