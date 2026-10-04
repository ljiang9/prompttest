# Prompt 回归测试示例

用 `prompttest run examples/demo.pt.md --dry-run` 先看解析结果，
配好 `OPENAI_API_KEY` 后去掉 `--dry-run` 真跑。

## formal-tone

```prompt
把这句话改成正式商务语气：尽快回我。
```

expect: 请
not-expect: 尽快

## json-reply

```prompt
用 JSON 回复：你的名字叫什么？
```

expect: /"name"/
not-expect: 不知道
