# Getting Started

## Prerequisites

- Python 3.11+
- Docker (for the managed PostgreSQL container)
- Node.js 18+ (only if rebuilding the Admin UI)

## Installation

```bash
git clone https://github.com/yourname/ppbase.git
cd ppbase

# Create virtual environment
python -m venv .venv
source .venv/bin/activate        # bash / zsh
source .venv/bin/activate.fish   # fish

# Install package + dev extras
pip install -e ".[dev]"
```

## Start the database

PPBase ships with a `db` CLI sub-command that manages a local Docker container running PostgreSQL 17 on port **5433**:

```bash
python -m ppbase db start    # create & start (first run pulls the image)
python -m ppbase db status   # check if running
python -m ppbase db stop     # stop
python -m ppbase db restart  # restart
```

> **Custom PostgreSQL?** Set `PPBASE_DATABASE_URL` to your connection string and skip this step.

## Create your first admin

```bash
python -m ppbase create-admin --email admin@example.com --password secret123
```

## Run the server

```bash
# Foreground (dev)
python -m ppbase serve

# Background daemon
python -m ppbase serve -d --port 8090

# Stop daemon
python -m ppbase stop

# Check status
python -m ppbase status
```

Or using the shell helper:

```bash
./ppctl.sh start
./ppctl.sh stop
./ppctl.sh status
```

Open **http://127.0.0.1:8090/_/** in your browser to access the Admin UI.

## Your first extension

Create `my_app.py` next to `ppbase/`:

```python
from ppbase import pb

@pb.get("/hello")
async def hello():
    return {"message": "Hello World!", "ok": True}

if __name__ == "__main__":
    pb.start(host="127.0.0.1", port=8090)
```

Run it:

```bash
python my_app.py
curl http://127.0.0.1:8090/hello
# {"message":"Hello World!","ok":true}
```

## Project layout (recommended)

```
my_project/
├── main.py            ← entry-point
├── hooks/
│   ├── users.py       ← user-related hooks
│   ├── posts.py       ← post hooks
│   └── audit.py       ← cross-cutting audit log
├── routes/
│   ├── blog.py        ← blog API routes
│   └── metrics.py     ← internal metrics
└── ppbase/            ← ppbase package (git submodule or pip-installed)
```

```python
# main.py
from ppbase import pb

# load modules — each receives the `pb` facade and registers its hooks/routes
pb.load_hooks("hooks.users:setup")
pb.load_hooks("hooks.posts:setup")
pb.load_hooks("hooks.audit:setup")
pb.load_hooks("routes.blog:setup")
pb.load_hooks("routes.metrics:setup")

if __name__ == "__main__":
    pb.start()
```

```python
# hooks/users.py
def setup(pb):
    from ppbase.ext.events import RecordRequestEvent

    @pb.on_record_create_request("users")
    async def on_user_create(event: RecordRequestEvent):
        event.data.setdefault("role", "member")
        return await event.next()
```

See [Multi-file Projects](./multi-file.md) for the full pattern.

## Verify the app boots

```bash
python -c "from ppbase.app import create_app; app = create_app(); print('OK')"
```
