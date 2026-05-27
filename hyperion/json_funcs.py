"""SQLite-compatible JSON scalar functions and json_each table-valued expansion."""
import copy
import json
import re
from typing import Any


# ── Internal helpers ──────────────────────────────────────────────────────────

def _json_parse(val: Any) -> Any:
    if val is None:
        return None
    if isinstance(val, (dict, list)):
        return val
    try:
        return json.loads(str(val))
    except (json.JSONDecodeError, TypeError):
        return None


def _json_dump(obj: Any) -> str | None:
    if obj is None:
        return None
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def _json_type_name(val: Any) -> str:
    if val is None:       return "null"
    if isinstance(val, bool):  return "true" if val else "false"
    if isinstance(val, int):   return "integer"
    if isinstance(val, float): return "real"
    if isinstance(val, list):  return "array"
    if isinstance(val, dict):  return "object"
    return "text"


# ── JSONPath navigator ────────────────────────────────────────────────────────

def _path_get(obj: Any, path: str) -> Any:
    """Navigate obj using SQLite JSONPath ($.key, $[0], $.a.b[1])."""
    if path == "$":
        return obj
    rest = path[1:] if path.startswith("$") else path
    cur = obj
    while rest:
        if rest.startswith("."):
            rest = rest[1:]
            m = re.match(r'^"([^"]+)"(.*)', rest) or re.match(r'^([^\.\[]+)(.*)', rest)
            if not m:
                return None
            key, rest = m.group(1), m.group(2)
            if not isinstance(cur, dict):
                return None
            cur = cur.get(key)
        elif rest.startswith("["):
            m = re.match(r'^\[(\d+)\](.*)', rest)
            if not m:
                return None
            idx_str, rest = m.group(1), m.group(2)
            if not isinstance(cur, list):
                return None
            try:
                cur = cur[int(idx_str)]
            except IndexError:
                return None
        else:
            break
    return cur


def _path_parent(path: str) -> str:
    last_dot     = path.rfind(".", 1)
    last_bracket = path.rfind("[", 1)
    cut = max(last_dot, last_bracket)
    return path[:cut] if cut > 0 else "$"


def _path_last_key(path: str) -> Any:
    m = re.search(r'\.([^\.\[]+)$', path)
    if m:
        return m.group(1)
    m = re.search(r'\[(\d+)\]$', path)
    if m:
        return int(m.group(1))
    return None


def _path_set(obj: Any, path: str, value: Any, mode: str) -> Any:
    """mode: 'set' (insert+replace), 'insert' (no overwrite), 'replace' (no insert)."""
    if path == "$":
        return obj if mode == "insert" else value
    parent_path = _path_parent(path)
    key         = _path_last_key(path)
    if key is None:
        return obj
    new_obj    = copy.deepcopy(obj)
    new_parent = _path_get(new_obj, parent_path)
    if isinstance(new_parent, dict) and isinstance(key, str):
        exists = key in new_parent
        if mode == "insert" and exists:     return obj
        if mode == "replace" and not exists: return obj
        new_parent[key] = value
    elif isinstance(new_parent, list) and isinstance(key, int):
        exists = 0 <= key < len(new_parent)
        if mode == "insert" and exists:     return obj
        if mode == "replace" and not exists: return obj
        if exists:
            new_parent[key] = value
        elif key == len(new_parent):
            new_parent.append(value)
    return new_obj


def _path_remove(obj: Any, path: str) -> Any:
    parent_path = _path_parent(path)
    key         = _path_last_key(path)
    if key is None:
        return obj
    new_obj    = copy.deepcopy(obj)
    new_parent = _path_get(new_obj, parent_path)
    if isinstance(new_parent, dict) and isinstance(key, str):
        new_parent.pop(key, None)
    elif isinstance(new_parent, list) and isinstance(key, int):
        if 0 <= key < len(new_parent):
            new_parent.pop(key)
    return new_obj


def _json_merge_patch(target: Any, patch: Any) -> Any:
    if not isinstance(patch, dict):
        return patch
    result = dict(target) if isinstance(target, dict) else {}
    for k, v in patch.items():
        if v is None:
            result.pop(k, None)
        else:
            result[k] = _json_merge_patch(result.get(k), v)
    return result


# ── Scalar function dispatcher ────────────────────────────────────────────────

