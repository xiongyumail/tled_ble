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

from .const import DOMAIN, MANUFACTURER
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
    
    # 1. 创建并添加初始已配置的子设备
    entities = []
    for addr, info in controller.subdevices.items():
        entities.append(TLEDBLELight(controller, addr, info["name"]))
    async_add_entities(entities)

    # 2. 注册动态发现监听器
    @callback
    def async_discover_new_device(event):
        """当控制器发现新 Mesh 地址时，动态添加实体"""
        if event.data.get("controller_mac") == mac:
            address = event.data["address"]
            name = event.data["name"]
            _LOGGER.info(f"大王，正在为新发现的设备 {name} 册封实体！")
            async_add_entities([TLEDBLELight(controller, address, name)])

    # 绑定监听器到总线，并在卸载时取消
    entry.async_on_unload(
        hass.bus.async_listen(f"{DOMAIN}_new_subdevice_found", async_discover_new_device)
    )

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
        
        # 初始化状态
        if address in controller.subdevices:
            state = controller.subdevices[address]["state"]
            self._is_on = state["on"]
            self._brightness = state["brightness"]

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
        """返回设备信息，将每个 Mesh 地址映射为独立设备"""
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self.controller.mac_address}_{self.address:04X}")},
            name=self._name,
            manufacturer=MANUFACTURER,
            model="Mesh 智能灯",
            via_device=(DOMAIN, self.controller.mac_address),
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
        return "mdi:lightbulb"

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