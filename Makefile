# 开发与提交用的固定命令。
#
# 为什么要有这个文件：
#   以前每次检查都现写一长串命令（格式化 && 检查 && 测试 && ...）。写错一个符号（比如把 && 写成 ;），
#   或者中间某一步失败，都得排查、重跑，白白多花几轮；开发中顺手跑格式化还会改动文件，
#   让整个文件的内容重新灌进对话，越到后面越贵。
#   把命令固定下来、写一次写对，以后只调用 make check / make ready：
#     - make 任何一步失败都会立刻停下，不会出现「前面失败了，后面还在跑」
#     - 开发中只检查、不改文件；格式化只在提交前做一次，而且只动这次改过的文件
#
# 用法：
#   make check                            开发中：检查写法与模块依赖 + 跑全部离线测试
#   make check TESTS=tests/test_sync.py   开发中：只跑指定的测试文件，更快
#   make ready                            提交前：格式化改过的文件 + 检查 + 全部离线测试

.PHONY: check ready

# 这两个测试文件要连 Tushare，平时不跑
OFFLINE = --ignore=tests/test_tushare_live.py --ignore=tests/test_panel_live.py
TESTS ?= tests

# 相对上一次提交改过的、以及新加的 .py 文件（删掉的不算）。
# 提交前只格式化它们：格式化整个目录会顺手改到无关的文件，把不相干的改动混进这次提交
CHANGED_PY = $(shell { git diff --name-only --diff-filter=d HEAD; git ls-files --others --exclude-standard; } | grep '\.py$$')

# 开发中用：只检查、不修改任何文件
check:
	uv run ruff check litmus tests
	uv run lint-imports
	uv run pytest -q $(OFFLINE) $(TESTS)

# 提交前用：格式化改过的文件，再做完整检查和全部离线测试
ready:
	@if [ -n "$(strip $(CHANGED_PY))" ]; then uv run ruff format $(CHANGED_PY); fi
	uv run ruff check litmus tests
	uv run lint-imports
	uv run pytest -q $(OFFLINE) tests