def eval_json_func(fname: str, args: list[Any]) -> Any:
    upper = fname.upper()

    if upper == "JSON":
        obj = _json_parse(args[0] if args else None)
        return _json_dump(obj)

    if upper == "JSON_VALID":
        return 1 if _json_parse(args[0] if args else None) is not None else 0

    if upper == "JSON_QUOTE":
        if not args or args[0] is None:
            return "null"
        v = args[0]
        if isinstance(v, bool):   return "true" if v else "false"
        if isinstance(v, int):    return str(v)
        if isinstance(v, float):  return str(v)
        return json.dumps(str(v), ensure_ascii=False)

    if upper == "JSON_TYPE":
        obj = _json_parse(args[0] if args else None)
        if len(args) >= 2 and args[1] is not None:
            obj = _path_get(obj, str(args[1]))
        return _json_type_name(obj)

    if upper == "JSON_EXTRACT":
        if len(args) < 2 or args[0] is None:
            return None
        obj = _json_parse(args[0])
        if obj is None:
            return None
        if len(args) == 2:
            val = _path_get(obj, str(args[1]))
            return _json_dump(val) if isinstance(val, (dict, list)) else val
        results = [_path_get(obj, str(p)) for p in args[1:]]
        return _json_dump(results)

    if upper == "JSON_ARRAY":
        return _json_dump(list(args))

    if upper == "JSON_OBJECT":
        if len(args) % 2 != 0:
            return None
        obj: dict[str, Any] = {}
        for i in range(0, len(args), 2):
            if args[i] is None:
                return None
            obj[str(args[i])] = args[i + 1]
        return _json_dump(obj)

    if upper in ("JSON_SET", "JSON_INSERT", "JSON_REPLACE"):
        if len(args) < 3:
            return None
        obj2 = _json_parse(args[0])
        if obj2 is None:
            return None
        mode = {"JSON_SET": "set", "JSON_INSERT": "insert", "JSON_REPLACE": "replace"}[upper]
        idx = 1
        while idx + 1 < len(args):
            obj2 = _path_set(obj2, str(args[idx]), args[idx + 1], mode)
            idx += 2
        return _json_dump(obj2)

    if upper == "JSON_REMOVE":
        if len(args) < 2:
            return None
        obj3 = _json_parse(args[0])
        if obj3 is None:
            return None
        for path in args[1:]:
            obj3 = _path_remove(obj3, str(path))
        return _json_dump(obj3)

    if upper == "JSON_PATCH":
        if len(args) < 2:
            return None
        obj4  = _json_parse(args[0])
        patch = _json_parse(args[1])
        if patch is None:
            return _json_dump(obj4)
        return _json_dump(_json_merge_patch(obj4, patch))

    if upper == "JSON_ARRAY_LENGTH":
        obj5 = _json_parse(args[0] if args else None)
        if len(args) >= 2 and args[1] is not None:
            obj5 = _path_get(obj5, str(args[1]))
        return len(obj5) if isinstance(obj5, list) else None

    return None


# ── Table-valued expansion (json_each) ────────────────────────────────────────

def json_each_rows(json_val: Any, path: str = "$") -> list[dict]:
    """Expand a JSON value to rows for json_each(val[, path])."""
    obj = _json_parse(json_val)
    if obj is None:
        return []
    if path != "$":
        obj = _path_get(obj, path)
    rows: list[dict] = []
    if isinstance(obj, list):
        for idx, val in enumerate(obj):
            atom = val if not isinstance(val, (dict, list)) else None
            rows.append({
                "key":     idx,
                "value":   _json_dump(val) if isinstance(val, (dict, list)) else val,
                "type":    _json_type_name(val),
                "atom":    atom,
                "id":      idx,
                "parent":  None,
                "fullkey": f"$[{idx}]",
                "path":    "$",
            })
    elif isinstance(obj, dict):
        for idx, (key, val) in enumerate(obj.items()):
            atom = val if not isinstance(val, (dict, list)) else None
            rows.append({
                "key":     key,
                "value":   _json_dump(val) if isinstance(val, (dict, list)) else val,
                "type":    _json_type_name(val),
                "atom":    atom,
                "id":      idx,
                "parent":  None,
                "fullkey": f"$.{key}",
                "path":    "$",
            })
    return rows
