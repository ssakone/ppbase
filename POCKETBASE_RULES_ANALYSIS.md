# PocketBase API Rules Implementation Analysis

This document provides a detailed analysis of how PocketBase (Go) implements API rules for collections, based on analysis of the actual source code from GitHub.

## 1. Rule Definition Structure

### Collection Model (`core/collection_model.go`)

Rules are defined as **nullable string pointers** (`*string`) in the `baseCollection` struct:

```go
type baseCollection struct {
    BaseModel
    
    ListRule   *string `db:"listRule" json:"listRule" form:"listRule"`
    ViewRule   *string `db:"viewRule" json:"viewRule" form:"viewRule"`
    CreateRule *string `db:"createRule" json:"createRule" form:"createRule"`
    UpdateRule *string `db:"updateRule" json:"updateRule" form:"updateRule"`
    DeleteRule *string `db:"deleteRule" json:"deleteRule" form:"deleteRule"`
    
    // ... other fields
}
```

### Rule States

Each rule can be in one of three states:

1. **`nil` (NULL)** - Only superusers can access (locked/admin-only)
2. **`""` (empty string)** - Public access (no restrictions)
3. **`"expression"`** - Conditional access (filter expression evaluated)

## 2. Rule Evaluation Order

The evaluation follows this exact order in each CRUD operation:

### Step 1: Superuser Check (Bypass)
```go
hasSuperuserAuth := requestInfo.HasSuperuserAuth()
```

**If `hasSuperuserAuth == true`:**
- **All rules are bypassed**
- Superusers can perform the operation regardless of rule values
- This check happens **before** any rule evaluation

### Step 2: NULL Rule Check
```go
if collection.ListRule == nil && !requestInfo.HasSuperuserAuth() {
    return e.ForbiddenError("Only superusers can perform this action.", nil)
}
```

**If rule is `nil` and not superuser:**
- Return `403 Forbidden` immediately
- No further evaluation

### Step 3: Empty String Check
```go
if !requestInfo.HasSuperuserAuth() && collection.ListRule != nil && *collection.ListRule != "" {
    // Evaluate expression
}
```

**If rule is `""` (empty string):**
- **Allow access** - no filter applied
- Operation proceeds normally

### Step 4: Expression Evaluation
**If rule is non-empty string:**
- Parse and build SQL expression from the filter
- Apply as WHERE clause to the query
- If expression evaluates to false/no matches → deny access

## 3. Rule Enforcement by Operation

### LIST (`GET /api/collections/{collection}/records`)

**File:** `apis/record_crud.go:recordsList()`

```go
// 1. Check NULL rule
if collection.ListRule == nil && !requestInfo.HasSuperuserAuth() {
    return e.ForbiddenError("Only superusers can perform this action.", nil)
}

// 2. Build query
query := e.App.RecordQuery(collection)

// 3. Apply expression if non-empty
if !requestInfo.HasSuperuserAuth() && collection.ListRule != nil && *collection.ListRule != "" {
    fieldsResolver := core.NewRecordFieldResolver(e.App, collection, requestInfo, true)
    expr, err := search.FilterData(*collection.ListRule).BuildExpr(fieldsResolver)
    if err != nil {
        return err
    }
    query.AndWhere(expr)  // Applied as WHERE clause
    // Note: fieldsResolver.UpdateQuery(query) is commented as "will be applied by search provider"
}

// 4. Execute query with search provider
searchProvider := search.NewProvider(fieldsResolver).Query(query)
records := []*core.Record{}
result, err := searchProvider.ParseAndExec(e.Request.URL.Query().Encode(), &records)
```

**Key Points:**
- ListRule is applied **as a WHERE clause** (pre-filter, not post-filter)
- Combined with client-side `?filter=` query params
- If rule matches no records → returns empty array (200 OK, not 403)
- Special timing attack protection for empty results

### VIEW (`GET /api/collections/{collection}/records/{id}`)

**File:** `apis/record_crud.go:recordView()`

