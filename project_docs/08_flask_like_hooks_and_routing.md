# PPBase Flask-like Hooks and Routing

## Goal

Allow Python developers to extend PPBase directly with decorators, similar to Flask:

- `@pb.get(...)`, `@pb.post(...)`, `@pb.route(...)`
- `@pb.on_...(...)`

The public facade is available from:

```python
from ppbase import pb, PPBase
```

`pb` is a process-wide singleton.

## Routing API

Available methods:

- `pb.route(path, methods=[...], **fastapi_kwargs)`
- `pb.get(path, **fastapi_kwargs)`
- `pb.post(path, **fastapi_kwargs)`
- `pb.put(path, **fastapi_kwargs)`
- `pb.patch(path, **fastapi_kwargs)`
- `pb.delete(path, **fastapi_kwargs)`
- `pb.options(path, **fastapi_kwargs)`
- `pb.head(path, **fastapi_kwargs)`

Route handlers are standard FastAPI handlers, including typed parameters and `Depends`.

## Hooks API

Lifecycle:

- `pb.on_bootstrap(...)`
- `pb.on_serve(...)`
- `pb.on_terminate(...)`

Records request hooks:

- `pb.on_records_list_request(*collections, ...)`
- `pb.on_record_view_request(*collections, ...)`
- `pb.on_record_create_request(*collections, ...)`
- `pb.on_record_update_request(*collections, ...)`
- `pb.on_record_delete_request(*collections, ...)`

Record auth request hooks:

- `pb.on_record_auth_with_password_request(*collections, ...)`
- `pb.on_record_auth_with_oauth2_request(*collections, ...)`
- `pb.on_record_auth_refresh_request(*collections, ...)`
- `pb.on_record_auth_request(*collections, ...)`

Realtime hooks:

- `pb.on_realtime_connect_request(...)`
- `pb.on_realtime_subscribe_request(...)`
- `pb.on_realtime_message_send(...)`

## Hook chain semantics

Hooks are middleware-like and chain with:

```python
return await e.next()
```

Behavior:

- priority order: higher `priority` runs first
- stable order inside same priority: first registered runs first
- fail-closed default: exceptions stop request flow
- `e.next()` can be called only once per handler
- if `e.next()` is not called, chain is short-circuited

## Transaction behavior (mutations)

For records create/update/delete request hooks:

- Hook chain and default DB write run in the same DB transaction context.
- If a hook raises an exception, the DB mutation is rolled back.

## Module loading patterns

### Side effects

```python
from ppbase import pb
import my_hooks_module  # decorators execute during import
```

### register(pb) style

```python
from ppbase import pb
from my_hooks_module import register

register(pb)
```

### CLI loading

```bash
python -m ppbase serve --hooks package.module:register_hooks
```

`--hooks` is repeatable and works in daemon mode too.

## Route collision policy

At startup, extension routes are checked against:

- core PPBase routes
- other extension routes

Any path+method collision raises a blocking startup error with detailed conflicts.
