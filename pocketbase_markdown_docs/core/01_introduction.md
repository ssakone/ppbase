# Introduction

> Source: https://pocketbase.io/docs/

---

Introduction

Please keep in mind that PocketBase is still under active development and full backward compatibility is not guaranteed before reaching v1.0.0. PocketBase is NOT recommended for production critical applications yet, unless you are fine with reading the [changelog](https://github.com/pocketbase/pocketbase/blob/master/CHANGELOG.md) and applying some manual migration steps from time to time.

PocketBase is an open source backend consisting of embedded database (SQLite) with realtime subscriptions, builtin auth management, convenient dashboard UI and simple REST-ish API. It can be used both as Go framework and as standalone application.

The easiest way to get started is to download the prebuilt minimal PocketBase executable:

x64 ARM64

-   [Download v0.36.4 for Linux x64](https://github.com/pocketbase/pocketbase/releases/download/v0.36.4/pocketbase_0.36.4_linux_amd64.zip) (~12MB zip)

-   [Download v0.36.4 for Windows x64](https://github.com/pocketbase/pocketbase/releases/download/v0.36.4/pocketbase_0.36.4_windows_amd64.zip) (~12MB zip)

-   [Download v0.36.4 for macOS x64](https://github.com/pocketbase/pocketbase/releases/download/v0.36.4/pocketbase_0.36.4_darwin_amd64.zip) (~12MB zip)


-   [Download v0.36.4 for Linux ARM64](https://github.com/pocketbase/pocketbase/releases/download/v0.36.4/pocketbase_0.36.4_linux_arm64.zip) (~11MB zip)

-   [Download v0.36.4 for Windows ARM64](https://github.com/pocketbase/pocketbase/releases/download/v0.36.4/pocketbase_0.36.4_windows_arm64.zip) (~11MB zip)

-   [Download v0.36.4 for macOS ARM64](https://github.com/pocketbase/pocketbase/releases/download/v0.36.4/pocketbase_0.36.4_darwin_arm64.zip) (~11MB zip)


See the [GitHub Releases page](https://github.com/pocketbase/pocketbase/releases) for other platforms and more details.

* * *

Once you've extracted the archive, you could start the application by running `**./pocketbase serve**` in the extracted directory.

**And that's it!** The first time it will generate an installer link that should be automatically opened in the browser to set up your first superuser account (you can also create the first superuser manually via `./pocketbase superuser create EMAIL PASS`) .

The started web server has the following default routes:

-   [`http://127.0.0.1:8090`](http://127.0.0.1:8090) - if `pb_public` directory exists, serves the static content from it (html, css, images, etc.)
-   [`http://127.0.0.1:8090/_/`](http://127.0.0.1:8090/_/) - superusers dashboard
-   [`http://127.0.0.1:8090/api/`](http://127.0.0.1:8090/api/) - REST-ish API

The prebuilt PocketBase executable will create and manage 2 new directories alongside the executable:

-   `pb_data` - stores your application data, uploaded files, etc. (usually should be added in `.gitignore`).
-   `pb_migrations` - contains JS migration files with your collection changes (can be safely committed in your repository).

    You can even write custom migration scripts. For more info check the [JS migrations docs](/docs/js-migrations).


You could find all available commands and their options by running `./pocketbase --help` or `./pocketbase [command] --help`