```go
// 1. Check NULL rule
if collection.ViewRule == nil && !requestInfo.HasSuperuserAuth() {
    return e.ForbiddenError("Only superusers can perform this action.", nil)
}

// 2. Build rule function
ruleFunc := func(q *dbx.SelectQuery) error {
    if !requestInfo.HasSuperuserAuth() && collection.ViewRule != nil && *collection.ViewRule != "" {
        resolver := core.NewRecordFieldResolver(e.App, collection, requestInfo, true)
        expr, err := search.FilterData(*collection.ViewRule).BuildExpr(resolver)
        if err != nil {
            return err
        }
        q.AndWhere(expr)  // Applied as WHERE clause
        err = resolver.UpdateQuery(q)  // Adds JOINs if needed
        if err != nil {
            return err
        }
    }
    return nil
}

// 3. Fetch record with rule applied
record, fetchErr := e.App.FindRecordById(collection, recordId, ruleFunc)
if fetchErr != nil || record == nil {
    return e.NotFoundError("", fetchErr)  // Returns 404, not 403
}
```

**Key Points:**
- ViewRule is applied **as a WHERE clause** during record fetch
- If rule doesn't match → returns `404 Not Found` (not `403 Forbidden`)
- This prevents information disclosure about record existence

### CREATE (`POST /api/collections/{collection}/records`)

**File:** `apis/record_crud.go:recordCreate()`

```go
// 1. Check NULL rule
hasSuperuserAuth := requestInfo.HasSuperuserAuth()
if !hasSuperuserAuth && collection.CreateRule == nil {
    return e.ForbiddenError("Only superusers can perform this action.", nil)
}

// 2. Create dummy record with submitted data
dummyRecord := record.Clone()
dummyRandomPart := "__pb_create__" + security.PseudorandomString(6)
if dummyRecord.Id == "" {
    dummyRecord.Id = "__temp_id__" + dummyRandomPart
}
dummyRecord.SetVerified(false)  // Always unset verified

// 3. Export dummy record to DB params
dummyExport, err := dummyRecord.DBExport(e.App)
dummyParams := make(dbx.Params, len(dummyExport))
// ... build WITH clause and SELECT

// 4. Evaluate CreateRule expression against dummy record
if *dummyCollection.CreateRule != "" {
    ruleQuery := e.App.ConcurrentDB().Select("(1)").
        PreFragment(withFrom).
        From(dummyCollection.Name).
        AndBind(dummyParams)
    
    resolver := core.NewRecordFieldResolver(e.App, &dummyCollection, requestInfo, true)
    expr, err := search.FilterData(*dummyCollection.CreateRule).BuildExpr(resolver)
    if err != nil {
        return e.BadRequestError("Failed to create record", err)
    }
    ruleQuery.AndWhere(expr)
    err = resolver.UpdateQuery(ruleQuery)
    if err != nil {
        return e.BadRequestError("Failed to create record", err)
    }
    
    var exists int
    err = ruleQuery.Limit(1).Row(&exists)
    if err != nil || exists == 0 {
        return e.BadRequestError("Failed to create record", fmt.Errorf("create rule failure: %w", err))
    }
}

// 5. Check ManageRule for auth collections (if applicable)
// ... (for password changes, etc.)
```

**Key Points:**
- CreateRule is evaluated **before** the record is created
- Uses a **dummy/CTE record** with the submitted data
- If rule fails → returns `400 Bad Request` (not `403`)
- The `verified` field is **always unset** in the dummy record to prevent manage rule misuse
- Uses a WITH clause (CTE) to simulate the record in a query

### UPDATE (`PATCH /api/collections/{collection}/records/{id}`)

**File:** `apis/record_crud.go:recordUpdate()`

