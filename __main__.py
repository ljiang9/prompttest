"""prompttest 入口: python -m prompttest ..."""
try:
    from .prompttest import main
except ImportError:  # 直接 python __main__.py 运行时的 fallback
    from prompttest import main

if __name__ == "__main__":
    raise SystemExit(main())
