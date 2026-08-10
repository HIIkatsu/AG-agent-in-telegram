"""Power management via SSH and Xiaomi Cloud."""

import asyncio
import json
import logging
import os

from bot.config import settings
from bot.services.ssh_executor import execute_command

try:
    from micloud import MiCloud
except ImportError:
    MiCloud = None

logger = logging.getLogger(__name__)

MI_TOKEN_PATH = "/opt/antigravity-bot/.mi_token.json"


async def soft_shutdown(env_name: str) -> tuple[int, str, str]:
    """Shutdown PC gracefully via SSH."""
    logger.info(f"Initiating soft shutdown for {env_name}")
    # Windows shutdown command: shutdown /s /t 0
    # Linux poweroff command: sudo poweroff (requires NOPASSWD or root)
    # We will try both, ignoring errors on one of them if it fails
    cmd = "shutdown /s /t 0 || sudo poweroff || poweroff"
    return await execute_command(env_name, cmd)


def _init_micloud() -> "MiCloud":
    """Initialize MiCloud, handling token caching."""
    if not MiCloud:
        raise RuntimeError("micloud library is not installed.")
        
    if not settings.xiaomi_user or not settings.xiaomi_pass:
        raise ValueError("Xiaomi credentials are not configured in .env")

    mc = MiCloud(settings.xiaomi_user, settings.xiaomi_pass)
    mc.login_server = settings.xiaomi_server
    
    # Try loading cached token
    if os.path.exists(MI_TOKEN_PATH):
        try:
            with open(MI_TOKEN_PATH, "r", encoding="utf-8") as f:
                cached_data = json.load(f)
            
            # The token property might not be directly assignable in all versions, 
            # but usually micloud provides a way or we just set mc.token
            mc.token = cached_data.get("token")
            # To be safe, we'll verify it by doing a quick call or just let it fail later
            return mc
        except Exception as e:
            logger.warning(f"Failed to load cached mi_token: {e}")

    # If no cache or load failed, login
    logger.info("Logging into Xiaomi Cloud...")
    mc.login()
    
    # Save cache
    try:
        with open(MI_TOKEN_PATH, "w", encoding="utf-8") as f:
            json.dump({"token": mc.token}, f)
    except Exception as e:
        logger.error(f"Failed to save mi_token: {e}")
        
    return mc

def _hard_wake_up_sync(device_name: str) -> str:
    """Synchronous implementation of hard wake up."""
    mc = _init_micloud()
    
    try:
        devices = mc.get_devices()
    except Exception as e:
        # If token is invalid (e.g. 401), we should clear cache and re-login
        logger.warning(f"Failed to get devices, attempting re-login: {e}")
        if os.path.exists(MI_TOKEN_PATH):
            os.remove(MI_TOKEN_PATH)
        mc = _init_micloud()
        devices = mc.get_devices()

    # Find device
    target_device = None
    for dev in devices:
        if dev.get("name", "").lower() == device_name.lower():
            target_device = dev
            break
            
    if not target_device:
        return f"Device '{device_name}' not found."

    # Need did and token to control
    did = target_device.get("did")
    token = target_device.get("token")
    local_ip = target_device.get("localip")

    if not did or not token:
        return f"Device '{device_name}' found, but did/token is missing."

    logger.info(f"Found device: {device_name} (IP: {local_ip})")
    
    import time
    from micloud.micloudexception import MiCloudException
    
    # Turn off first
    try:
        mc.device_ctrl(did, "set_power", ["off"])
        logger.info(f"Turned OFF {device_name}")
    except MiCloudException as e:
        # It might already be off or offline
        logger.info(f"Turn OFF failed (might be already off): {e}")
    except Exception as e:
        logger.error(f"Unexpected error turning off: {e}")
        
    # Wait 3 seconds
    time.sleep(3)
    
    # Turn on
    try:
        mc.device_ctrl(did, "set_power", ["on"])
        logger.info(f"Turned ON {device_name}")
        return f"Successfully power cycled '{device_name}'."
    except Exception as e:
        logger.error(f"Turn ON failed: {e}")
        return f"Error turning on '{device_name}': {e}"


async def hard_wake_up(device_name: str) -> str:
    """Wake up PC by power cycling the smart plug via Xiaomi Cloud."""
    return await asyncio.to_thread(_hard_wake_up_sync, device_name)


def _list_devices_sync() -> str:
    """Synchronously list all devices."""
    mc = _init_micloud()
    try:
        devices = mc.get_devices()
    except Exception as e:
        if os.path.exists(MI_TOKEN_PATH):
            os.remove(MI_TOKEN_PATH)
        mc = _init_micloud()
        devices = mc.get_devices()
        
    result = []
    for dev in devices:
        name = dev.get("name", "Unknown")
        did = dev.get("did", "")
        ip = dev.get("localip", "No IP")
        is_online = dev.get("isOnline", False)
        status = "Online" if is_online else "Offline"
        result.append(f"{name} ({did}) - {ip} - {status}")
        
    return "\n".join(result) if result else "No devices found."


async def list_devices() -> str:
    """List all Xiaomi devices."""
    return await asyncio.to_thread(_list_devices_sync)