```go
// 1. Check NULL rule
hasSuperuserAuth := requestInfo.HasSuperuserAuth()
if !hasSuperuserAuth && collection.UpdateRule == nil {
    return e.ForbiddenError("Only superusers can perform this action.", nil)
}

// 2. Fetch existing record first (for modifiers)
record, err := e.App.FindRecordById(collection, recordId)
// ... load data from request

// 3. Build rule function
ruleFunc := func(q *dbx.SelectQuery) error {
    if !hasSuperuserAuth && collection.UpdateRule != nil && *collection.UpdateRule != "" {
        resolver := core.NewRecordFieldResolver(e.App, collection, requestInfo, true)
        expr, err := search.FilterData(*collection.UpdateRule).BuildExpr(resolver)
        if err != nil {
            return err
        }
        q.AndWhere(expr)  // Applied as WHERE clause
        err = resolver.UpdateQuery(q)
        if err != nil {
            return err
        }
    }
    return nil
}

// 4. Refetch with rule applied
record, err = e.App.FindRecordById(collection, recordId, ruleFunc)
if err != nil {
    return e.NotFoundError("", err)  // Returns 404 if rule doesn't match
}

// 5. Proceed with update
```

**Key Points:**
- UpdateRule is applied **as a WHERE clause** when fetching the record
- If rule doesn't match → returns `404 Not Found` (not `403`)
- Record is fetched twice: once for modifiers, once with rule check

### DELETE (`DELETE /api/collections/{collection}/records/{id}`)

**File:** `apis/record_crud.go:recordDelete()`

```go
// 1. Check NULL rule
if !requestInfo.HasSuperuserAuth() && collection.DeleteRule == nil {
    return e.ForbiddenError("Only superusers can perform this action.", nil)
}

// 2. Build rule function
ruleFunc := func(q *dbx.SelectQuery) error {
    if !requestInfo.HasSuperuserAuth() && collection.DeleteRule != nil && *collection.DeleteRule != "" {
        resolver := core.NewRecordFieldResolver(e.App, collection, requestInfo, true)
        expr, err := search.FilterData(*collection.DeleteRule).BuildExpr(resolver)
        if err != nil {
            return err
        }
        q.AndWhere(expr)  // Applied as WHERE clause
        err = resolver.UpdateQuery(q)
        if err != nil {
            return err
        }
    }
    return nil
}

// 3. Fetch with rule applied
record, err := e.App.FindRecordById(collection, recordId, ruleFunc)
if err != nil || record == nil {
    return e.NotFoundError("", err)  // Returns 404 if rule doesn't match
}

// 4. Proceed with delete
```

**Key Points:**
- DeleteRule is applied **as a WHERE clause** when fetching the record
- If rule doesn't match → returns `404 Not Found` (not `403`)

## 4. RequestInfo and Auth Context

### RequestInfo Structure

**File:** `core/record_field_resolver.go:NewRecordFieldResolver()`

The `RequestInfo` contains:
- `Auth` - The authenticated record (can be superuser or auth collection record)
- `Context` - Request context (`RequestInfoContextOAuth2`, `RequestInfoContextExpand`, etc.)
- `Method` - HTTP method (`GET`, `POST`, etc.)
- `Query` - Query parameters map
- `Headers` - HTTP headers map
- `Body` - Request body data

### HasSuperuserAuth() Check

```go
func (ri *RequestInfo) HasSuperuserAuth() bool {
    return ri.Auth != nil && ri.Auth.IsSuperuser()
}

func (r *Record) IsSuperuser() bool {
    return r.Collection().Name == CollectionNameSuperusers  // "_superusers"
}
```

**Superuser Detection:**
- Checks if `requestInfo.Auth` is not nil
- Checks if the auth record's collection name is `"_superusers"`
- If both true → superuser bypasses ALL rules

## 5. @request.auth.* Variable Resolution

**File:** `core/record_field_resolver.go:NewRecordFieldResolver()`

### Static Request Info Setup

```go
r.staticRequestInfo = map[string]any{}
if r.requestInfo != nil {
    r.staticRequestInfo["context"] = r.requestInfo.Context
    r.staticRequestInfo["method"] = r.requestInfo.Method
    r.staticRequestInfo["query"] = r.requestInfo.Query
    r.staticRequestInfo["headers"] = r.requestInfo.Headers
    r.staticRequestInfo["body"] = r.requestInfo.Body
    r.staticRequestInfo["auth"] = nil
    if r.requestInfo.Auth != nil {
        authClone := r.requestInfo.Auth.Clone()
        r.staticRequestInfo["auth"] = authClone.
            Unhide(authClone.Collection().Fields.FieldNames()...).  // Unhide all fields
            IgnoreEmailVisibility(true).                            // Allow email access
            PublicExport()                                           // Export as JSON
    }
}
```

