# -*- coding: utf-8 -*-
"""
工作区技术栈识别（与前端 context.js parseTechStack 语义对齐）。
纯函数，便于单测；清单文件内容由调用方读取后传入。
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List

MANIFEST_FILES = [
    "package.json",
    "go.mod",
    "requirements.txt",
    "pom.xml",
    "Cargo.toml",
]

NPM_FRAMEWORK_MAP = {
    "react": "React",
    "vue": "Vue",
    "@angular/core": "Angular",
    "express": "Express",
    "koa": "Koa",
    "@nestjs/core": "NestJS",
    "next": "Next.js",
    "nuxt": "Nuxt",
    "gatsby": "Gatsby",
    "react-dom": "React DOM",
    "pinia": "Pinia",
    "vuex": "Vuex",
    "react-router-dom": "React Router",
    "axios": "Axios",
}


def parse_tech_stack(file_entries: Dict[str, str] | None) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "languages": [],
        "frameworks": [],
        "runtimes": [],
        "manifestFiles": [],
    }
    if not file_entries:
        return result

    def add_language(lang: str) -> None:
        if lang not in result["languages"]:
            result["languages"].append(lang)

    def add_runtime(rt: str) -> None:
        if rt not in result["runtimes"]:
            result["runtimes"].append(rt)

    def add_framework(name: str, version: str, source: str) -> None:
        result["frameworks"].append({
            "name": name,
            "version": version or "unknown",
            "source": source,
        })

    pkg_raw = file_entries.get("package.json")
    if pkg_raw:
        result["manifestFiles"].append("package.json")
        try:
            pkg = json.loads(pkg_raw)
        except json.JSONDecodeError:
            pkg = None
        if pkg:
            add_language("JavaScript")
            deps: Dict[str, str] = {}
            deps.update(pkg.get("dependencies") or {})
            deps.update(pkg.get("devDependencies") or {})
            if deps.get("typescript") or (pkg.get("devDependencies") or {}).get("typescript"):
                add_language("TypeScript")
            if deps.get("vite"):
                add_framework("Vite", deps["vite"], "package.json")
            if deps.get("webpack"):
                add_framework("Webpack", deps["webpack"], "package.json")
            for dep, label in NPM_FRAMEWORK_MAP.items():
                if dep in deps:
                    add_framework(label, deps[dep], "package.json")
            engines = pkg.get("engines") or {}
            add_runtime(f"Node {engines['node']}" if engines.get("node") else "Node")

    go_mod = file_entries.get("go.mod")
    if go_mod:
        result["manifestFiles"].append("go.mod")
        add_language("Go")
        go_ver = re.search(r"^go\s+([\d.]+)", go_mod, re.M)
        if go_ver:
            add_runtime(f"Go {go_ver.group(1)}")
        go_map = {
            "gin": "Gin", "echo": "Echo", "fiber": "Fiber",
            "gorm": "GORM", "grpc": "gRPC", "kratos": "Kratos",
        }
        for mod, label in go_map.items():
            if re.search(rf"[\w.-]+/{mod}\b", go_mod):
                ver = re.search(rf"[\w.-]+/{mod}\s+(v?[\d.\w+-]+)", go_mod)
                add_framework(label, ver.group(1) if ver else "unknown", "go.mod")

    req_txt = file_entries.get("requirements.txt")
    if req_txt:
        result["manifestFiles"].append("requirements.txt")
        add_language("Python")
        add_runtime("Python 3")
        py_map = {
            "fastapi": "FastAPI", "flask": "Flask", "django": "Django",
            "tornado": "Tornado", "pydantic": "Pydantic", "sqlalchemy": "SQLAlchemy",
        }
        for line in req_txt.splitlines():
            m = re.match(r"^([A-Za-z0-9_.\-]+)\s*(?:[=<>!~]=?\s*([\w.\-*]+))?", line.strip())
            if not m:
                continue
            pkg_name = m.group(1).lower()
            if pkg_name in py_map:
                add_framework(py_map[pkg_name], m.group(2) or "unknown", "requirements.txt")

    pom = file_entries.get("pom.xml")
    if pom:
        result["manifestFiles"].append("pom.xml")
        add_language("Java")
        add_runtime("JVM (Maven)")
        java_map = {
            "spring-boot": "Spring Boot",
            "springframework": "Spring Framework",
            "mybatis": "MyBatis",
            "lombok": "Lombok",
        }
        for art, label in java_map.items():
            if art in pom:
                add_framework(label, "unknown", "pom.xml")

    cargo = file_entries.get("Cargo.toml")
    if cargo:
        result["manifestFiles"].append("Cargo.toml")
        add_language("Rust")
        add_runtime("Cargo")
        rs_map = {
            "actix-web": "Actix-web", "tokio": "Tokio", "rocket": "Rocket",
            "serde": "Serde", "warp": "Warp",
        }
        for crate, label in rs_map.items():
            pattern = rf"^{re.escape(crate)}\s*="
            if re.search(pattern, cargo, re.M):
                add_framework(label, "unknown", "Cargo.toml")

    seen: set[str] = set()
    deduped: List[Dict[str, str]] = []
    for fw in result["frameworks"]:
        key = f"{fw['name']}@{fw['source']}"
        if key in seen:
            continue
        seen.add(key)
        deduped.append(fw)
    result["frameworks"] = deduped
    return result
