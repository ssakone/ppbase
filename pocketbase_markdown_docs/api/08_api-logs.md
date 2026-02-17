# Web API - Logs

> Source: https://pocketbase.io/docs/api-logs/

---

API Logs

**[List logs](#list-logs)**

Returns a paginated logs list.

Only superusers can perform this action.

JavaScript

Dart

`import PocketBase from 'pocketbase'; const pb = new PocketBase('http://127.0.0.1:8090'); ... await pb.collection("_superusers").authWithPassword('test@example.com', '1234567890'); const pageResult = await pb.logs.getList(1, 20, { filter: 'data.status >= 400' });`

`import 'package:pocketbase/pocketbase.dart'; final pb = PocketBase('http://127.0.0.1:8090'); ... await pb.collection("_superusers").authWithPassword('test@example.com', '1234567890'); final pageResult = await pb.logs.getList( page: 1, perPage: 20, filter: 'data.status >= 400', );`

###### API details

**GET**

/api/logs

Requires `Authorization:TOKEN`

Query parameters

Param

Type

Description

page

Number

The page (aka. offset) of the paginated list (_default to 1_).

perPage

Number

The max returned logs per page (_default to 30_).

sort

String

Specify the _ORDER BY_ fields.

Add `-` / `+` (default) in front of the attribute for DESC / ASC order, e.g.:

`// DESC by the insertion rowid and ASC by level ?sort=-rowid,level`

**Supported log sort fields:**
`@random`, `rowid`, `id`, `created`, `updated`, `level`, `message` and any `data.*` attribute.

filter

String

Filter expression to filter/search the returned logs list, e.g.:

`?filter=(data.url~'test.com' && level>0)`

**Supported log filter fields:**
`id`, `created`, `updated`, `level`, `message` and any `data.*` attribute.

The syntax basically follows the format `OPERAND OPERATOR OPERAND`, where:

-   `OPERAND` - could be any field literal, string (single or double quoted), number, null, true, false
-   `OPERATOR` - is one of:
    -   `=` Equal
    -   `!=` NOT equal
    -   `>` Greater than
    -   `>=` Greater than or equal
    -   `<` Less than
    -   `<=` Less than or equal
    -   `~` Like/Contains (if not specified auto wraps the right string OPERAND in a "%" for wildcard match)
    -   `!~` NOT Like/Contains (if not specified auto wraps the right string OPERAND in a "%" for wildcard match)
    -   `?=` _Any/At least one of_ Equal
    -   `?!=` _Any/At least one of_ NOT equal
    -   `?>` _Any/At least one of_ Greater than
    -   `?>=` _Any/At least one of_ Greater than or equal
    -   `?<` _Any/At least one of_ Less than
    -   `?<=` _Any/At least one of_ Less than or equal
    -   `?~` _Any/At least one of_ Like/Contains (if not specified auto wraps the right string OPERAND in a "%" for wildcard match)
    -   `?!~` _Any/At least one of_ NOT Like/Contains (if not specified auto wraps the right string OPERAND in a "%" for wildcard match)

To group and combine several expressions you can use parenthesis `(...)`, `&&` (AND) and `||` (OR) tokens.

Single line comments are also supported: `// Example comment`.

Field expressions with array-like value or nested fields that originate from a source with multiple records will apply a **match-all** constraint by default. If you want **any/at-least-one-of** type of constraint for such fields you'll have to prefix your operator with `?` (e.g. `multiRelation.title ?= "test"`).

fields

String

Comma separated string of the fields to return in the JSON response _(by default returns all fields)_. Ex.:

`?fields=*,expand.relField.name`

`*` targets all keys from the specific depth level.

In addition, the following field modifiers are also supported:

-   `:excerpt(maxLength, withEllipsis?)`
    Returns a short plain text version of the field string value.
    Ex.: `?fields=*,description:excerpt(200,true)`

Responses

200 400 401 403

`{ "page": 1, "perPage": 20, "totalItems": 2, "items": [ { "id": "ai5z3aoed6809au", "created": "2024-10-27 09:28:19.524Z", "data": { "auth": "_superusers", "execTime": 2.392327, "method": "GET", "referer": "http://localhost:8090/_/", "remoteIP": "127.0.0.1", "status": 200, "type": "request", "url": "/api/collections/_pbc_2287844090/records?page=1&perPage=1&filter=&fields=id", "userAgent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36", "userIP": "127.0.0.1" }, "message": "GET /api/collections/_pbc_2287844090/records?page=1&perPage=1&filter=&fields=id", "level": 0 }, { "id": "26apis4s3sm9yqm", "created": "2024-10-27 09:28:19.524Z", "data": { "auth": "_superusers", "execTime": 2.392327, "method": "GET", "referer": "http://localhost:8090/_/", "remoteIP": "127.0.0.1", "status": 200, "type": "request", "url": "/api/collections/_pbc_2287844090/records?page=1&perPage=1&filter=&fields=id", "userAgent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36", "userIP": "127.0.0.1" }, "message": "GET /api/collections/_pbc_2287844090/records?page=1&perPage=1&filter=&fields=id", "level": 0 } ] }`

`{ "status": 400, "message": "Something went wrong while processing your request. Invalid filter.", "data": {} }`

`{ "status": 401, "message": "The request requires valid record authorization token.", "data": {} }`

`{ "status": 403, "message": "The authorized record is not allowed to perform this action.", "data": {} }`

**[View log](#view-log)**

Returns a single log by its ID.

Only superusers can perform this action.

JavaScript

Dart

`import PocketBase from 'pocketbase'; const pb = new PocketBase('http://127.0.0.1:8090'); ... await pb.collection("_superusers").authWithEmail('test@example.com', '123456'); const log = await pb.logs.getOne('LOG_ID');`

`import 'package:pocketbase/pocketbase.dart'; final pb = PocketBase('http://127.0.0.1:8090'); ... await pb.collection("_superusers").authWithEmail('test@example.com', '123456'); final log = await pb.logs.getOne('LOG_ID');`

###### API details

**GET**

/api/logs/`id`

Requires `Authorization:TOKEN`

Path parameters

Param

Type

Description

id

String

ID of the log to view.

Query parameters

Param

Type

Description

fields

String

Comma separated string of the fields to return in the JSON response _(by default returns all fields)_. Ex.:

`?fields=*,expand.relField.name`

`*` targets all keys from the specific depth level.

In addition, the following field modifiers are also supported:

-   `:excerpt(maxLength, withEllipsis?)`
    Returns a short plain text version of the field string value.
    Ex.: `?fields=*,description:excerpt(200,true)`

Responses

200 401 403 404

`{ "id": "ai5z3aoed6809au", "created": "2024-10-27 09:28:19.524Z", "data": { "auth": "_superusers", "execTime": 2.392327, "method": "GET", "referer": "http://localhost:8090/_/", "remoteIP": "127.0.0.1", "status": 200, "type": "request", "url": "/api/collections/_pbc_2287844090/records?page=1&perPage=1&filter=&fields=id", "userAgent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36", "userIP": "127.0.0.1" }, "message": "GET /api/collections/_pbc_2287844090/records?page=1&perPage=1&filter=&fields=id", "level": 0 }`

`{ "status": 401, "message": "The request requires valid record authorization token.", "data": {} }`

`{ "status": 403, "message": "The authorized record is not allowed to perform this action.", "data": {} }`

`{ "status": 404, "message": "The requested resource wasn't found.", "data": {} }`

**[Logs statistics](#logs-statistics)**

Returns hourly aggregated logs statistics.

Only superusers can perform this action.

JavaScript

Dart

`import PocketBase from 'pocketbase'; const pb = new PocketBase('http://127.0.0.1:8090'); ... await pb.collection("_superusers").authWithPassword('test@example.com', '123456'); const stats = await pb.logs.getStats({ filter: 'data.status >= 400' });`

`import 'package:pocketbase/pocketbase.dart'; final pb = PocketBase('http://127.0.0.1:8090'); ... await pb.collection("_superusers").authWithPassword('test@example.com', '123456'); final stats = await pb.logs.getStats( filter: 'data.status >= 400' );`

###### API details

**GET**

/api/logs/stats

Requires `Authorization:TOKEN`

Query parameters

Param

Type

Description

filter

String

Filter expression to filter/search the logs, e.g.:

`?filter=(data.url~'test.com' && level>0)`

**Supported log filter fields:**
`rowid`, `id`, `created`, `updated`, `level`, `message` and any `data.*` attribute.

The syntax basically follows the format `OPERAND OPERATOR OPERAND`, where:

-   `OPERAND` - could be any field literal, string (single or double quoted), number, null, true, false
-   `OPERATOR` - is one of:
    -   `=` Equal
    -   `!=` NOT equal
    -   `>` Greater than
    -   `>=` Greater than or equal
    -   `<` Less than
    -   `<=` Less than or equal
    -   `~` Like/Contains (if not specified auto wraps the right string OPERAND in a "%" for wildcard match)
    -   `!~` NOT Like/Contains (if not specified auto wraps the right string OPERAND in a "%" for wildcard match)
    -   `?=` _Any/At least one of_ Equal
    -   `?!=` _Any/At least one of_ NOT equal
    -   `?>` _Any/At least one of_ Greater than
    -   `?>=` _Any/At least one of_ Greater than or equal
    -   `?<` _Any/At least one of_ Less than
    -   `?<=` _Any/At least one of_ Less than or equal
    -   `?~` _Any/At least one of_ Like/Contains (if not specified auto wraps the right string OPERAND in a "%" for wildcard match)
    -   `?!~` _Any/At least one of_ NOT Like/Contains (if not specified auto wraps the right string OPERAND in a "%" for wildcard match)

To group and combine several expressions you can use parenthesis `(...)`, `&&` (AND) and `||` (OR) tokens.

Single line comments are also supported: `// Example comment`.

Field expressions with array-like value or nested fields that originate from a source with multiple records will apply a **match-all** constraint by default. If you want **any/at-least-one-of** type of constraint for such fields you'll have to prefix your operator with `?` (e.g. `multiRelation.title ?= "test"`).

fields

String

Comma separated string of the fields to return in the JSON response _(by default returns all fields)_. Ex.:

`?fields=*,expand.relField.name`

`*` targets all keys from the specific depth level.

In addition, the following field modifiers are also supported:

-   `:excerpt(maxLength, withEllipsis?)`
    Returns a short plain text version of the field string value.
    Ex.: `?fields=*,description:excerpt(200,true)`

Responses

200 400 401 403

`[ { "total": 4, "date": "2022-06-01 19:00:00.000" }, { "total": 1, "date": "2022-06-02 12:00:00.000" }, { "total": 8, "date": "2022-06-02 13:00:00.000" } ]`

`{ "status": 400, "message": "Something went wrong while processing your request. Invalid filter.", "data": {} }`

`{ "status": 401, "message": "The request requires valid record authorization token.", "data": {} }`

`{ "status": 403, "message": "The authorized record is not allowed to perform this action.", "data": {} }`