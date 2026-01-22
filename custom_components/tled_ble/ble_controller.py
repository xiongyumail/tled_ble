# tled_ble/ble_controller.py
import asyncio
import logging
from typing import Optional
from bleak import BleakClient, BleakError
from homeassistant.components.bluetooth import async_get_scanner
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError

from .const import (
    HEADER,
    CONTROL_CMD,
    DOMAIN
)

_LOGGER = logging.getLogger(__name__)

class TLEDBLEController:
    """控制与TLED BLE设备的连接和通信"""
    
    def __init__(self, hass: HomeAssistant, device_address: str, service_uuid: str, char_uuid: str):
        self.hass = hass
        self.device_address = device_address  # MAC地址
        self.mac_address = device_address
        self.service_uuid = service_uuid      # 服务UUID
        self.char_uuid = char_uuid            # 特征值UUID
        self.name = ""  # 设备名称（从配置中获取）
        self.client: Optional[BleakClient] = None
        self.connected = False
        self.max_retries = 3  # 默认最大重试次数
        self.base_timeout = 15.0  # 基础超时时间（秒）
        self.subdevices = {}  # 存储子设备状态 {地址: {name, state}}
        self.config_entry = None  # 配置条目引用
        self._connection_lock = asyncio.Lock()  # 连接操作锁，避免并发冲突
        self._heartbeat_task: Optional[asyncio.Task] = None  # 心跳任务
        self._reconnect_task: Optional[asyncio.Task] = None  # 重连任务
        self.keep_alive_interval = 30  # 心跳间隔（秒）

    async def connect(self, timeout: Optional[float] = None, retries: Optional[int] = None) -> bool:
        """连接到BLE设备"""
        async with self._connection_lock:  # 确保连接操作互斥
            timeout = timeout or self.base_timeout
            retries = retries or self.max_retries
            
            for attempt in range(retries):
                try:
                    _LOGGER.info(
                        f"尝试连接到设备 {self.device_address}（尝试 {attempt+1}/{retries}），超时时间: {timeout}秒"
                    )
                    
                    # 如果已有客户端实例，先确保断开连接
                    if self.client and self.client.is_connected:
                        await self.disconnect()
                    
                    # 创建新的客户端实例
                    self.client = BleakClient(self.device_address)
                    
                    # 尝试连接
                    _LOGGER.debug(f"发起连接请求到 {self.device_address}")
                    await self.client.connect(timeout=timeout)
                    
                    if self.client.is_connected:
                        self.connected = True
                        _LOGGER.info(f"成功连接到设备 {self.device_address}")
                        
                        # 启动通知监听
                        try:
                            await self.client.start_notify(self.char_uuid, self._notification_handler)
                            _LOGGER.info(f"成功订阅通知: {self.char_uuid}")
                        except Exception as e:
                            _LOGGER.warning(f"订阅通知失败: {str(e)}")

                        # 启动心跳任务
                        self._start_heartbeat()
                        # 注册连接断开回调
                        self.client.set_disconnected_callback(self._on_disconnected)
                        return True
                    
                    _LOGGER.warning(f"连接尝试 {attempt+1} 未成功建立连接")
                    
                except TimeoutError:
                    _LOGGER.warning(
                        f"连接过程超时（尝试 {attempt+1}/{retries}），超时时间: {timeout}秒"
                    )
                except BleakError as e:
                    _LOGGER.error(
                        f"连接过程发生BLE错误（尝试 {attempt+1}/{retries}）: {str(e)}"
                    )
                except Exception as e:
                    _LOGGER.exception(
                        f"连接过程发生意外错误（尝试 {attempt+1}/{retries}）: {str(e)}"
                    )
                
                # 指数退避重试
                if attempt < retries - 1:
                    wait_time = min(5 * (attempt + 1), 30)
                    _LOGGER.info(f"等待 {wait_time} 秒后进行下一次连接尝试")
                    await asyncio.sleep(wait_time)
            
            self.connected = False
            _LOGGER.error(f"所有 {retries} 次连接尝试均失败")
            return False

    async def disconnect(self) -> None:
        """断开与设备的连接并清理资源"""
        async with self._connection_lock:
            self._stop_heartbeat()  # 停止心跳
            if self._reconnect_task and not self._reconnect_task.done():
                self._reconnect_task.cancel()
                self._reconnect_task = None

            if self.client and self.client.is_connected:
                try:
                    _LOGGER.info(f"断开与设备 {self.device_address} 的连接")
                    await self.client.disconnect()
                except Exception as e:
                    _LOGGER.error(f"断开连接时发生错误: {str(e)}")
            
            self.connected = False
            self.client = None

    def _on_disconnected(self, client: BleakClient) -> None:
        """连接断开时的回调处理"""
        if self.connected:
            _LOGGER.warning(f"与设备 {self.device_address} 的连接意外断开")
            self.connected = False
            # 停止心跳任务
            self._stop_heartbeat()
            # 触发自动重连（避免重复创建任务）
            if not self._reconnect_task or self._reconnect_task.done():
                self._reconnect_task = self.hass.loop.create_task(self._persistent_reconnect())

    def _notification_handler(self, sender: int, data: bytearray):
        """处理来自设备的通知数据"""
        # 数据格式: [Header(0xA5), AddrL, AddrH, OpH, OpL, OnOff, Brightness]
        if len(data) < 7 or data[0] != HEADER:
            return
            
        # 解析地址
        address = data[1] + (data[2] << 8)
        
        # 解析状态
        is_on = data[5] != 0
        brightness = data[6]
        
        _LOGGER.debug(f"收到通知: 地址={address}, 开关={is_on}, 亮度={brightness}")
        
        # 更新状态
        if address in self.subdevices:
            self.subdevices[address]["state"] = {
                "on": is_on,
                "brightness": brightness
            }
            self.hass.bus.async_fire(
                f"{DOMAIN}_subdevice_updated",
                {"address": address, "state": self.subdevices[address]["state"]}
            )

    async def _persistent_reconnect(self) -> None:
        """持续重连直到成功，带指数退避策略"""
        attempt = 0
        while not self.connected and self.hass.is_running:
            attempt += 1
            timeout = min(self.base_timeout + (attempt * 5), 60.0)  # 最大超时60秒
            wait_time = min(2 **attempt, 60)  # 指数退避，最大等待60秒

            _LOGGER.info(
                f"持久化重连尝试 {attempt} - 设备 {self.device_address}, "
                f"超时 {timeout}s, 下次重试等待 {wait_time}s"
            )

            if await self.connect(timeout=timeout, retries=1):
                _LOGGER.info(f"持久化重连成功 - 设备 {self.device_address}")
                return

            await asyncio.sleep(wait_time)

    def _start_heartbeat(self) -> None:
        """启动心跳任务"""
        if self._heartbeat_task and not self._heartbeat_task.done():
            return

        async def heartbeat_loop():
            while self.connected and self.hass.is_running:
                try:
                    # 发送心跳命令（根据设备协议调整，示例为0xA5+0x00的空操作帧）
                    heartbeat_cmd = bytearray([HEADER, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])
                    await self.send_command(heartbeat_cmd)
                    _LOGGER.debug(f"已发送心跳包到 {self.device_address}")
                except Exception as e:
                    _LOGGER.warning(f"心跳发送失败: {str(e)}, 将触发重连")
                    self.connected = False
                    self._stop_heartbeat()
                    if not self._reconnect_task or self._reconnect_task.done():
                        self._reconnect_task = self.hass.loop.create_task(self._persistent_reconnect())
                    break  # 退出本轮心跳，等待重连后再启动

                # 等待下一次心跳间隔
                await asyncio.sleep(self.keep_alive_interval)

        self._heartbeat_task = self.hass.loop.create_task(heartbeat_loop())

    def _stop_heartbeat(self) -> None:
        """停止心跳任务"""
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            self._heartbeat_task = None

    async def send_command(self, command: bytes) -> bool:
        """向设备发送原始命令（增强版）"""
        async with self._connection_lock:  # 确保发送操作互斥
            if not self.connected or not self.client or not self.client.is_connected:
                _LOGGER.warning("发送命令失败：未连接到设备，等待重连")
                return False  # 由重连任务负责恢复连接，避免在这里嵌套重连
            
            try:
                await self.client.write_gatt_char(self.char_uuid, command)
                _LOGGER.debug(f"成功发送命令到 {self.device_address}: {command.hex()}")
                return True
            except Exception as e:
                _LOGGER.error(f"发送命令时发生错误: {str(e)}")
                self.connected = False
                self._stop_heartbeat()
                # 触发重连
                if not self._reconnect_task or self._reconnect_task.done():
                    self._reconnect_task = self.hass.loop.create_task(self._persistent_reconnect())
                return False

    async def send_control_command(self, address: int, is_on: bool, brightness: int) -> bool:
        """发送灯光控制命令（修正：确保地址为整数类型）"""
        # 确保地址是整数类型
        if isinstance(address, str):
            try:
                # 尝试从十六进制字符串转换
                address = int(address, 16)
            except ValueError:
                _LOGGER.error(f"无效的地址格式: {address}，必须是整数或十六进制字符串")
                return False
        
        # 直接使用原始亮度值（0-255）
        normalized_brightness = brightness if brightness else 0
        
        # 构建命令帧
        cmd_frame = bytearray([
            HEADER,  # 帧头（0xA5）
            address & 0xFF,             # 设备地址低8位
            (address >> 8) & 0xFF,      # 设备地址高8位
            (CONTROL_CMD >> 8) & 0xFF,  # 命令高8位
            CONTROL_CMD & 0xFF,         # 命令低8位
            0x01 if is_on else 0x00,    # 开关状态
            normalized_brightness,      # 亮度
        ])
        
        # 发送命令并更新本地状态
        success = await self.send_command(cmd_frame)
        if success and address in self.subdevices:
            self.subdevices[address]["state"] = {
                "on": is_on,
                "brightness": normalized_brightness
            }
            # 发送状态更新事件
            self.hass.bus.async_fire(
                f"{DOMAIN}_subdevice_updated",
                {"address": address, "state": self.subdevices[address]["state"]}
            )
        return success

    async def scan_for_device(self, timeout: float = 10.0) -> bool:
        """扫描设备是否在范围内"""
        try:
            scanner = async_get_scanner(self.hass)
            _LOGGER.info(f"扫描设备 {self.device_address}，超时时间: {timeout}秒")
            
            devices = await scanner.async_discover(timeout)
            found = any(device.address == self.device_address for device in devices)
            
            if found:
                _LOGGER.info(f"在扫描中发现设备 {self.device_address}")
                return True
            else:
                _LOGGER.warning(f"扫描超时，未发现设备 {self.device_address}")
                return False
        except Exception as e:
            _LOGGER.error(f"扫描设备时发生错误: {str(e)}")
            return False

    async def __aenter__(self):
        """异步上下文管理器进入方法"""
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        """异步上下文管理器退出方法"""
        await self.disconnect()