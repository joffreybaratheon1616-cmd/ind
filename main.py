#!/usr/bin/env python3
"""
╔═══════════════════════════════════════════════════════════════╗
║         Private — Session Management Platform         ║
╚═══════════════════════════════════════════════════════════════╝
"""

import asyncio
import os
import sqlite3

# Optimize file descriptor limit for 500+ concurrent user sessions
try:
    import resource
    _soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (min(65536, _hard), _hard))
except Exception:
    pass

import config
import session_core
import operations
import handlers

# Re-export everything from major modules for 100% backward compatibility
from config import *
from session_core import *
from operations import *
from handlers import *

for _m in (config, session_core, operations, handlers):
    for _k, _v in vars(_m).items():
        if not _k.startswith('__'):
            globals()[_k] = _v

async def handle_health_check(_reader, writer):
    try:
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK")
        await writer.drain()
    except (ConnectionResetError, BrokenPipeError, OSError):
        pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

async def start_web_server():
    """Starts a dummy web server to satisfy KataBump's / Wispbyte's port-binding requirement."""
    port = int(os.environ.get("PORT", os.environ.get("SERVER_PORT", 8080)))
    try:
        server = await asyncio.start_server(
            handle_health_check, '0.0.0.0', port,
            reuse_address=True, reuse_port=True
        )
    except OSError:
        # Kill whatever is holding the port and retry
        os.system(f"fuser -k {port}/tcp 2>/dev/null")
        await asyncio.sleep(1)
        server = await asyncio.start_server(
            handle_health_check, '0.0.0.0', port,
            reuse_address=True, reuse_port=True
        )
    print(f"🌐 Web health server started on 0.0.0.0:{port}")
    return server