### Field Resolution

When a rule references `@request.auth.id`:

1. **Parse field path:** `["request", "auth", "id"]`
2. **Extract from staticRequestInfo:**
   - `staticRequestInfo["auth"]` → auth record JSON
   - Navigate to `["id"]` property
3. **Return as SQL placeholder:**
   ```go
   return &search.ResolverResult{
       Identifier: "{:" + placeholder + "}",
       Params: dbx.Params{placeholder: authRecordId},
   }
   ```

### Supported @request.* Patterns

From `record_field_resolver.go` allowed fields:
- `@request.context` - Request context string
- `@request.method` - HTTP method
- `@request.auth.*` - Auth record fields (id, email, etc.)
- `@request.body.*` - Request body fields
- `@request.query.*` - Query parameters
- `@request.headers.*` - HTTP headers
- `@collection.{name}.*` - Related collection fields (via JOINs)

## 6. Filter Expression Application

### Expression Building

**File:** `tools/search/filter.go` (referenced via `search.FilterData()`)

1. **Parse filter string** using Lark grammar parser
2. **Build SQL expression** via `BuildExpr(resolver)`
3. **Apply as WHERE clause** via `query.AndWhere(expr)`

### WHERE Clause Application

Rules are **always applied as WHERE clauses**, not post-filters:

```go
// Example from recordView()
ruleFunc := func(q *dbx.SelectQuery) error {
    if rule != "" {
        expr, err := search.FilterData(rule).BuildExpr(resolver)
        q.AndWhere(expr)  // Added to WHERE clause
        resolver.UpdateQuery(q)  // Adds JOINs if needed for @collection.* references
    }
    return nil
}
```

**This means:**
- Rules filter at the **database level**, not application level
- More efficient (database does the filtering)
- Can use indexes for performance
- Combined with client `?filter=` params via AND

### JOIN Handling

When rules reference `@collection.{name}.*` fields:

```go
// From record_field_resolver.go:registerJoin()
func (r *RecordFieldResolver) registerJoin(tableName string, tableAlias string, on dbx.Expression) error {
    // ... validation ...
    r.joins = append(r.joins, &search.Join{
        TableName: tableName,
        TableAlias: tableAlias,
        On: on,
    })
    return nil
}

// Later in UpdateQuery()
func (r *RecordFieldResolver) UpdateQuery(query *dbx.SelectQuery) error {
    for _, join := range r.joins {
        query.LeftJoin(
            (join.TableName + " " + join.TableAlias),
            join.On,
        )
    }
    return nil
}
```

**JOINs are added automatically** when rules reference related collections.

## 7. Special Cases and Edge Cases

### Empty String vs NULL

- **NULL (`nil`)**: Admin-only, returns 403 if not superuser
- **Empty string (`""`)**: Public access, no filter applied
- **Expression**: Conditional access, filter applied

### Auth Collections - ManageRule

**File:** `apis/record_crud.go:hasAuthManageAccess()`

Auth collections have an additional `ManageRule` (not in baseCollection):

```go
func hasAuthManageAccess(app core.App, requestInfo *core.RequestInfo, collection *core.Collection, query *dbx.SelectQuery) bool {
    if !collection.IsAuth() {
        return false
    }
    
    manageRule := collection.ManageRule
    if manageRule == nil || *manageRule == "" {
        return false  // Only for superusers
    }
    
    if requestInfo == nil || requestInfo.Auth == nil {
        return false  // No auth record
    }
    
    // Evaluate ManageRule expression
    resolver := core.NewRecordFieldResolver(app, collection, requestInfo, true)
    expr, err := search.FilterData(*manageRule).BuildExpr(resolver)
    if err != nil {
        return false
    }
    query.AndWhere(expr)
    err = resolver.UpdateQuery(query)
    if err != nil {
        return false
    }
    
    var exists int
    err = query.Limit(1).Row(&exists)
    return err == nil && exists > 0
}
```

