#!/usr/bin/env python3
"""prompttest — prompt 回归测试小工具。

在 Markdown 文件里写 prompt 测试用例，跑一遍真实 LLM API，
检查输出是否满足断言（包含/正则/禁止出现），并把每次运行存成 JSON，
方便在 CI 里做回归对比。纯标准库，零第三方依赖。
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

VERSION = "0.1.0"
DEFAULT_BASE_URL = "https://api.openai.com/v1"


# ---------------------------------------------------------------- 数据结构

@dataclass
class Assertion:
    kind: str          # "contains" | "regex" | "not_contains" | "not_regex"
    pattern: str
    passed: bool = False
    note: str = ""

    def describe(self) -> str:
        label = {
            "contains": "expect",
            "regex": "expect",
            "not_contains": "not-expect",
            "not_regex": "not-expect",
        }[self.kind]
        pat = f"/{self.pattern}/" if self.kind in ("regex", "not_regex") else self.pattern
        return f"{label}: {pat}"


@dataclass
class TestCase:
    name: str
    prompt: str
    assertions: list[Assertion] = field(default_factory=list)
    model: str = ""


class ParseError(Exception):
    """测试文件格式错误。"""


# ---------------------------------------------------------------- 解析测试文件

def _is_regex_literal(text: str) -> bool:
    return len(text) >= 2 and text.startswith("/") and text.endswith("/")


def parse_test_file(path: str) -> list[TestCase]:
    """解析 .pt.md 测试文件。格式：

    ## test-name
    ```prompt
    ...prompt 内容...
    ```
    expect: 子串
    expect: /正则/
    not-expect: 禁止出现的子串
    model: gpt-4o-mini
    """
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()

    tests: list[TestCase] = []
    current: TestCase | None = None
    in_prompt = False
    prompt_buf: list[str] = []
    has_prompt = False

    def finish_test() -> None:
        nonlocal current, has_prompt
        if current is None:
            return
        if in_prompt:
            raise ParseError(f"测试「{current.name}」的 prompt 代码块没有闭合（缺少结尾的 ```）")
        if not has_prompt:
            raise ParseError(f"测试「{current.name}」缺少 prompt 代码块（需要 ```prompt … ```）")
        tests.append(current)

    for lineno, raw in enumerate(lines, 1):
        line = raw.strip()
        if line.startswith("## ") and not line.startswith("###"):
            finish_test()
            current = TestCase(name=line[3:].strip(), prompt="")
            has_prompt = False
            continue
        if current is None:
            continue  # 测试开始前的说明文字，忽略
        if line.startswith("```"):
            if not in_prompt and line.startswith("```prompt"):
                in_prompt = True
                prompt_buf = []
            elif in_prompt:
                in_prompt = False
                current.prompt = "\n".join(prompt_buf).strip()
                has_prompt = True
            continue
        if in_prompt:
            prompt_buf.append(raw.rstrip("\n"))
            continue
        if line.startswith("expect:"):
            body = line[len("expect:"):].strip()
            if _is_regex_literal(body):
                current.assertions.append(Assertion("regex", body[1:-1]))
            else:
                current.assertions.append(Assertion("contains", body))
        elif line.startswith("not-expect:"):
            body = line[len("not-expect:"):].strip()
            if _is_regex_literal(body):
                current.assertions.append(Assertion("not_regex", body[1:-1]))
            else:
                current.assertions.append(Assertion("not_contains", body))
        elif line.startswith("model:"):
            current.model = line[len("model:"):].strip()

    finish_test()

    if not tests:
        raise ParseError("文件中没有找到任何测试（测试以 `## 测试名` 开头）")
    return tests


# ---------------------------------------------------------------- LLM 调用

def get_api_key() -> str:
    key = os.environ.get("OPENAI_API_KEY") or os.environ.get("PROMPTTEST_API_KEY")
    if not key:
        print("error: 未找到 API key。请先设置环境变量 OPENAI_API_KEY（或 PROMPTTEST_API_KEY）。",
              file=sys.stderr)
        sys.exit(2)
    return key


def chat_complete(base_url: str, api_key: str, model: str, prompt: str) -> str:
    url = base_url.rstrip("/") + "/chat/completions"
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,  # 回归测试默认 0，减少随机性
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"API 返回 HTTP {e.code}: {body}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"网络请求失败: {e.reason}")
    try:
        return data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(f"API 返回格式异常: {str(data)[:200]}")


# ---------------------------------------------------------------- 断言检查

def check_assertions(response: str, assertions: list[Assertion]) -> None:
    for a in assertions:
        if a.kind == "contains":
            a.passed = a.pattern in response
            a.note = "" if a.passed else f"输出中未找到「{a.pattern}」"
        elif a.kind == "not_contains":
            a.passed = a.pattern not in response
            a.note = "" if a.passed else f"输出中出现了禁用的「{a.pattern}」"
        elif a.kind in ("regex", "not_regex"):
            try:
                hit = re.search(a.pattern, response) is not None
            except re.error as e:
                a.passed = False
                a.note = f"正则写错了: {e}"
                continue
            if a.kind == "regex":
                a.passed = hit
                a.note = "" if hit else f"输出不符合正则 /{a.pattern}/"
            else:
                a.passed = not hit
                a.note = "" if not hit else f"输出命中了禁用的正则 /{a.pattern}/"


# ---------------------------------------------------------------- 输出表格

def print_run_table(results: list[dict]) -> None:
    name_w = max([4] + [len(r["name"]) for r in results])
    print(f"\n{'测试'.ljust(name_w)}  结果   断言")
    print("-" * (name_w + 30))
    for r in results:
        total = len(r["assertions"])
        ok = sum(1 for a in r["assertions"] if a["passed"])
        status = "PASS" if r["passed"] else "FAIL"
        print(f"{r['name'].ljust(name_w)}  {status}   {ok}/{total} 通过")
        for a in r["assertions"]:
            if not a["passed"]:
                print(f"{' ' * (name_w + 2)}  ✗ {a['note']}")
    passed = sum(1 for r in results if r["passed"])
    failed = len(results) - passed
    print(f"\n共 {len(results)} 个测试: {passed} 通过, {failed} 失败")


# ---------------------------------------------------------------- run

def cmd_run(args: argparse.Namespace) -> int:
    try:
        tests = parse_test_file(args.file)
    except ParseError as e:
        print(f"error: 测试文件解析失败: {e}", file=sys.stderr)
        return 2
    except OSError as e:
        print(f"error: 读不到测试文件 {args.file}: {e}", file=sys.stderr)
        return 2

    if args.dry_run:
        print(f"测试文件: {args.file}（dry-run，只解析不联网）\n")
        for t in tests:
            print(f"## {t.name}")
            preview = t.prompt.replace("\n", " ")
            if len(preview) > 60:
                preview = preview[:60] + "…"
            print(f"  prompt: {preview}")
            print(f"  model: {t.model or args.model or '(默认)'}")
            for a in t.assertions:
                print(f"  - {a.describe()}")
            print()
        print(f"共 {len(tests)} 个测试，{sum(len(t.assertions) for t in tests)} 条断言。")
        return 0

    api_key = get_api_key()
    base_url = os.environ.get("OPENAI_BASE_URL", DEFAULT_BASE_URL)
    default_model = args.model or os.environ.get("PROMPTTEST_MODEL", "gpt-4o-mini")

    results: list[dict] = []
    for i, t in enumerate(tests, 1):
        model = t.model or default_model
        print(f"[{i}/{len(tests)}] 跑 {t.name} …", end=" ", flush=True)
        try:
            response = chat_complete(base_url, api_key, model, t.prompt)
            err = ""
        except RuntimeError as e:
            response, err = "", str(e)
        if err:
            print(f"调用失败: {err}")
            results.append({"name": t.name, "prompt": t.prompt, "model": model,
                            "response": "", "error": err, "passed": False,
                            "assertions": [{"kind": a.kind, "pattern": a.pattern,
                                            "passed": False, "note": "API 调用失败"} for a in t.assertions]})
            continue
        print("OK")
        check_assertions(response, t.assertions)
        passed = all(a.passed for a in t.assertions)
        results.append({"name": t.name, "prompt": t.prompt, "model": model,
                        "response": response, "error": "",
                        "passed": passed,
                        "assertions": [{"kind": a.kind, "pattern": a.pattern,
                                        "passed": a.passed, "note": a.note} for a in t.assertions]})

    print_run_table(results)

    out_dir = args.out or "results"
    os.makedirs(out_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    run_path = os.path.join(out_dir, f"prompttest-{stamp}.json")
    with open(run_path, "w", encoding="utf-8") as f:
        json.dump({
            "timestamp": stamp,
            "file": args.file,
            "model": default_model,
            "base_url": base_url,
            "summary": {
                "total": len(results),
                "passed": sum(1 for r in results if r["passed"]),
                "failed": sum(1 for r in results if not r["passed"]),
            },
            "tests": results,
        }, f, ensure_ascii=False, indent=2)
    print(f"\n运行结果已保存: {run_path}")

    failed = sum(1 for r in results if not r["passed"])
    return failed  # exit code = 失败数，0 表示全过


# ---------------------------------------------------------------- diff

def load_run(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def cmd_diff(args: argparse.Namespace) -> int:
    try:
        a, b = load_run(args.a), load_run(args.b)
    except (OSError, json.JSONDecodeError) as e:
        print(f"error: 读运行结果失败: {e}", file=sys.stderr)
        return 2

    ta = {t["name"]: t for t in a.get("tests", [])}
    tb = {t["name"]: t for t in b.get("tests", [])}
    names = sorted(set(ta) | set(tb))

    print(f"\n对比: {args.a} ({a.get('timestamp', '?')})  vs  {args.b} ({b.get('timestamp', '?')})")
    print(f"{'测试':<20} {'A':<6} {'B':<6} {'状态':<8} 输出相似度")
    print("-" * 60)
    flips = 0
    for name in names:
        ra, rb = ta.get(name), tb.get(name)
        if ra is None:
            print(f"{name:<20} {'—':<6} {'PASS' if rb['passed'] else 'FAIL':<6} {'新增':<8} —")
            continue
        if rb is None:
            print(f"{name:<20} {'PASS' if ra['passed'] else 'FAIL':<6} {'—':<6} {'删除':<8} —")
            continue
        sa = "PASS" if ra["passed"] else "FAIL"
        sb = "PASS" if rb["passed"] else "FAIL"
        sim = difflib.SequenceMatcher(None, ra.get("response", ""), rb.get("response", "")).ratio()
        if sa == sb:
            status = "未变"
        else:
            status = f"翻转({sa}→{sb})"
            flips += 1
        print(f"{name:<20} {sa:<6} {sb:<6} {status:<8} {sim:.0%}")
    print(f"\n共 {len(names)} 个测试，其中 {flips} 个结果翻转。")
    return 0


# ---------------------------------------------------------------- CLI

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="prompttest", description="prompt 回归测试: 用 Markdown 写用例，跑 LLM API，做断言对比。")
    p.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="运行测试文件")
    r.add_argument("file", help="测试文件 (.pt.md)")
    r.add_argument("--model", "-m", default="", help="默认模型（测试内 model: 可覆盖）")
    r.add_argument("--out", "-o", default="results", help="运行结果 JSON 保存目录（默认 results/）")
    r.add_argument("--dry-run", action="store_true", help="只解析文件、列出测试与断言，不联网")
    r.set_defaults(func=cmd_run)

    d = sub.add_parser("diff", help="对比两次运行结果")
    d.add_argument("a", help="第一次运行的 JSON")
    d.add_argument("b", help="第二次运行的 JSON")
    d.set_defaults(func=cmd_diff)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
