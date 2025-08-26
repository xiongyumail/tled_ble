"""Light entities for TLED BLE integration."""
import logging
from homeassistant.config_entries import ConfigEntry
from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    LightEntity,
    ColorMode
)
from homeassistant.const import CONF_MAC
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, MANUFACTURER, BROADCAST_ADDR
from .ble_controller import TLEDBLEController

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up TLED BLE light entities from a config entry."""
    mac = entry.data[CONF_MAC]
    controller = hass.data[DOMAIN][mac]
    
    # 创建子设备实体
    entities = []
    
    # 添加已配置的子设备
    for addr, info in controller.subdevices.items():
        entities.append(TLEDBLELight(controller, addr, info["name"]))
    
    # 添加广播控制实体（控制所有设备）
    entities.append(TLEDBLELight(controller, int(BROADCAST_ADDR), "All TLED Devices"))
    
    async_add_entities(entities)

class TLEDBLELight(LightEntity):
    """Representation of a TLED BLE light."""

    def __init__(self, controller: TLEDBLEController, address: int, name: str):
        self.controller = controller
        self.address = address
        self._name = name
        self._is_on = False
        self._brightness = 0
        self._unique_id = f"{controller.mac_address}_{address}"
        
        # 注册状态更新监听
        self._unsub_update = self.controller.hass.bus.async_listen(
            f"{DOMAIN}_subdevice_updated", self._handle_state_update
        )
        
        # 注册配置更新监听（修正后的代码）
        self._unsub_config_update = self.controller.config_entry.add_update_listener(
            self._handle_config_update_listener
        )
        
        # 初始化状态
        if address in controller.subdevices:
            state = controller.subdevices[address]["state"]
            self._is_on = state["on"]
            self._brightness = state["brightness"]

    async def _handle_config_update_listener(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """处理配置更新的监听器"""
        self._handle_config_update(entry)

    @callback
    def _handle_config_update(self, entry: ConfigEntry) -> None:
        """配置更新时重新加载实体"""
        self.controller.subdevices = entry.options.get("subdevices", {})
        # 检查当前设备是否仍在配置中
        if self.address in self.controller.subdevices:
            info = self.controller.subdevices[self.address]
            self._name = info["name"]
            self._is_on = info["state"]["on"]
            self._brightness = info["state"]["brightness"]
            self.async_write_ha_state()

    @callback
    def _handle_state_update(self, event):
        """处理子设备状态更新事件"""
        if event.data.get("address") == self.address:
            state = event.data["state"]
            self._is_on = state["on"]
            self._brightness = state["brightness"]
            self.async_write_ha_state()

    @property
    def unique_id(self):
        """Return the unique ID for this entity."""
        return self._unique_id

    @property
    def name(self):
        """Return the name of the entity."""
        return self._name

    @property
    def is_on(self):
        """Return true if the light is on."""
        return self._is_on

    @property
    def brightness(self):
        """Return the brightness of this light between 0..255."""
        return self._brightness

    @property
    def should_poll(self):
        """No polling needed."""
        return False

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.controller.mac_address)},
            name=self.controller.name,
            manufacturer=MANUFACTURER,
        )

    @property
    def color_mode(self):
        """返回当前颜色模式"""
        return ColorMode.BRIGHTNESS

    @property
    def supported_color_modes(self):
        """返回支持的颜色模式集合"""
        return {ColorMode.BRIGHTNESS}

    @property
    def icon(self):
        """返回实体图标"""
        return "mdi:lightbulb-group"

    async def async_turn_on(self, **kwargs):
        """Turn on the light or adjust brightness."""
        is_on = True
        brightness = kwargs.get(ATTR_BRIGHTNESS, self._brightness or 255)
        
        # 发送命令
        success = await self.controller.send_control_command(
            self.address, is_on, brightness
        )
        
        if success:
            self._is_on = is_on
            self._brightness = brightness
            self.async_write_ha_state()

    async def async_turn_off(self,** kwargs):
        """Turn off the light."""
        success = await self.controller.send_control_command(
            self.address, False, self._brightness
        )
        
        if success:
            self._is_on = False
            self.async_write_ha_state()

    async def async_will_remove_from_hass(self):
        """Clean up when entity is removed."""
        if hasattr(self, "_unsub_update"):
            self._unsub_update()
        if hasattr(self, "_unsub_config_update"):
            self._unsub_config_update()