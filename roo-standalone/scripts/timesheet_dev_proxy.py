"""Local-only ngrok multiplexer: /timesheet-dev goes to the fake-data runner.

All existing routes continue to the old local Roo server on port 8000. No Slack
signature/body modification, no external forwarding, and no request logging.
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import Response
import httpx
import uvicorn

HOP = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
       'te', 'trailer', 'transfer-encoding', 'upgrade', 'host', 'content-length'}


@asynccontextmanager
async def lifespan(app):
    async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
        app.state.client = client
        yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.api_route('/{path:path}', methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS'])
async def forward(request: Request, path: str):
    raw = request.scope['raw_path']
    prefix = b'/timesheet-dev'
    demo = raw.startswith(prefix + b'/')
    target_path = raw[len(prefix):] if demo else raw
    target = ('http://127.0.0.1:8012' if demo else 'http://127.0.0.1:8000') + target_path.decode('ascii')
    if request.scope['query_string']:
        target += '?' + request.scope['query_string'].decode('ascii')
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP}
    try:
        result = await request.app.state.client.request(request.method, target, content=await request.body(), headers=headers)
    except httpx.HTTPError:
        return Response('Local upstream unavailable', status_code=502)
    response_headers = {k: v for k, v in result.headers.items() if k.lower() not in HOP | {'content-encoding'}}
    return Response(result.content, status_code=result.status_code, headers=response_headers)


if __name__ == '__main__':
    uvicorn.run(app, host='127.0.0.1', port=8013, access_log=False)