**ManageRule allows:**
- Changing passwords without `oldPassword`
- Modifying system auth fields
- Only evaluated if `CreateRule` or `UpdateRule` passes first

### List Rule Timing Attack Protection

**File:** `apis/record_crud.go:recordsList()`

```go
// Randomized throttle for empty search results
if !e.HasSuperuserAuth() &&
    (collection.ListRule != nil && *collection.ListRule != "") &&
    (requestInfo.Query["filter"] != "") &&
    len(e.Records) == 0 &&
    checkRateLimit(...) != nil {
    randomizedThrottle(500)  // Sleep 0-500ms randomly
}
```

**Purpose:** Prevents timing attacks that could reveal rule expressions by measuring response times.

### Hidden Fields in Rules

**File:** `core/record_field_resolver.go`

```go
// Hidden fields are searchable only by superusers
fieldsResolver.SetAllowHiddenFields(requestInfo.HasSuperuserAuth())
```

**Behavior:**
- Non-superusers **cannot** filter by hidden fields in rules
- Superusers can use hidden fields
- This prevents information disclosure via rule expressions

### View Collections

**File:** `apis/record_crud.go`

View collections (read-only SQL views):
- Can have `ListRule` and `ViewRule`
- **Cannot** have `CreateRule`, `UpdateRule`, `DeleteRule` (returns 400 if attempted)

## 8. Error Responses

| Rule State | Operation | Non-Superuser Result |
|------------|-----------|---------------------|
| `nil` | Any | `403 Forbidden` - "Only superusers can perform this action." |
| `""` | Any | `200 OK` - Operation succeeds (public access) |
| Expression (no match) | LIST | `200 OK` - Empty array `[]` |
| Expression (no match) | VIEW | `404 Not Found` |
| Expression (no match) | CREATE | `400 Bad Request` - "Failed to create record" |
| Expression (no match) | UPDATE | `404 Not Found` |
| Expression (no match) | DELETE | `404 Not Found` |

**Key Design Decisions:**
- LIST returns empty array (not 403) to prevent information disclosure
- VIEW/UPDATE/DELETE return 404 (not 403) to hide record existence
- CREATE returns 400 (not 403) as it's a validation error

## 9. Summary

### Rule Evaluation Flow

```
1. Check: Is superuser?
   ├─ YES → Bypass all rules, allow operation
   └─ NO → Continue

2. Check: Is rule NULL?
   ├─ YES → Return 403 Forbidden
   └─ NO → Continue

3. Check: Is rule empty string?
   ├─ YES → Allow operation (public access)
   └─ NO → Continue

4. Evaluate expression:
   ├─ Parse filter string
   ├─ Build SQL expression
   ├─ Apply as WHERE clause
   ├─ Execute query
   └─ Check result:
       ├─ MATCH → Allow operation
       └─ NO MATCH → Return error (varies by operation)
```

### Key Implementation Details

1. **Rules are WHERE clauses**, not post-filters
2. **Superusers bypass ALL rules** (checked first)
3. **NULL = admin-only**, `""` = public, `"expr"` = conditional
4. **@request.auth.*** resolves to auth record fields (unhidden, email visible)
5. **JOINs added automatically** for `@collection.*` references
6. **Hidden fields** only accessible to superusers in rules
7. **Error codes vary** by operation (404 vs 403 vs 400) for security

### Differences from PPBase Current Implementation

Based on the codebase analysis, PPBase should ensure:

1. ✅ Rules stored as nullable strings (`NULL`, `""`, or expression)
2. ✅ Superuser check happens **before** rule evaluation
3. ✅ Rules applied as **WHERE clauses** (not post-filters)
4. ✅ `@request.auth.*` variables resolve from auth record
5. ✅ JOINs added automatically for `@collection.*` references
6. ✅ Error codes match PocketBase (404 for VIEW/UPDATE/DELETE, 200 empty for LIST, 400 for CREATE)
