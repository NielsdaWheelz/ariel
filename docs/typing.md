# Typing

## Scope

This document covers how Python values should be typed inside the codebase: when to use `Any`, `object`, `dict`, `TypedDict`, `tuple`, `NamedTuple`, dataclasses, pydantic models, and domain classes.

## Goal

Types should make illegal states hard to express and make downstream code boring. A function signature should tell the reader what shape the value has, what operations are valid, and what assumptions have already been checked.

Internal code should not carry vague values like `Any`, `dict[str, Any]`, or positional tuples whose meaning lives only in the author’s head. If a value has a known shape, encode that shape.

## `Any`

`typing.Any` disables type checking. Treat it like an unsafe escape hatch.

- Do not use `Any` in core domain logic.
- Do not use `Any` to avoid modeling a known shape.
- Do not use `Any` to silence the type checker.
- Do not use `dict[str, Any]` for data whose fields are known.
- Quarantine `Any` at external or dynamic boundaries.
- Narrow `Any` immediately into a specific typed form.
- Do not let `Any` leak across internal module boundaries.

Acceptable uses:

- raw data from JSON, YAML, environment variables, RPC requests, webhooks, or third-party APIs before validation
- untyped third-party libraries
- migration scaffolding in legacy code
- rare decorator, metaprogramming, plugin, registry, or passthrough code
- genuinely heterogeneous storage where the value is not inspected without narrowing

Prefer `object` when the value is unknown but should not permit arbitrary operations.

```python
value: object
````

means “unknown; narrow before use.”

```python
value: Any
```

means “the type checker gives up here.”

## External Data

Raw external data may temporarily be `Any` or broadly shaped.

```python
raw: Any = json.loads(body)
```

But raw data must be parsed into a narrow type at the boundary.

```python
payload = UserPayload.model_validate(raw)
```

After parsing, downstream code receives the narrow type, not the raw value.

Do not pass unvalidated `Any`, `dict[str, Any]`, or raw JSON-like values deeper into the system.

## Dictionaries

Dictionaries are acceptable when the value is actually a map: arbitrary keys of one type to arbitrary values of one type.

Good:

```python
user_id_to_name: dict[int, str]
feature_flags: dict[str, bool]
counts_by_word: dict[str, int]
headers: dict[str, str]
```

A dictionary is suspicious when it is secretly a record.

Bad:

```python
user: dict[str, str | int | None]
```

Worse:

```python
user: dict[str, Any]
```

If the keys are known, encode the schema.

For JSON-like boundary data, use `TypedDict` or a validation model.

```python
class UserPayload(TypedDict):
    email: str
    name: str | None
```

For internal domain data, use a dataclass, pydantic model, or explicit domain class.

```python
@dataclass(frozen=True)
class User:
    id: UserId
    email: EmailAddress
    created_at: datetime
```

Rule:

* `dict[K, V]` is for maps.
* `TypedDict` is for structured JSON-like records.
* dataclasses, pydantic models, and classes are for internal domain objects.
* `dict[str, Any]` is almost always boundary-only.

## Tuples

Tuples are acceptable when the positional meaning is obvious, small, and conventional.

Good:

```python
point: tuple[float, float]
rgb: tuple[int, int, int]
items: tuple[str, ...]
```

Tuples are bad when the positions carry domain meaning that is not obvious.

Bad:

```python
user: tuple[int, str, str, bool]
```

If a tuple needs explanation, it needs names.

Use `NamedTuple`, dataclass, or a domain object.

```python
class UserRow(NamedTuple):
    id: int
    email: str
    name: str
    is_active: bool
```

Prefer dataclasses for internal domain objects unless tuple behavior is specifically useful.

## Records

A record is a value with known fields.

Do not represent records as loose dictionaries or long positional tuples.

Use:

* `TypedDict` for external JSON-like payloads
* dataclasses for simple internal values
* pydantic models when runtime validation or serialization is needed
* explicit classes when behavior, invariants, or lifecycle matter
* `NamedTuple` only when immutability and tuple-like behavior are intentional

The shape should be visible in the type, not reconstructed from scattered string keys or tuple indexes.

## Type Narrowing

Broad types must be narrowed near where they enter.

Good:

```python
def handle_request(raw: Any) -> User:
    payload = UserPayload.model_validate(raw)
    return User.from_payload(payload)
```

Bad:

```python
def handle_request(raw: dict[str, Any]) -> None:
    process_user(raw)
```

Do not make every downstream function rediscover the same facts.

If a value has already been parsed, its type should carry the guarantee.

## Internal APIs

Internal function signatures should be narrow.

Bad:

```python
def process_user(user: dict[str, Any]) -> Any:
    ...
```

Better:

```python
def process_user(user: User) -> ProcessedUser:
    ...
```

Bad:

```python
def update_status(user_id: str, status: str) -> None:
    ...
```

Better:

```python
def update_status(user_id: UserId, status: UserStatus) -> None:
    ...
```

Do not widen meaningful values back to primitives unless crossing an external boundary.

## Return Types

Return types should be as specific as practical.

Avoid:

```python
def load_config() -> dict[str, Any]:
    ...
```

Prefer:

```python
def load_config() -> AppConfig:
    ...
```

Avoid returning tuples when the caller has to remember what each position means.

Bad:

```python
def get_user() -> tuple[int, str, bool]:
    ...
```

Better:

```python
def get_user() -> User:
    ...
```

## Exceptions

A broad type is allowed only when the broadness is real.

Acceptable:

```python
metadata: dict[str, JsonValue]
```

Acceptable:

```python
cache: dict[str, object]
```

Acceptable:

```python
plugin_state: dict[str, Any]
```

But each use needs a reason. “it was faster to write” is not a reason.

## Enforcement

* New domain code should not introduce `Any` except at explicit boundaries.
* New domain code should not introduce `dict[str, Any]` without a boundary/migration justification.
* Known records should be modeled with `TypedDict`, dataclasses, pydantic models, or classes.
* Positional tuples with unclear meaning should be replaced with named structures.
* Broad types should be narrowed once, near the boundary, then kept narrow.
* Type ignores and casts should be rare, local, and explained.

The smell is not `dict`, `tuple`, or `Any` by themselves. The smell is implicit schema.

```