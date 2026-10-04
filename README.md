# prompttest

给 prompt 写回归测试的小工具。测试用例写在 Markdown 文件里，一条命令跑完真实 LLM API，按断言判 PASS/FAIL，每次运行存成 JSON，方便 `diff` 对比两次运行、看哪些测试翻转了。适合放进 CI，每天/每次改 prompt 跑一遍。

纯 Python 标准库，零 pip 依赖。

## 快速开始

```bash
export OPENAI_API_KEY=sk-...
# 先 dry-run 看解析对不对（不联网）
python -m prompttest run examples/demo.pt.md --dry-run
# 真跑
python -m prompttest run examples/demo.pt.md
# 对比两次运行
python -m prompttest diff results/prompttest-20261005-100000.json results/prompttest-20261005-110000.json
```

也支持任何 OpenAI 兼容接口：

```bash
export OPENAI_BASE_URL=https://api.deepseek.com/v1   # 或本地 Ollama: http://localhost:11434/v1
export PROMPTTEST_MODEL=deepseek-chat                 # 默认模型（也可用 --model 指定）
```

## 测试文件格式（`.pt.md`）

```markdown
## formal-tone
```prompt
把这句话改成正式商务语气：尽快回我。
```
expect: 请              # 输出必须包含
expect: /"name"/        # /.../ 表示正则，输出必须匹配
not-expect: 尽快         # 输出不能包含
model: gpt-4o-mini       # 可选，覆盖默认模型
```

- 每个测试以 `## 测试名` 开头；
- prompt 写在 ` ```prompt … ``` ` 代码块里（必需，缺了会报错并点名）；
- 断言写在代码块外面，支持 `expect:` / `not-expect:`，`/正则/` 走正则匹配；
- 所有断言通过才算 PASS；退出码 = 失败的测试数（0 = 全过，CI 直接用）。

## 输出

- 控制台打印每测试 PASS/FAIL 表 + 汇总；
- `results/prompttest-YYYYMMDD-HHMMSS.json` 存完整运行（含 prompt、response、每条断言结果）；
- `diff` 输出每个测试 A/B 两次的结果、是否翻转、两次输出的文本相似度。

## 局限（实话）

- 子串/正则断言很粗糙：只能判"有没有"，判不了"好不好"。复杂语义建议再加一层 LLM-as-judge（本工具不管）。
- LLM 输出有随机性：回归测试默认 `temperature=0` 压住随机；即便如此，模型版本更新仍可能让旧断言失效——这正是回归测试存在的意义，红了就去修 prompt 或断言。
- exit code 取失败数，测试很多时注意 shell 对退出码上限的处理。

## License

MIT
