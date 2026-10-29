# memory/skill_library.py —— 技能库（Voyager 式：技能=py 代码文件 + meta）
# 列表/调用接口即可。技能文件形如 def <name>(env, **params): ...  + 同级 meta 由文件名推断。

import os
import importlib.util
import json


class SkillLibrary:
    def __init__(self, skills_dir: str = "skills"):
        self.skills_dir = skills_dir
        os.makedirs(skills_dir, exist_ok=True)

    def list_skills(self) -> list:
        """列出技能菜单：{name, file, trigger}。"""
        out = []
        for fn in sorted(os.listdir(self.skills_dir)):
            if fn.endswith(".py") and not fn.startswith("_"):
                name = fn[:-3]
                meta = self._read_meta(name)
                out.append({
                    "name": name,
                    "file": fn,
                    "trigger": meta.get("trigger", ""),
                    "success_rate": meta.get("success_rate", None),
                })
        return out

    def _meta_path(self, name: str) -> str:
        return os.path.join(self.skills_dir, name + ".meta.json")

    def _read_meta(self, name: str) -> dict:
        p = self._meta_path(name)
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def save_skill(self, name: str, code: str, trigger: str = "", meta: dict | None = None):
        """保存技能代码 + meta（来自 P10 技能抽象）。"""
        with open(os.path.join(self.skills_dir, name + ".py"), "w", encoding="utf-8") as f:
            f.write(code)
        m = meta or {}
        m.setdefault("trigger", trigger)
        with open(self._meta_path(name), "w", encoding="utf-8") as f:
            json.dump(m, f, ensure_ascii=False, indent=2)

    def invoke_skill(self, name: str, env=None, **params):
        """加载并执行技能函数（与技能库同名）。失败返回 (False, reason)。"""
        path = os.path.join(self.skills_dir, name + ".py")
        if not os.path.exists(path):
            return (False, "skill_not_found")
        try:
            spec = importlib.util.spec_from_file_location(f"_skill_{name}", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            fn = getattr(mod, name, None)
            if fn is None:
                return (False, "no_entry_fn")
            result = fn(env, **params)
            return (True, result)
        except Exception as e:
            return (False, str(e))
