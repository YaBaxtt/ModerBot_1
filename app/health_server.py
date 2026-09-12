from __future__ import annotations

from aiohttp import web


async def start_health_server(port: int | None) -> web.AppRunner | None:
    """Expose a tiny endpoint for web-style hosting platforms when PORT is set."""
    if not port:
        return None

    async def health(_request: web.Request) -> web.Response:
        return web.json_response({'status': 'ok', 'service': 'moderbot'})

    app = web.Application()
    app.router.add_get('/', health)
    app.router.add_get('/health', health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, '0.0.0.0', port).start()
    return runner
