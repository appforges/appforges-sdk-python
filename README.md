# AppForges Python SDK

Official synchronous and asynchronous Python SDK. Python 3.11 or newer.
Generated from the same OpenAPI specification tag as the TypeScript SDK.

Install: `pip install appforges==0.65.0`

```python
import os
from appforges import AppForges, BearerAuth, APIError

with AppForges(auth=BearerAuth(os.environ["APPFORGES_ACCESS_TOKEN"])) as client:
    try:
        profile = client.users.get_profile()
        print(profile.user_id)
    except APIError as error:
        print(error.http_status, error.reason, error.request_id, error.trace_id)
        raise
```

```python
import asyncio
import os
from appforges import AsyncAppForges, BearerAuth

async def main() -> None:
    async with AsyncAppForges(auth=BearerAuth(os.environ["APPFORGES_ACCESS_TOKEN"])) as client:
        profile = await client.users.get_profile()
        print(profile.user_id)

asyncio.run(main())
```

The profile endpoint requires a user OAuth access token. Set base_url to
https://api-staging.appforges.com for staging. Wire fields retain snake_case;
unset fields are omitted, explicit None is sent only for nullable fields.
Models use strict pydantic v2 validation. Both clients support Bearer/API key
authentication, OAuth helpers, typed errors, bounded safe retries (default two),
cursor iteration (`*_all()`) and SSE iterators where specified by the API.
Resources expose endpoint-specific methods from the complete specification.
Clients close owned HTTP transports; injected transports remain caller-owned.
See https://developer.appforges.com for authentication and API reference.
