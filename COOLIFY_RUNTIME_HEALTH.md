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