if __name__ == "__main__":
    ensure_storage()
    try:
        _meta_startup = load_meta()
        GLOBAL_ANNOUNCEMENT = _meta_startup.get("announcement", "")
        MAINTENANCE_MODE = _meta_startup.get("maintenance", False)
        WHITELIST_MODE = _meta_startup.get("whitelist", False)
        for _adm in _meta_startup.get("admins", []):
            if _adm not in ADMIN_IDS:
                ADMIN_IDS.append(_adm)
    except Exception:
        pass
    print(f"💎 {PLATFORM_NAME} is online.")  # Tell the terminal it's alive
    try:
        loop = asyncio.get_event_loop()
        import concurrent.futures
        loop.set_default_executor(concurrent.futures.ThreadPoolExecutor(max_workers=64, thread_name_prefix="AsyncWorker"))
        loop.run_until_complete(start_web_server())
        
        async def _preflight_telegram_reachability():
            """Quick TCP probe to Telegram DC2. Prints guidance if the host blocks TG."""
            import socket
            targets = [
                ("149.154.167.50", 443),   # DC2
                ("149.154.167.91", 443),   # DC4
                ("91.108.56.130", 443),    # DC5
            ]
            ok = False
            for host, port in targets:
                try:
                    fut = asyncio.get_running_loop().run_in_executor(
                        None,
                        lambda h=host, p=port: socket.create_connection((h, p), timeout=8),
                    )
                    sock = await asyncio.wait_for(fut, timeout=10)
                    try:
                        sock.close()
                    except Exception:
                        pass
                    ok = True
                    print(f"[preflight] Reachable {host}:{port}")
                    break
                except Exception as e:
                    print(f"[preflight] Unreachable {host}:{port} ({type(e).__name__})")
            if not ok and not PYRO_PROXY:
                print(
                    "⚠️  Host cannot reach Telegram DCs and no PROXY_* is set.\n"
                    "    Add to .env, e.g.:\n"
                    "      PROXY_TYPE=socks5\n"
                    "      PROXY_HOST=your.proxy.ip\n"
                    "      PROXY_PORT=1080\n"
                    "      PROXY_USER=...\n"
                    "      PROXY_PASS=...\n"
                    "    Then restart. Bot client will route through that proxy."
                )
            return ok

        async def main_runner():
            app.loop = asyncio.get_running_loop()
            await _preflight_telegram_reachability()

            # If PROXY_* was set after Client() construction (rare), re-apply.
            if PYRO_PROXY and getattr(app, "proxy", None) != PYRO_PROXY:
                try:
                    app.proxy = PYRO_PROXY
                except Exception:
                    pass

            max_attempts = int(os.environ.get("TG_CONNECT_RETRIES", "10"))
            connected = False
            last_err = None
            for _attempt in range(max_attempts):
                try:
                    print(f"🚀 Calling app.start()... (attempt {_attempt + 1}/{max_attempts})")
                    # Bound the whole start so a hung TCP does not sit forever
                    await asyncio.wait_for(app.start(), timeout=45)
                    print("✅ app.start() successful!")
                    connected = True
                    break
                except (TimeoutError, asyncio.TimeoutError, ConnectionError, OSError) as e:
                    last_err = e
                    delay = min(30, 3 * (2 ** min(_attempt, 4)))  # 3,6,12,24,30...
                    print(
                        f"⏳ Telegram connection failed (attempt {_attempt + 1}/{max_attempts}): "
                        f"{type(e).__name__}: {e} — retrying in {delay}s..."
                    )
                    # Soft reconnect cleanup between attempts
                    try:
                        if getattr(app, "is_connected", False):
                            await asyncio.wait_for(app.disconnect(), timeout=5)
                    except Exception:
                        pass
                    await asyncio.sleep(delay)
                except Exception as e:
                    last_err = e
                    delay = min(20, 5 + _attempt * 2)
                    print(
                        f"⏳ Unexpected start error (attempt {_attempt + 1}/{max_attempts}): "
                        f"{type(e).__name__}: {e} — retrying in {delay}s..."
                    )
                    await asyncio.sleep(delay)
            if not connected:
                print("❌ Failed to connect to Telegram after "
                      f"{max_attempts} attempts. Last error: {type(last_err).__name__}: {last_err}")
                if not PYRO_PROXY:
                    print(
                        "💡 Tip: this host likely blocks Telegram. Set PROXY_TYPE / PROXY_HOST / "
                        "PROXY_PORT in .env and redeploy."
                    )
                return
            if ADMIN_IDS:
                for admin_id in ADMIN_IDS:
                    try:
                        print(f"✉️ Sending startup message to {admin_id}...")
                        await app.send_message(admin_id, f"🟢 **Bot Online**\n\n{PLATFORM_NAME} has been successfully started/restarted and is now active.")
                    except Exception as e:
                        print(f"⚠️ Failed to send startup message: {e}")
            
            async def connection_watchdog():
                """Heartbeat monitor: ensures bot stays responsive and auto-recovers if disconnected."""
                heartbeat_count = 0
                while True:
                    await asyncio.sleep(20)
                    heartbeat_count += 1
                    try:
                        if not getattr(app, "is_connected", False):
                            print("⚠️ [Watchdog] app.is_connected is False! Reconnecting...")
                            try:
                                await asyncio.wait_for(app.connect(), timeout=15)
                                print("✅ [Watchdog] Reconnected successfully!")
                            except Exception as e:
                                print(f"❌ [Watchdog] Reconnect attempt failed: {e}")
                        elif heartbeat_count % 3 == 0:  # Active MTProto health check every 60s
                            try:
                                await asyncio.wait_for(app.get_me(), timeout=10)
                            except Exception as ping_err:
                                print(f"⚠️ [Watchdog] Active ping warning: {ping_err}")
                    except Exception:
                        pass

            watchdog_task = asyncio.create_task(connection_watchdog())
            import pyrogram
            print("💤 Entering pyrogram.idle()...")
            try:
                await pyrogram.idle()
            finally:
                watchdog_task.cancel()
            print("🛑 Exiting pyrogram.idle()...")
            await app.stop()
            
        loop.run_until_complete(main_runner())
    except KeyboardInterrupt:
        pass
    except sqlite3.OperationalError as e:
        if "database is locked" in str(e):
            print(f"\n❌ Error: The session database '{ROOT_DIR}/accstore_engine.session' is locked.")
            print("Another instance of the bot is already running in the background and using this file.")
            print("To fix this, please close or kill the other process. You can find it using:")
            print(f"  lsof {ROOT_DIR}/accstore_engine.session")
            print("And kill it using: kill -9 <PID>")
            import sys
            sys.exit(1)
        else:
            raise
    finally:
        # Clean up any pending asyncio tasks (like hanging Telethon client loops)
        try:
            loop = asyncio.get_event_loop()
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        except Exception:
            pass

