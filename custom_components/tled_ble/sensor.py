"""Sensor entities for TLED BLE RSSI monitoring."""
import logging
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.components.bluetooth import (
    async_discovered_service_info,
    async_last_service_info,
    async_register_callback,
    BluetoothScanningMode,
)
from homeassistant.const import (
    CONF_MAC,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.config_entries import ConfigEntry

from .const import DOMAIN, MANUFACTURER
from .ble_controller import TLEDBLEController

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up TLED BLE sensor from config entry."""
    mac = entry.data[CONF_MAC]
    controller = hass.data[DOMAIN][mac]
    async_add_entities([TLEDBLERSSISensor(controller)])

class TLEDBLERSSISensor(SensorEntity):
    """Representation of a TLED BLE RSSI sensor."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_native_unit_of_measurement = SIGNAL_STRENGTH_DECIBELS_MILLIWATT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_has_entity_name = True

    def __init__(self, controller: TLEDBLEController):
        """Initialize the RSSI sensor."""
        self.controller = controller
        self._mac = controller.mac_address
        self._attr_unique_id = f"{self._mac}_rssi"
        self._attr_name = "信号强度"
        self._rssi = None

    @property
    def native_value(self):
        """Return the current RSSI value."""
        return self._rssi

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information, linking to the gateway."""
        from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH
        return DeviceInfo(
            connections={(CONNECTION_BLUETOOTH, self._mac)},
            identifiers={(DOMAIN, self._mac)},
            name="tled.gateway",
            manufacturer=MANUFACTURER,
            model="Mesh 网关",
        )

    async def async_added_to_hass(self) -> None:
        """Register callbacks when entity is added."""
        # 初始化获取最后一次的信号强度
        service_info = async_last_service_info(self.hass, self._mac, connectable=True)
        if service_info:
            self._rssi = service_info.rssi

        # 1. 注册蓝牙广播回调，实时更新信号强度（使用 ACTIVE 模式提高灵敏度）
        self.async_on_remove(
            async_register_callback(
                self.hass,
                self._handle_bluetooth_event,
                {"address": self._mac},
                BluetoothScanningMode.ACTIVE,
            )
        )

        # 2. 注册控制器 RSSI 更新事件监听，解决连接后广播停止的问题
        self.async_on_remove(
            self.hass.bus.async_listen(f"{DOMAIN}_rssi_updated", self._handle_rssi_event)
        )

        # 3. 注册可用性变更监听
        self.async_on_remove(
            self.hass.bus.async_listen(f"{DOMAIN}_availability_changed", self._handle_availability_update)
        )

        await super().async_added_to_hass()

    @callback
    def _handle_availability_update(self, event):
        """处理网关可用性变更"""
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """返回实体是否可用"""
        return self.controller.connected

    @callback
    def _handle_rssi_event(self, event):
        """Handle RSSI update event from controller."""
        if event.data.get("address") == self._mac:
            self._rssi = event.data.get("rssi")
            self.async_write_ha_state()

    @callback
    def _handle_bluetooth_event(self, service_info, change):
        """Handle bluetooth advertisement update."""
        self._rssi = service_info.rssi
        self.async_write_ha_state()
