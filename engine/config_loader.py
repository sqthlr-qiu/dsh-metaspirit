# config_loader.py —— 配置加载
# 零重依赖：优先尝试 import yaml；不存在则优先读取同名 .json；再不行用极简 YAML 解析。
# 极简解析仅支持本工程 config 使用的子集：缩进映射、块列表、标量（int/float/bool/null/字符串）。

import json
import os


def _split_flow(s: str) -> list:
    """顶层逗号分割 flow-style 内容（忽略引号内逗号与嵌套括号）。"""
    parts, depth, quote, cur = [], 0, "", ""
    for ch in s:
        if quote:
            cur += ch
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
            cur += ch
        elif ch in "{[":
            depth += 1
            cur += ch
        elif ch in "}]":
            depth -= 1
            cur += ch
        elif ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        parts.append(cur)
    return parts


def _parse_scalar(s: str):
    """把字符串标量解析为 Python 值（支持 flow-style {..} / [..]）。"""
    s = s.strip()
    if s == "" or s.lower() == "null" or s.lower() == "~":
        return None
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        return s[1:-1]
    if s.startswith("{") and s.endswith("}"):
        d = {}
        for part in _split_flow(s[1:-1].strip()):
            if ":" in part:
                k, _, v = part.partition(":")
                d[k.strip()] = _parse_scalar(v.strip())
        return d
    if s.startswith("[") and s.endswith("]"):
        return [_parse_scalar(x) for x in _split_flow(s[1:-1].strip())]
    if s.lower() == "true":
        return True
    if s.lower() == "false":
        return False
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return s


def _minimal_yaml_parse(text: str):
    """极简 YAML 解析：支持 2 空格缩进映射、块列表、标量。返回 dict/list。"""
    # 预处理：去注释、保留结构
    lines = []
    for raw in text.splitlines():
        # 去掉 # 注释（不在引号内，本工程配置不含引号内 #）
        if "#" in raw:
            raw = raw.split("#", 1)[0]
        if raw.strip() == "":
            continue
        lines.append(raw.rstrip())

    # 计算缩进
    def indent_of(line: str) -> int:
        return len(line) - len(line.lstrip(" "))

    # 构造 token 列表 (indent, content)
    tokens = [(indent_of(l), l.strip()) for l in lines]

    pos = 0

    def parse_block(min_indent: int):
        nonlocal pos
        # 决定是映射还是列表
        if pos >= len(tokens):
            return None
        indent, content = tokens[pos]
        if content.startswith("- "):
            return parse_list(indent)
        return parse_map(indent)

    def parse_map(indent: int):
        nonlocal pos
        result = {}
        while pos < len(tokens):
            cur_indent, content = tokens[pos]
            if cur_indent < indent:
                break
            if cur_indent > indent:
                break
            if content.startswith("- "):
                break
            # key: value 或 key:
            if ":" not in content:
                pos += 1
                continue
            key, _, rest = content.partition(":")
            key = key.strip()
            rest = rest.strip()
            pos += 1
            if rest == "":
                # 子块
                if pos < len(tokens) and tokens[pos][0] > indent:
                    result[key] = parse_block(tokens[pos][0])
                else:
                    result[key] = None
            else:
                result[key] = _parse_scalar(rest)
        return result

    def parse_list(indent: int):
        nonlocal pos
        result = []
        while pos < len(tokens):
            cur_indent, content = tokens[pos]
            if cur_indent != indent or not content.startswith("- "):
                break
            item = content[2:].strip()
            pos += 1
            if item == "":
                # 嵌套块项
                if pos < len(tokens) and tokens[pos][0] > indent:
                    result.append(parse_block(tokens[pos][0]))
                else:
                    result.append(None)
            elif ":" in item and not (item.startswith('"') or item.startswith("'")):
                # 列表项是映射：回退一格，用映射解析（把 item 当作第一行）
                # 构造临时 token 插入：将 item 以 indent+2 重新解析
                # 简单处理：直接解析 "key: value" 单行的映射
                m = {}
                k, _, v = item.partition(":")
                m[k.strip()] = _parse_scalar(v.strip())
                # 后续同缩进的子映射行
                while pos < len(tokens) and tokens[pos][0] > indent and not tokens[pos][1].startswith("- "):
                    sub_indent, sub = tokens[pos]
                    if ":" in sub:
                        sk, _, sv = sub.partition(":")
                        m[sk.strip()] = _parse_scalar(sv.strip())
                        pos += 1
                    else:
                        break
                result.append(m)
            else:
                result.append(_parse_scalar(item))
        return result

    parsed = parse_block(0)
    return parsed if parsed is not None else {}


def load_config(path: str) -> dict:
    """加载配置：yaml/json 双格式，读哪个存在用哪个；都没有则用极简解析读 .yaml。"""
    base, _ = os.path.splitext(path)
    candidates = []
    if path.endswith(".yaml") or path.endswith(".yml"):
        candidates = [path, base + ".json"]
    elif path.endswith(".json"):
        candidates = [path, base + ".yaml"]
    else:
        candidates = [path + ".yaml", path + ".json"]

    loaded = None
    used = None
    for c in candidates:
        if os.path.exists(c):
            try:
                if c.endswith(".json"):
                    with open(c, "r", encoding="utf-8") as f:
                        loaded = json.load(f)
                else:
                    try:
                        import yaml  # type: ignore
                        with open(c, "r", encoding="utf-8") as f:
                            loaded = yaml.safe_load(f)
                    except ImportError:
                        with open(c, "r", encoding="utf-8") as f:
                            loaded = _minimal_yaml_parse(f.read())
                used = c
                break
            except Exception as e:  # pragma: no cover - 兜底
                import sys as _sys
                print(f"[config_loader] 解析失败 {c}: {e}", file=_sys.stderr)
                continue
    if loaded is None:
        raise FileNotFoundError(f"找不到配置文件: {path} (尝试过 {candidates})")
    loaded["_config_path"] = used
    return loaded


def deep_get(d: dict, path: str, default=None):
    """深层取值，path 形如 'mllm.fast.model'。"""
    cur = d
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return default
    return cur
