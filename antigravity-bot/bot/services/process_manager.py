"""Process Manager for handling background processes and tunnels."""

import asyncio
import logging
import os
import re
import signal

from bot.db import db

logger = logging.getLogger(__name__)

# Regular expression to extract URL from tunnel output
URL_PATTERN = re.compile(r'(https?://[^\s]+)')

async def _monitor_tunnel_output(process: asyncio.subprocess.Process, process_id: int):
    """Read stdout/stderr to find the tunnel URL and update DB."""
    if not process.stdout:
        return

    while True:
        try:
            line = await process.stdout.readline()
            if not line:
                break
            line_str = line.decode('utf-8', errors='ignore')
            logger.debug(f"Tunnel {process_id} output: {line_str.strip()}")
            match = URL_PATTERN.search(line_str)
            if match:
                url = match.group(1)
                logger.info(f"Found tunnel URL for process {process_id}: {url}")
                await db.update_background_process(process_id, status='running', url=url)
                # Found the URL, stop monitoring if we want, or keep draining to avoid buffer fill.
                # It's safer to keep draining stdout so it doesn't block.
        except Exception as e:
            logger.error(f"Error reading tunnel output for process {process_id}: {e}")
            break

async def start_process(command: str, thread_id: int, project_id: int | None, type_: str) -> int:
    """Start a background process and save it to the database."""
    # os.setsid is Unix-only, this code is expected to run on Linux where the bot is deployed
    try:
        if os.name == 'posix':
            # Use asyncio.create_subprocess_shell
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE if type_ == 'tunnel' else asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.STDOUT if type_ == 'tunnel' else asyncio.subprocess.DEVNULL,
                preexec_fn=os.setsid
            )
        else:
            # Fallback for Windows (testing)
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE if type_ == 'tunnel' else asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.STDOUT if type_ == 'tunnel' else asyncio.subprocess.DEVNULL
            )
            
        # Write PID to database
        process_id = await db.create_background_process(
            thread_id=thread_id,
            project_id=project_id,
            type_=type_,
            pid=process.pid
        )
        logger.info(f"Started background process {process_id} (PID {process.pid}) for command: {command}")

        if type_ == 'tunnel':
            asyncio.create_task(_monitor_tunnel_output(process, process_id))
            
        return process_id
    except Exception as e:
        logger.error(f"Failed to start process '{command}': {e}")
        raise

async def stop_process(process_id: int) -> bool:
    """Stop a background process by killing its process group."""
    process_info = await db.get_background_process(process_id)
    if not process_info:
        logger.warning(f"Process {process_id} not found in database.")
        return False
        
    pid = process_info['pid']
    
    if os.name == 'posix':
        try:
            logger.info(f"Killing process group for PID {pid}")
            os.killpg(pid, signal.SIGTERM)
            
            # Allow some time to terminate gracefully
            await asyncio.sleep(1)
            
            # Send SIGKILL just in case it's still alive
            try:
                os.killpg(pid, 0) # Check if it exists
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                pass # Process group doesn't exist anymore
                
        except OSError as e:
            logger.warning(f"Error killing process {pid}: {e}")
    else:
        # Fallback for Windows
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass

    await db.update_background_process(process_id, status='stopped')
    return True

async def stop_all_processes(thread_id: int) -> int:
    """Stop all active processes for a specific thread_id."""
    processes = await db.list_background_processes(thread_id, include_stopped=False)
    count = 0
    for proc in processes:
        if proc['status'] == 'running':
            await stop_process(proc['id'])
            count += 1
    return count
