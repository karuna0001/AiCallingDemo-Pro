# Coolify Runtime Health Check & Diagnostics Guide

This guide details how to configure health checks in Coolify/Traefik and diagnose the `"no available server"` error.

---

## 1. Recommended Health Check Path

* **Liveness Path (Best for Traefik proxy check)**: `/api/live`
  * This is a lightweight endpoint that returns immediately with process uptime and time details.
  * **Why use it**: It does not touch database or third-party connections. If Traefik pings it, it responds in under 5ms, avoiding container timeouts and preventing Traefik from falsely marking the container as down.
* **Readiness Path (Alternative)**: `/api/ready`
  * Verifies that the container can connect to Supabase, WhatsApp API, and LiveKit.
  * **Warning**: Only use this if you want Coolify to hold routing traffic during a complete external service failure. Do not use this as a frequent (e.g. every 5s) health check to prevent load spikes.
* **Diagnostics Path**: `/api/runtime/health`
  * Provides comprehensive runtime diagnostics for troubleshooting, including timezone info, scheduler state, process memory usage, file descriptor counts, and WhatsApp health attributes (such as `whatsapp_enabled`, `whatsapp_provider`, `whatsapp_templates_configured`, and `whatsapp_status`). Do not use this for Traefik proxy checks since it queries dynamic settings.

---

## 2. Recommended Coolify Configuration

* **Health Check Path**: `/api/live`
* **Restart Policy**: `always` or `unless-stopped`
* **Health Check Settings**:
  * **Interval**: 30 seconds
  * **Timeout**: 5 seconds
  * **Retries**: 3
  * **Start Period (Grace Period)**: 15-30 seconds (allows FastAPI to execute startup diagnostics and LiveKit agent worker loop connection before checking status).

---

## 3. Diagnosing "No Available Server"

If you encounter `"no available server"` in Traefik:

1. **Check Container Status**: Verify if the backend container is running or crashed.
2. **Review Application Logs**: Run `docker logs <container_id>` to check for database connection failures or syntax crashes.
3. **Monitor Memory Usage**: Ensure the container has enough memory. You can view memory statistics on `/api/runtime/health`.
4. **Third-Party Integrations**: Confirm Supabase service limits have not been hit (e.g. client connection exhaustion).
5. **Uptime Monitor**: If Traefik periodically loses track of the server due to network sleep, set up an uptime monitor (e.g. Uptime Kuma) to ping `/api/live` every 5 minutes.

---

## 4. File Descriptor Leak & Errno 24

* **Root Cause**: If the container starts logging `OSError: [Errno 24] Too many open files`, this is caused by repeated creation of new async event loops and Supabase/HTTPX clients in background jobs. Because the event loops and HTTP clients were created per-run and never properly garbage-collected, the process leaked socket file descriptors until it reached the system limit, making the server unresponsive.
* **Resolution**:
  * We introduced a single **persistent background event loop** thread started once on app startup. All background sync jobs now schedule their coroutines thread-safely inside this single loop.
  * Overlapping job execution guards were added to skip overlapping runs, and a 45-second execution timeout is enforced.
  * File descriptor limits and current usage can be monitored on `/api/runtime/health` under `open_fds_count`, `open_fds_soft_limit`, and `open_fds_hard_limit`.
  * Coolify restart policy `always` or `unless-stopped` is still highly recommended to recover from external environment leaks.
  * The lightweight liveness path `/api/live` remains completely independent and does not require event loop access.
