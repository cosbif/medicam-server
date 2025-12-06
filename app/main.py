from fastapi import FastAPI
from app.routes import router as api_router

app = FastAPI(title="Raspberry Camera API")

app.include_router(api_router)

@app.get("/")
async def root():
    return {"message": "Camera API is running"}

# --- mDNS / zeroconf registration ---
# регистрируем сервис при старте приложения, чтобы клиенты могли находить device.local
try:
    from zeroconf import Zeroconf, ServiceInfo
    import socket
    import asyncio

    zeroconf = None
    service_info = None

    def _get_local_ipv4():
        # Попытка получить первый глобальный IPv4 интерфейс
        import subprocess, re
        try:
            out = subprocess.check_output(["ip", "-4", "addr", "show", "scope", "global"], text=True)
            m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", out)
            if m:
                return m.group(1)
        except Exception:
            pass
        # fallback
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "127.0.0.1"

    @app.on_event("startup")
    async def register_mdns():
        global zeroconf, service_info
        try:
            ip = _get_local_ipv4()
            zeroconf = Zeroconf()
            desc = {'path': '/'}
            # имя сервиса: Medicam._http._tcp.local.
            service_info = ServiceInfo(
                "_http._tcp.local.",
                "Medicam._http._tcp.local.",
                addresses=[socket.inet_aton(ip)],
                port=8000,
                properties=desc,
                server="medicam.local."
            )
            zeroconf.register_service(service_info)
            print(f"[mDNS] Registered Medicam on {ip}:8000")
        except Exception as e:
            print(f"[mDNS] registration failed: {e}")

    @app.on_event("shutdown")
    async def unregister_mdns():
        global zeroconf, service_info
        try:
            if zeroconf and service_info:
                zeroconf.unregister_service(service_info)
                zeroconf.close()
                print("[mDNS] Unregistered Medicam")
        except Exception as e:
            print(f"[mDNS] unregister failed: {e}")

except Exception:
    print("[mDNS] zeroconf not available (skip mDNS registration)")
