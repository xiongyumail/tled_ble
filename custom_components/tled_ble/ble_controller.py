# tled_ble/ble_controller.py
import asyncio
import logging
from typing import Optional
from bleak import BleakClient, BleakError
from homeassistant.components.bluetooth import async_get_scanner, async_ble_device_from_address
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError

from .const import (
    HEADER,
    CONTROL_CMD,
    QUERY_CMD,
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
        self.notify_uuid = None               # 自动发现的 Notify UUID
        self.name = ""  # 设备名称（从配置中获取）
        self.client: Optional[BleakClient] = None
        self.connected = False
        self.max_retries = 3  # 默认最大重试次数
        self.base_timeout = 15.0  # 基础超时时间（秒）
        self.subdevices = {}  # 存储子设备状态 {地址: {name, state}}
        self.gateway_address = 0x0001  # 网关自身的 Mesh 地址
        self.config_entry = None  # 配置条目引用
        self._connection_lock = asyncio.Lock()  # 连接操作锁，避免并发冲突
        self._heartbeat_task: Optional[asyncio.Task] = None  # 心跳任务
        self._reconnect_task: Optional[asyncio.Task] = None  # 重连任务
        self.keep_alive_interval = 30  # 心跳间隔（秒）

    async def connect(self, timeout: Optional[float] = None, retries: Optional[int] = None) -> bool:
        """连接到BLE设备"""
        async with self._connection_lock:  # 确保连接操作互斥
            # 检查是否已连接
            if self.connected and self.client and self.client.is_connected:
                return True

            timeout = timeout or self.base_timeout
            retries = retries or self.max_retries
            
            for attempt in range(retries):
                try:
                    _LOGGER.info(
                        f"尝试连接到设备 {self.device_address}（尝试 {attempt+1}/{retries}），超时时间: {timeout}秒"
                    )
                    
                    # 确保清理旧的连接状态
                    await self._cleanup_client()
                    
                    # 创建新的客户端实例
                    # 优化：优先从 HA 蓝牙管理器获取设备对象（这样可以自动支持 ESPHome 代理）
                    ble_device = async_ble_device_from_address(self.hass, self.device_address, connectable=True)
                    if ble_device:
                        self.client = BleakClient(ble_device)
                        _LOGGER.debug(f"通过 HA 蓝牙管理器连接到设备 (支持代理): {ble_device}")
                    else:
                        self.client = BleakClient(self.device_address)
                        _LOGGER.warning(f"未在 HA 蓝牙缓存中找到设备，尝试直接使用地址连接: {self.device_address}")
                    
                    # 尝试连接
                    _LOGGER.debug(f"发起连接请求到 {self.device_address}")
                    await self.client.connect(timeout=timeout)
                    
                    if self.client.is_connected:
                        self.connected = True
                        _LOGGER.info(f"成功连接到设备 {self.device_address}")
                        
                        # 启动通知监听
                        try:
                            # 1. 确定用于通知的 UUID
                            target_notify_uuid = None
                            
                            # 获取服务对象
                            service = self.client.services.get_service(self.service_uuid)
                            if service:
                                # 遍历服务下的特征值，寻找支持 notify 的
                                for char in service.characteristics:
                                    if "notify" in char.properties:
                                        target_notify_uuid = char.uuid
                                        # 如果找到的正好是配置的 UUID，优先使用
                                        if char.uuid == self.char_uuid:
                                            break
                            
                            # 如果服务里没找到，或者是通过 UUID 直接连接的某些特殊情况，回退尝试配置的 UUID
                            if not target_notify_uuid:
                                char = self.client.services.get_characteristic(self.char_uuid)
                                if char and "notify" in char.properties:
                                    target_notify_uuid = self.char_uuid

                            # 2. 执行订阅
                            if target_notify_uuid:
                                await self.client.start_notify(target_notify_uuid, self._notification_handler)
                                self.notify_uuid = target_notify_uuid
                                _LOGGER.info(f"成功订阅通知 UUID: {target_notify_uuid} (配置的读写 UUID: {self.char_uuid})")
                            else:
                                _LOGGER.warning(f"在服务 {self.service_uuid} 下未找到支持 Notify 的特征值，尝试使用配置 UUID")
                                # 最后的尝试
                                await self.client.start_notify(self.char_uuid, self._notification_handler)
                                self.notify_uuid = self.char_uuid
                                
                        except Exception as e:
                            _LOGGER.warning(f"订阅通知失败: {str(e)}")

                        # 启动心跳任务
                        self._start_heartbeat()
                        # 注册连接断开回调
                        self.client.set_disconnected_callback(self._on_disconnected)
                        
                        # 连接建立后，主动查询所有子设备状态
                        for addr in self.subdevices:
                            self.hass.loop.create_task(self.send_query_command(addr))
                            # 稍微错开一点时间，避免瞬时拥塞
                            await asyncio.sleep(0.1)

                        # 延迟 3 秒后再启动 Mesh 扫描，确保连接初期稳定
                        self.hass.loop.call_later(3.0, lambda: self.hass.loop.create_task(self.async_scan_mesh(20)))

                        return True
                    
                    _LOGGER.warning(f"连接尝试 {attempt+1} 未成功建立连接")
                    
                except TimeoutError:
                    _LOGGER.warning(
                        f"连接过程超时（尝试 {attempt+1}/{retries}），超时时间: {timeout}秒"
                    )
                except BleakError as e:
                    error_msg = str(e)
                    if "Operation already in progress" in error_msg or "br-connection-canceled" in error_msg:
                        _LOGGER.warning(f"BLE操作繁忙或被取消，将在退避后重试: {error_msg}")
                        # 遇到此类错误，额外增加等待时间，让 BlueZ 有时间清理
                        await asyncio.sleep(2.0)
                        
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

    async def _cleanup_client(self):
        """内部清理客户端资源（不加锁）"""
        self._stop_heartbeat()
        # 修复：防止重连任务在执行过程中取消自身，导致重连中断
        current_task = asyncio.current_task()
        if self._reconnect_task and self._reconnect_task != current_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
            self._reconnect_task = None

        if self.client:
            try:
                # 尝试断开连接，无论当前状态如何
                await self.client.disconnect()
            except Exception as e:
                _LOGGER.debug(f"清理连接时发生错误: {str(e)}")
            finally:
                self.client = None
        
        self.connected = False

    async def disconnect(self) -> None:
        """断开与设备的连接并清理资源"""
        async with self._connection_lock:
            _LOGGER.info(f"断开与设备 {self.device_address} 的连接")
            await self._cleanup_client()

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
        
        _LOGGER.debug(f"收到通知: 地址=0x{address:04X}, 开关={is_on}, 亮度={brightness}")
        
        # 自动发现逻辑：如果地址不在已知子设备列表中，则自动添加
        if address not in self.subdevices:
            _LOGGER.info(f"发现新Mesh设备！地址: 0x{address:04X}")
            self.hass.loop.create_task(self._async_add_discovered_subdevice(address, is_on, brightness))
            return

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

    async def _async_add_discovered_subdevice(self, address: int, is_on: bool, brightness: int):
        """异步添加发现的子设备并通知系统"""
        if address in self.subdevices:
            return

        name = f"{address:04X}"
        self.subdevices[address] = {
            "name": name,
            "state": {"on": is_on, "brightness": brightness}
        }
        
        # 触发事件，让 light.py 动态添加实体
        self.hass.bus.async_fire(
            f"{DOMAIN}_new_subdevice_found",
            {
                "controller_mac": self.mac_address,
                "address": address, 
                "name": name,
                "state": self.subdevices[address]["state"]
            }
        )
        
        # 更新配置条目以实现持久化
        if self.config_entry:
            new_options = dict(self.config_entry.options)
            subdevices_config = new_options.get("subdevices", {}).copy()
            # 存入配置时将地址转为字符串，保持与现有格式一致
            subdevices_config[str(address)] = {
                "name": name,
                "state": self.subdevices[address]["state"]
            }
            new_options["subdevices"] = subdevices_config
            self.hass.config_entries.async_update_entry(self.config_entry, options=new_options)

    async def async_scan_mesh(self, scan_range: int = 16):
        """主动扫描Mesh网络中的设备（查询 0x0001 到 scan_range 的地址）"""
        _LOGGER.info(f"大王，正在为您慢速巡视 Mesh 领地 (扫描前 {scan_range} 个地址)...")
        for addr in range(1, scan_range + 1):
            if not self.connected:
                break
            # 如果地址已经在已知列表中，跳过查询
            if addr in self.subdevices:
                continue
            await self.send_query_command(addr)
            await asyncio.sleep(0.6)  # 增加扫描间隔，保护网关不被淹没

    async def _persistent_reconnect(self) -> None:
        """持续重连直到成功，带指数退避策略"""
        attempt = 0
        # 初始等待 5 秒，给蓝牙堆栈和网关一些清理时间
        await asyncio.sleep(5.0)
        
        while not self.connected and self.hass.is_running:
            attempt += 1
            timeout = min(self.base_timeout + (attempt * 5), 60.0)  # 最大超时60秒
            wait_time = min(2 **attempt, 60)  # 指数退避，最大等待60秒

            _LOGGER.info(
                f"持久化重连尝试 {attempt} - 设备 {self.device_address}, "
                f"超时 {timeout}s, 下次重试等待 {wait_time}s"
            )

            # 连续重连失败时，通过扫描尝试刷新 HA 蓝牙设备的缓存状态
            if attempt > 1 and attempt % 2 == 0:
                _LOGGER.debug(f"尝试扫描以唤醒设备缓存: {self.device_address}")
                await self.scan_for_device(timeout=5.0)

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

                    # 连接状态下主动获取并更新 RSSI
                    if self.client and self.client.is_connected:
                        try:
                            rssi = await self.client.get_rssi()
                            self.hass.bus.async_fire(
                                f"{DOMAIN}_rssi_updated",
                                {"address": self.mac_address, "rssi": rssi}
                            )
                        except Exception:
                            pass
                    
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
                # 仅针对非心跳/查询的控制命令在断连时输出日志
                if len(command) >= 5 and (command[3] != 0 or command[4] != 0):
                    _LOGGER.debug(f"设备断开中，忽略发送命令: {command.hex()}")
                return False  # 由重连任务负责恢复连接，避免在这里嵌套重连
            
            try:
                # 使用 response=False (Write Without Response)，对于 Mesh 命令更稳定且响应更快
                await self.client.write_gatt_char(self.char_uuid, command, response=False)
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

    async def send_query_command(self, address: int) -> bool:
        """发送状态查询命令"""
        if isinstance(address, str):
            try:
                address = int(address, 16)
            except ValueError:
                return False
        
        # 构建命令帧 [Header, AddrL, AddrH, OpH, OpL, 0x00, 0x00]
        cmd_frame = bytearray([
            HEADER,
            address & 0xFF,
            (address >> 8) & 0xFF,
            (QUERY_CMD >> 8) & 0xFF,
            QUERY_CMD & 0xFF,
            0x00,
            0x00,
        ])
        return await self.send_command(cmd_frame)

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
        """扫描设备是否在范围内（优化：直接使用 HA 的发现缓存）"""
        try:
            from homeassistant.components.bluetooth import async_discovered_service_info
            
            _LOGGER.debug(f"正在为您检查蓝牙缓存，寻找设备 {self.device_address}...")
            
            # 获取所有当前可见的蓝牙设备信息
            for service_info in async_discovered_service_info(self.hass, connectable=True):
                if service_info.address == self.device_address:
                    _LOGGER.info(f"大王！在缓存中找到了设备 {self.device_address} ({service_info.name})")
                    return True
            
            _LOGGER.warning(f"缓存中未发现设备 {self.device_address}")
            return False
        except Exception as e:
            _LOGGER.error(f"扫描设备时发生意外错误: {str(e)}")
            return False

    async def __aenter__(self):
        """异步上下文管理器进入方法"""
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        """异步上下文管理器退出方法"""
        await self.disconnect()